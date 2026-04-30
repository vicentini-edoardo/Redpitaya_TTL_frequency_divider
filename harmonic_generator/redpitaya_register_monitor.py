#!/usr/bin/env python3
"""
redpitaya_register_monitor.py — harmonic generator variant

Poll the Red Pitaya FPGA harmonic generator registers over SSH and print a
compact live view. The board must have /root/rp_harmonic_ctl compiled.

Typical use:
  python3 redpitaya_register_monitor.py --host rp-f06a51.local
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time


CLOCK_HZ  = 125_000_000
BASE_ADDR = 0x40600000
REMOTE_BIN = "/root/rp_harmonic_ctl"


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


def _freq_from_period(period_cycles: int) -> float:
    if period_cycles <= 0:
        return 0.0
    return CLOCK_HZ / period_cycles


def _hz_from_phase_word(word: int) -> float:
    return word * CLOCK_HZ / (2**48)


def print_snapshot(data: dict) -> None:
    control       = int(data.get("control", 0))
    mult_n        = int(data.get("mult_n", 1))
    status        = int(data.get("status", 0))
    period_stable = int(data.get("period_stable", 0))
    freerun       = int(data.get("freerun_active", 0))
    meas_time_us  = int(data.get("meas_time_us", 0))
    raw_period    = int(data.get("raw_period", 0))
    avg_period    = int(data.get("period_avg", 0))
    step_offset   = int(data.get("phase_step_offset", 0))
    step_base     = int(data.get("phase_step_base", 0))
    step_live     = int(data.get("phase_step", 0))

    pulse_enable = control & 0x1
    pulse_busy   = (status >> 0) & 0x1
    period_valid = (status >> 1) & 0x1
    timeout_flag = (status >> 3) & 0x1

    raw_freq    = _freq_from_period(raw_period)
    avg_freq    = _freq_from_period(avg_period)
    shift_hz    = _hz_from_phase_word(step_offset)
    output_hz   = mult_n * avg_freq + shift_hz

    print(
        f"ctrl=0x{control:02X} [en={pulse_enable}]  "
        f"status=0x{status:02X} [busy={pulse_busy} valid={period_valid} "
        f"stable={period_stable} timeout={timeout_flag} freerun={freerun}]  "
        f"window={meas_time_us} µs"
    )
    print(
        f"mult_n={mult_n}  "
        f"period_raw={raw_period} cyc ({fmt_freq_hz(raw_freq)})  "
        f"period_avg={avg_period} cyc ({fmt_freq_hz(avg_freq)})"
    )
    print(
        f"phase_step_offset={step_offset:+d} ({shift_hz:+.6f} Hz)  "
        f"phase_step_base={step_base}  phase_step={step_live}"
    )
    print(
        f"output_hz = {mult_n} * {fmt_freq_hz(avg_freq)} + {shift_hz:+.3f} Hz "
        f"= {fmt_freq_hz(output_hz)}"
    )
    print("-" * 100)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Red Pitaya FPGA harmonic generator registers over SSH."
    )
    parser.add_argument("--host", required=True, help="Red Pitaya hostname or IP")
    parser.add_argument("--user", default="root", help="SSH user")
    parser.add_argument("--port", type=int, default=22, help="SSH port")
    parser.add_argument("--base-addr", default="0x40600000", help="AXI base address")
    parser.add_argument("--interval", type=float, default=0.5, help="Polling interval in seconds")
    parser.add_argument("--count", type=int, default=0, help="Number of samples; 0 = forever")
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
