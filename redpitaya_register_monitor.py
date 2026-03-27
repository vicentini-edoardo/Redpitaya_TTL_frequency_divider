#!/usr/bin/env python3
"""
redpitaya_register_monitor.py

Poll the Red Pitaya FPGA pulse-generator registers over SSH using the existing
rp_pulse_ctl helper binary and print a compact live view for debugging.

Typical use:
  python3 redpitaya_register_monitor.py --host rp-f06a51.local

The board must already have /root/rp_pulse_ctl compiled.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time


CLOCK_HZ = 125_000_000
BASE_ADDR = 0x40600000
REMOTE_BIN = "/root/rp_pulse_ctl"

CONTROL_PULSE_ENABLE = 0x1
CONTROL_SOFT_RESET = 0x2
CONTROL_PHASE_MOD_ENABLE = 0x4


def fmt_freq_hz(freq_hz: float) -> str:
    if freq_hz >= 1e6:
        return f"{freq_hz / 1e6:.6g} MHz"
    if freq_hz >= 1e3:
        return f"{freq_hz / 1e3:.6g} kHz"
    return f"{freq_hz:.6g} Hz"


class RemoteCtl:
    def __init__(self, host: str, user: str, port: int):
        self.host = host
        self.user = user
        self.port = port

    def run(self, cmd: str) -> str:
        ssh_cmd = ["ssh", "-p", str(self.port), f"{self.user}@{self.host}", cmd]
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "SSH command failed.")
        return proc.stdout.strip()

    def read(self, base_addr: int) -> dict:
        remote_cmd = " ".join(
            [shlex.quote(REMOTE_BIN), shlex.quote(hex(base_addr)), shlex.quote("read")]
        )
        return json.loads(self.run(remote_cmd))


def _periods_from_payload(data: dict) -> tuple[int, int]:
    raw_period = int(data.get("period", data.get("raw_period", 0)))
    avg_period = int(data.get("period_avg", data.get("filt_period", 0)))
    return raw_period, avg_period


def _freq_from_period(period_cycles: int) -> float:
    if period_cycles <= 0:
        return 0.0
    return CLOCK_HZ / period_cycles


def _mod_freq_from_word(phase_word: int) -> float:
    return phase_word * CLOCK_HZ / (2**32)


def _mod_amp_from_q15(amp_q15: int) -> float:
    return amp_q15 / 32767 if 32767 > 0 else 0.0


def print_snapshot(data: dict) -> None:
    control = int(data.get("control", 0))
    divider = int(data.get("divider", 0))
    width = int(data.get("width", 0))
    delay = int(data.get("delay", 0))
    status = int(data.get("status", 0))
    phase_freq = int(data.get("phase_freq", 0))
    phase_amp_q15 = int(data.get("phase_amp_q15", 0))
    raw_period, avg_period = _periods_from_payload(data)

    pulse_enable = 1 if (control & CONTROL_PULSE_ENABLE) else 0
    soft_reset = 1 if (control & CONTROL_SOFT_RESET) else 0
    phase_mod = 1 if (control & CONTROL_PHASE_MOD_ENABLE) else 0

    pulse_busy = (status >> 0) & 0x1
    period_valid = (status >> 1) & 0x1
    timeout_flag = (status >> 2) & 0x1

    raw_freq = _freq_from_period(raw_period)
    avg_freq = _freq_from_period(avg_period)
    out_freq = avg_freq / max(1, divider) if avg_freq > 0 else 0.0
    mod_freq = _mod_freq_from_word(phase_freq)
    mod_amp = _mod_amp_from_q15(phase_amp_q15)

    print(
        f"control=0x{control:08X} [pulse_en={pulse_enable} reset={soft_reset} phase_mod={phase_mod}]  "
        f"status=0x{status:08X} [busy={pulse_busy} valid={period_valid} timeout={timeout_flag}]"
    )
    print(
        f"divider={divider}  width={width} cyc  delay={delay} cyc  "
        f"phase_freq=0x{phase_freq:08X} ({fmt_freq_hz(mod_freq)})  "
        f"phase_amp_q15={phase_amp_q15} ({mod_amp:.2f}T)"
    )
    print(
        f"period={raw_period} cyc ({fmt_freq_hz(raw_freq)})  "
        f"period_avg={avg_period} cyc ({fmt_freq_hz(avg_freq)})  "
        f"derived_output={fmt_freq_hz(out_freq)}"
    )
    print("-" * 120)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Red Pitaya FPGA pulse-generator registers over SSH.")
    parser.add_argument("--host", required=True, help="Red Pitaya hostname or IP")
    parser.add_argument("--user", default="root", help="SSH user")
    parser.add_argument("--port", type=int, default=22, help="SSH port")
    parser.add_argument("--base-addr", default="0x40600000", help="AXI base address")
    parser.add_argument("--interval", type=float, default=0.5, help="Polling interval in seconds")
    parser.add_argument("--count", type=int, default=0, help="Number of samples to read; 0 means forever")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not shutil.which("ssh"):
        print("OpenSSH client not found.", file=sys.stderr)
        return 1

    base_addr = int(str(args.base_addr).replace("_", ""), 0)
    remote = RemoteCtl(args.host, args.user, args.port)

    sample_idx = 0
    try:
        while True:
            data = remote.read(base_addr)
            print_snapshot(data)
            sample_idx += 1
            if args.count > 0 and sample_idx >= args.count:
                break
            time.sleep(max(0.05, args.interval))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
