#!/usr/bin/env python3
"""
Direct PicoSDK hardware verification for the Red Pitaya TTL frequency divider.

The script drives the Red Pitaya helper over SSH, captures TTL input/output
waveforms through a PicoScope 4000A-family acquisition card, analyzes the
captured edges, and writes a compact debug bundle that can be shared for
follow-up debugging.
"""
from __future__ import annotations

import argparse
import ctypes
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence, Union

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rp_math import (
    CLK_HZ,
    CTRL_ENABLE,
    CTRL_FORCE_HIGH,
    CTRL_OSC_MODE,
    DEFAULT_BASE,
    duty_to_cycles,
    f_shift_from_f_osc,
    hz_to_phase,
    osc_half_period_cycles,
    osc_phase_preload,
    phase_to_hz,
    trig_hz_to_phase_step,
)


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    WARN = "WARN"


@dataclass(frozen=True)
class Edge:
    time_s: float
    rising: bool


@dataclass(frozen=True)
class AnalysisConfig:
    threshold_v: float = 1.5
    min_edges: int = 5
    freq_rel_tol: float = 0.002
    freq_abs_tol_hz: float = 2.0
    duty_abs_tol: float = 0.05
    osc_phase_abs_tol: float = 0.025
    osc_freq_rel_tol: float = 0.10
    # Strict frequency-match check (pulse mode, multiplier 1 only): the measured
    # output frequency must equal the FPGA-commanded frequency
    # (phase_to_hz(phase_step)). freq_match_abs_tol_hz is the statistical
    # resolution floor (the coherent estimator reaches well under 1 mHz);
    # freq_match_timebase_rel_tol is the systematic allowance for the
    # independent PicoScope vs Red Pitaya sample clocks (tens of ppm), which
    # sets the real floor on any *absolute* frequency comparison. True
    # sub-millihertz verification needs a clock-independent ratio (DIO2).
    freq_match_abs_tol_hz: float = 0.001
    freq_match_timebase_rel_tol: float = 1e-4


@dataclass(frozen=True)
class PulseExpectation:
    input_multiplier: int
    shift_hz: float
    duty_frac: float | None = None


@dataclass(frozen=True)
class ConstantExpectation:
    high: bool


@dataclass(frozen=True)
class OscExpectation:
    f_osc_hz: float
    p_frac: float
    p0_frac: float


Expectation = Union[PulseExpectation, ConstantExpectation, OscExpectation]


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    messages: list[str] = field(default_factory=list)
    metrics: dict[str, float | int | str] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "messages": self.messages,
            "metrics": self.metrics,
        }


@dataclass(frozen=True)
class Capture:
    times_s: list[float]
    channels_v: dict[str, list[float]]


@dataclass(frozen=True)
class HardwareTest:
    name: str
    mode: str
    expectation: Expectation
    capture_seconds: float
    settle_seconds: float = 0.25
    shift_hz: float = 0.0
    duty_frac: float = 0.50
    harmonic_n: int = 1
    f_osc_hz: float = 0.0
    p_frac: float = 0.0
    p0_frac: float = 0.0


class RedPitayaCommandBuilder:
    def __init__(self, base_addr: int = DEFAULT_BASE):
        self.base = hex(base_addr)

    def pulse_control(self, ctrl: int) -> list[str]:
        return ["/root/rp_pulse_ctl", self.base, "control", str(ctrl)]

    def harmonic_control(self, ctrl: int) -> list[str]:
        return ["/root/rp_harmonic_ctl", self.base, "control", str(ctrl)]

    def pulse_write(self, width_cycles: int, shift_hz: float, ctrl: int = CTRL_ENABLE) -> list[str]:
        return [
            "/root/rp_pulse_ctl",
            self.base,
            "write",
            str(width_cycles),
            str(hz_to_phase(shift_hz)),
            str(ctrl),
        ]

    def harmonic_write(self, mult_n: int, shift_hz: float, ctrl: int = CTRL_ENABLE) -> list[str]:
        return [
            "/root/rp_harmonic_ctl",
            self.base,
            "write",
            str(mult_n),
            str(hz_to_phase(shift_hz)),
            str(ctrl),
        ]

    def osc_apply(
        self,
        width_cycles: int,
        half_period_cycles: int,
        preload: int,
        shift_hz: float,
    ) -> list[list[str]]:
        return [
            ["/root/rp_pulse_ctl", self.base, "osc", str(half_period_cycles), str(preload)],
            self.pulse_write(width_cycles, shift_hz, CTRL_ENABLE | CTRL_OSC_MODE),
        ]

    def trig(self, freq_hz: float) -> list[str]:
        return ["/root/rp_pulse_ctl", self.base, "trig", str(trig_hz_to_phase_step(freq_hz))]

    def read(self, harmonic: bool = False) -> list[str]:
        helper = "/root/rp_harmonic_ctl" if harmonic else "/root/rp_pulse_ctl"
        return [helper, self.base, "read"]

    def window(self, microseconds: int, harmonic: bool = False) -> list[str]:
        helper = "/root/rp_harmonic_ctl" if harmonic else "/root/rp_pulse_ctl"
        return [helper, self.base, "window", str(microseconds)]


class RedPitayaSSH:
    def __init__(
        self,
        host: str,
        username: str = "root",
        port: int = 22,
        key_filename: str | None = None,
        password: str | None = None,
        timeout_s: float = 10.0,
    ):
        self.host = host
        self.username = username
        self.port = port
        self.key_filename = key_filename
        self.password = password
        self.timeout_s = timeout_s
        self._client: Any = None

    def __enter__(self) -> "RedPitayaSSH":
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("paramiko is required for Red Pitaya SSH control. Install requirements-picosdk.txt.") from exc

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            key_filename=self.key_filename,
            timeout=self.timeout_s,
            banner_timeout=self.timeout_s,
            auth_timeout=self.timeout_s,
        )
        self._client = client
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def run(self, argv: Sequence[str]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("SSH client is not connected")
        command = " ".join(_shell_quote(arg) for arg in argv)
        stdin, stdout, stderr = self._client.exec_command(command, timeout=self.timeout_s)
        del stdin
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(f"Red Pitaya command failed ({rc}): {command}\n{err}")
        try:
            return json.loads(out.splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise RuntimeError(f"Red Pitaya command did not return JSON: {command}\nstdout={out!r}\nstderr={err!r}") from exc


def _shell_quote(text: str) -> str:
    if not text:
        return "''"
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:-+"
    if all(ch in safe for ch in text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


class Pico4000aScope:
    """Small block-capture wrapper around the PicoSDK ps4000a Python module."""

    CHANNEL_NAMES = {"A": "PS4000A_CHANNEL_A", "B": "PS4000A_CHANNEL_B", "C": "PS4000A_CHANNEL_C", "D": "PS4000A_CHANNEL_D"}
    # Integer indices match the ps4000a driver enum (0=10mV … 11=50V).
    # The SDK examples pass these integers directly to ps4000aSetChannel and adc2mV.
    RANGE_BY_VOLTS = {
        0.05: 2,   # 50 mV
        0.1:  3,   # 100 mV
        0.2:  4,   # 200 mV
        0.5:  5,   # 500 mV
        1.0:  6,   # 1 V
        2.0:  7,   # 2 V
        5.0:  8,   # 5 V
        10.0: 9,   # 10 V
        20.0: 10,  # 20 V
        50.0: 11,  # 50 V
    }

    def __init__(self, channels: Sequence[str], sample_rate_hz: float, range_v: float = 5.0):
        self.channels = [ch.upper() for ch in channels]
        self.sample_rate_hz = float(sample_rate_hz)
        self.range_v = float(range_v)
        self._handle = ctypes.c_int16()
        self._ps: Any = None
        self._assert_pico_ok: Any = None
        self._adc2mv: Any = None

    def __enter__(self) -> "Pico4000aScope":
        try:
            from picosdk.functions import adc2mV, assert_pico_ok
            from picosdk.ps4000a import ps4000a as ps
        except ImportError as exc:
            raise RuntimeError(
                "PicoSDK Python wrapper is required. Install requirements-picosdk.txt and Pico Technology's system PicoSDK."
            ) from exc

        self._ps = ps
        self._assert_pico_ok = assert_pico_ok
        self._adc2mv = adc2mV
        status = ps.ps4000aOpenUnit(ctypes.byref(self._handle), None)
        self._assert_open_status_ok(status)

        for channel in ("A", "B", "C", "D"):
            enabled = 1 if channel in self.channels else 0
            self._set_channel(channel, enabled)
        return self

    def _assert_open_status_ok(self, status: int) -> None:
        try:
            self._assert_pico_ok(status)
            return
        except Exception:
            pass
        try:
            from picosdk.constants import PICO_STATUS
        except ImportError:
            self._assert_pico_ok(status)
            return
        power_status_names = {
            "PICO_POWER_SUPPLY_CONNECTED",
            "PICO_POWER_SUPPLY_NOT_CONNECTED",
            "PICO_USB3_0_DEVICE_NON_USB3_0_PORT",
        }
        status_name_by_value = {value: name for name, value in PICO_STATUS.items()}
        if status_name_by_value.get(status) in power_status_names:
            self._assert_pico_ok(self._ps.ps4000aChangePowerSource(self._handle, status))
            return
        self._assert_pico_ok(status)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._ps is not None:
            try:
                self._ps.ps4000aStop(self._handle)
            finally:
                self._ps.ps4000aCloseUnit(self._handle)

    def _set_channel(self, channel: str, enabled: int) -> None:
        ps = self._ps
        range_idx = self.RANGE_BY_VOLTS[_nearest_range_key(self.range_v, self.RANGE_BY_VOLTS)]
        status = ps.ps4000aSetChannel(
            self._handle,
            ps.PS4000A_CHANNEL[self.CHANNEL_NAMES[channel]],
            enabled,
            ps.PS4000A_COUPLING["PS4000A_DC"],
            range_idx,
            0.0,
        )
        self._assert_pico_ok(status)

    def capture(self, duration_s: float) -> Capture:
        ps = self._ps
        if ps is None:
            raise RuntimeError("PicoScope is not open")
        samples = max(100, int(round(duration_s * self.sample_rate_hz)))
        timebase, actual_interval_ns = self._choose_timebase(samples)
        buffers: dict[str, Any] = {}
        max_adc = ctypes.c_int16()
        self._assert_pico_ok(ps.ps4000aMaximumValue(self._handle, ctypes.byref(max_adc)))

        for channel in self.channels:
            buf = (ctypes.c_int16 * samples)()
            buffers[channel] = buf
            status = ps.ps4000aSetDataBuffer(
                self._handle,
                ps.PS4000A_CHANNEL[self.CHANNEL_NAMES[channel]],
                buf,
                samples,
                0,
                ps.PS4000A_RATIO_MODE["PS4000A_RATIO_MODE_NONE"],
            )
            self._assert_pico_ok(status)

        self._assert_pico_ok(ps.ps4000aRunBlock(self._handle, 0, samples, timebase, None, 0, None, None))
        ready = ctypes.c_int16(0)
        while not ready.value:
            self._assert_pico_ok(ps.ps4000aIsReady(self._handle, ctypes.byref(ready)))
            time.sleep(0.01)

        sample_count = ctypes.c_int32(samples)
        overflow = ctypes.c_int16()
        self._assert_pico_ok(
            ps.ps4000aGetValues(
                self._handle,
                0,
                ctypes.byref(sample_count),
                0,
                ps.PS4000A_RATIO_MODE["PS4000A_RATIO_MODE_NONE"],
                0,
                ctypes.byref(overflow),
            )
        )
        n = int(sample_count.value)
        dt = actual_interval_ns * 1e-9
        times = [i * dt for i in range(n)]
        range_idx = self.RANGE_BY_VOLTS[_nearest_range_key(self.range_v, self.RANGE_BY_VOLTS)]
        channels_v = {
            channel: [mv / 1000.0 for mv in self._adc2mv(buffers[channel], range_idx, max_adc)[:n]]
            for channel in self.channels
        }
        return Capture(times, channels_v)

    def _choose_timebase(self, samples: int) -> tuple[int, float]:
        ps = self._ps
        desired_ns = 1e9 / self.sample_rate_hz
        # For ps4000a, timebase 1 gives 8 ns, 2 gives 16 ns; >=3 gives (timebase-2)*8 ns.
        first_guess = 1 if desired_ns <= 8 else 2 if desired_ns <= 16 else int(round(desired_ns / 8 + 2))
        first_guess = max(1, first_guess)
        last_error: Exception | None = None
        for timebase in range(first_guess, first_guess + 200):
            interval_ns = ctypes.c_float()
            max_samples = ctypes.c_int32()
            status = ps.ps4000aGetTimebase2(self._handle, timebase, samples, ctypes.byref(interval_ns), ctypes.byref(max_samples), 0)
            try:
                self._assert_pico_ok(status)
            except Exception as exc:  # pragma: no cover - hardware-dependent invalid timebases
                last_error = exc
                continue
            if max_samples.value >= samples:
                return timebase, float(interval_ns.value)
        raise RuntimeError(f"No valid PicoScope timebase found for {samples} samples") from last_error


def _nearest_range_key(request_v: float, range_map: dict[float, str]) -> float:
    for key in sorted(range_map):
        if request_v <= key:
            return key
    return max(range_map)


def detect_edges(times_s: Sequence[float], volts: Sequence[float], threshold_v: float = 1.5) -> list[Edge]:
    if len(times_s) != len(volts):
        raise ValueError("time and voltage arrays must have the same length")
    if not times_s:
        return []
    edges: list[Edge] = []
    prev_high = volts[0] >= threshold_v
    for idx in range(1, len(times_s)):
        high = volts[idx] >= threshold_v
        if high != prev_high:
            v0 = volts[idx - 1]
            v1 = volts[idx]
            t0 = times_s[idx - 1]
            t1 = times_s[idx]
            frac = 0.0 if v1 == v0 else (threshold_v - v0) / (v1 - v0)
            frac = min(1.0, max(0.0, frac))
            edges.append(Edge(t0 + frac * (t1 - t0), high))
        prev_high = high
    return edges


def square_wave(
    freq_hz: float,
    duration_s: float,
    sample_rate_hz: float,
    duty: float = 0.5,
    low_v: float = 0.0,
    high_v: float = 3.3,
    phase_frac: float = 0.0,
) -> tuple[list[float], list[float]]:
    samples = max(1, int(round(duration_s * sample_rate_hz)))
    times = [i / sample_rate_hz for i in range(samples)]
    volts = []
    for t in times:
        phase = (t * freq_hz + phase_frac) % 1.0
        volts.append(high_v if phase < duty else low_v)
    return times, volts


def analyze_capture(
    test_name: str,
    times_s: Sequence[float],
    channels_v: dict[str, Sequence[float]],
    input_channel: str,
    output_channel: str,
    expectation: Expectation,
    cfg: AnalysisConfig,
    commanded_output_hz: float | None = None,
    dio2_channel: str | None = None,
    commanded_ratio: float | None = None,
) -> CheckResult:
    input_channel = input_channel.upper()
    output_channel = output_channel.upper()
    if input_channel not in channels_v:
        return CheckResult(test_name, CheckStatus.FAIL, [f"missing input channel {input_channel}"])
    if output_channel not in channels_v:
        return CheckResult(test_name, CheckStatus.FAIL, [f"missing output channel {output_channel}"])

    out_v = channels_v[output_channel]
    metrics: dict[str, float | int | str] = {
        "output_min_v": min(out_v) if out_v else math.nan,
        "output_max_v": max(out_v) if out_v else math.nan,
        "output_mean_v": statistics.fmean(out_v) if out_v else math.nan,
    }

    if isinstance(expectation, ConstantExpectation):
        return _analyze_constant(test_name, out_v, expectation, cfg, metrics)

    in_edges = detect_edges(times_s, channels_v[input_channel], cfg.threshold_v)
    out_edges = detect_edges(times_s, out_v, cfg.threshold_v)
    in_rising = [e.time_s for e in in_edges if e.rising]
    out_rising = [e.time_s for e in out_edges if e.rising]

    input_hz = _frequency_from_rising_edges(in_rising)
    output_hz = _frequency_from_rising_edges(out_rising)
    # Coherent (least-squares) estimates: ~sqrt(N) tighter than the span values,
    # and the only way to resolve the in/out comparison to sub-millihertz.
    input_hz_coherent, input_hz_coherent_se = _coherent_frequency(in_rising)
    output_hz_coherent, output_hz_coherent_se = _coherent_frequency(out_rising)
    output_duty = _duty_from_samples(out_v, cfg.threshold_v)
    sample_dt = (times_s[1] - times_s[0]) if len(times_s) >= 2 else math.nan
    metrics.update({
        "input_edges": len(in_rising),
        "output_edges": len(out_rising),
        "input_hz": input_hz,
        "output_hz": output_hz,
        "output_duty": output_duty,
        # Diagnostics: the median-period estimate quantizes onto the sample
        # grid at low oversampling; keep it next to the span estimate above so
        # a large gap between them flags an under-sampled capture rather than a
        # real hardware frequency error.
        "input_hz_median": (1.0 / _median_period(in_rising)) if len(in_rising) >= 2 else math.nan,
        "output_hz_median": (1.0 / _median_period(out_rising)) if len(out_rising) >= 2 else math.nan,
        "output_samples_per_period": (1.0 / (output_hz * sample_dt)) if (math.isfinite(output_hz) and output_hz > 0 and math.isfinite(sample_dt) and sample_dt > 0) else math.nan,
        "input_hz_coherent": input_hz_coherent,
        "output_hz_coherent": output_hz_coherent,
        "input_hz_coherent_stderr": input_hz_coherent_se,
        "output_hz_coherent_stderr": output_hz_coherent_se,
        # In/out difference (scope timebase cancels): bounded below by the FPGA's
        # ~1/(2*window) frequency-quantization (~5 Hz at the 100 ms window), not
        # by the measurement, so this is a diagnostic, not the pass criterion.
        "output_minus_input_hz": (output_hz_coherent - input_hz_coherent) if (math.isfinite(output_hz_coherent) and math.isfinite(input_hz_coherent)) else math.nan,
    })

    if isinstance(expectation, OscExpectation):
        osc_result = analyze_osc_delay(in_rising, out_rising, expectation, cfg)
        metrics.update(osc_result.metrics)
        return CheckResult(test_name, osc_result.status, osc_result.messages, metrics)

    expected_hz = expectation.input_multiplier * input_hz + expectation.shift_hz
    metrics["expected_output_hz"] = expected_hz
    messages: list[str] = []
    if len(in_rising) < cfg.min_edges:
        messages.append(f"too few input rising edges: {len(in_rising)} < {cfg.min_edges}")
    if len(out_rising) < cfg.min_edges:
        messages.append(f"too few output rising edges: {len(out_rising)} < {cfg.min_edges}")
    freq_tol = max(cfg.freq_abs_tol_hz, abs(expected_hz) * cfg.freq_rel_tol, 2.0 / max(_duration(times_s), 1e-9))
    metrics["frequency_tolerance_hz"] = freq_tol
    if math.isfinite(output_hz) and abs(output_hz - expected_hz) > freq_tol:
        messages.append(f"output frequency {output_hz:.6g} Hz differs from expected {expected_hz:.6g} Hz by more than {freq_tol:.6g} Hz")
    if expectation.duty_frac is not None and math.isfinite(output_duty):
        metrics["expected_output_duty"] = expectation.duty_frac
        if abs(output_duty - expectation.duty_frac) > cfg.duty_abs_tol:
            messages.append(f"output duty {output_duty:.4f} differs from expected {expectation.duty_frac:.4f}")
    # Strict frequency-match check: the coherently-measured output frequency must
    # equal the FPGA-commanded frequency (phase_to_hz(phase_step)). Only run for
    # pulse mode (multiplier 1) and only when the FPGA register was read back.
    if expectation.input_multiplier == 1:
        messages.extend(_frequency_match_check(output_hz_coherent, output_hz_coherent_se, commanded_output_hz, cfg, metrics))
        # Clock-independent ratio check against DIO2, when that channel was captured.
        if dio2_channel is not None and dio2_channel.upper() in channels_v:
            dio2_rising = [e.time_s for e in detect_edges(times_s, channels_v[dio2_channel.upper()], cfg.threshold_v) if e.rising]
            dio2_hz_coherent, dio2_hz_coherent_se = _coherent_frequency(dio2_rising)
            messages.extend(_frequency_ratio_check(
                output_hz_coherent, output_hz_coherent_se,
                dio2_hz_coherent, dio2_hz_coherent_se,
                commanded_ratio, cfg, metrics,
            ))
    return CheckResult(test_name, CheckStatus.FAIL if messages else CheckStatus.PASS, messages, metrics)


def _analyze_constant(
    name: str,
    out_v: Sequence[float],
    expectation: ConstantExpectation,
    cfg: AnalysisConfig,
    metrics: dict[str, float | int | str],
) -> CheckResult:
    if not out_v:
        return CheckResult(name, CheckStatus.FAIL, ["empty output channel"], metrics)
    high_fraction = sum(1 for v in out_v if v >= cfg.threshold_v) / len(out_v)
    metrics["output_high_fraction"] = high_fraction
    if expectation.high:
        ok = high_fraction >= 0.98
        msg = [] if ok else [f"expected constant high, but high fraction is {high_fraction:.3f}"]
    else:
        ok = high_fraction <= 0.02
        msg = [] if ok else [f"expected constant low, but high fraction is {high_fraction:.3f}"]
    return CheckResult(name, CheckStatus.PASS if ok else CheckStatus.FAIL, msg, metrics)


def analyze_osc_delay(
    input_rising_s: Sequence[float],
    output_rising_s: Sequence[float],
    expectation: OscExpectation,
    cfg: AnalysisConfig | None = None,
) -> CheckResult:
    cfg = cfg or AnalysisConfig()
    if len(input_rising_s) < cfg.min_edges or len(output_rising_s) < cfg.min_edges:
        return CheckResult("osc_delay", CheckStatus.FAIL, ["too few edges for oscillating-delay analysis"], {
            "input_edges": len(input_rising_s),
            "output_edges": len(output_rising_s),
        })
    input_period = _median_period(input_rising_s)
    phases: list[float] = []
    output_times: list[float] = []
    i = 0
    for out_t in output_rising_s:
        while i + 1 < len(input_rising_s) and input_rising_s[i + 1] <= out_t:
            i += 1
        if i < len(input_rising_s):
            phase = ((out_t - input_rising_s[i]) / input_period) % 1.0
            phases.append(phase)
            output_times.append(out_t)
    if len(phases) < cfg.min_edges:
        return CheckResult("osc_delay", CheckStatus.FAIL, ["too few matched output/input edges"], {"matched_edges": len(phases)})

    deltas = [_wrap_signed_unit(phase - expectation.p0_frac) for phase in phases]
    center = expectation.p0_frac + statistics.fmean(deltas)
    amplitude = (max(deltas) - min(deltas)) / 2.0
    measured_rate = _estimate_osc_rate_from_delay(output_times, deltas)
    metrics: dict[str, float | int | str] = {
        "matched_edges": len(phases),
        "input_period_s": input_period,
        "delay_phase_min": min(phases),
        "delay_phase_max": max(phases),
        "delay_phase_center": center % 1.0,
        "delay_phase_amplitude": amplitude,
        "delay_osc_hz": measured_rate,
        "expected_delay_phase_center": expectation.p0_frac,
        "expected_delay_phase_amplitude": expectation.p_frac,
        "expected_delay_osc_hz": expectation.f_osc_hz,
    }
    messages: list[str] = []
    if abs(amplitude - expectation.p_frac) > cfg.osc_phase_abs_tol:
        messages.append(f"delay amplitude {amplitude:.5f} differs from expected {expectation.p_frac:.5f}")
    center_err = abs(_wrap_signed_unit((center % 1.0) - expectation.p0_frac))
    if center_err > cfg.osc_phase_abs_tol:
        messages.append(f"delay center {(center % 1.0):.5f} differs from expected {expectation.p0_frac:.5f}")
    if math.isfinite(measured_rate):
        rate_tol = max(0.5, expectation.f_osc_hz * cfg.osc_freq_rel_tol)
        metrics["delay_osc_tolerance_hz"] = rate_tol
        if abs(measured_rate - expectation.f_osc_hz) > rate_tol:
            messages.append(f"delay oscillation rate {measured_rate:.5f} Hz differs from expected {expectation.f_osc_hz:.5f} Hz")
    else:
        messages.append("could not estimate delay oscillation rate")
    return CheckResult("osc_delay", CheckStatus.FAIL if messages else CheckStatus.PASS, messages, metrics)


def _wrap_signed_unit(value: float) -> float:
    return ((value + 0.5) % 1.0) - 0.5


def _estimate_osc_rate_from_delay(times_s: Sequence[float], deltas: Sequence[float]) -> float:
    if len(times_s) < 4:
        return math.nan
    peaks: list[float] = []
    for i in range(1, len(deltas) - 1):
        if deltas[i] >= deltas[i - 1] and deltas[i] > deltas[i + 1]:
            peaks.append(times_s[i])
    if len(peaks) >= 2:
        return 1.0 / _median_period(peaks)
    # Fallback: a full triangle has two extrema per period.
    extrema: list[float] = []
    for i in range(1, len(deltas) - 1):
        if (deltas[i] >= deltas[i - 1] and deltas[i] > deltas[i + 1]) or (
            deltas[i] <= deltas[i - 1] and deltas[i] < deltas[i + 1]
        ):
            extrema.append(times_s[i])
    if len(extrema) >= 2:
        return 1.0 / (2.0 * _median_period(extrema))
    return math.nan


def _frequency_from_rising_edges(rising_edges_s: Sequence[float]) -> float:
    """Frequency from the total span of rising edges: (N-1) / (t_last - t_first).

    This is the reciprocal of the *mean* edge interval and is robust to both
    per-edge jitter and PicoScope sample-grid quantization. The previous
    1/median(instantaneous_period) estimator quantizes onto the sample grid
    when oversampling is low (e.g. ~11 samples/period for a 259 kHz signal at
    a 2.86 MHz effective sample rate), biasing a clean external input and an
    NCO-generated output by different amounts and producing false frequency
    mismatches even when the two signals carry an identical number of edges.
    """
    if len(rising_edges_s) < 2:
        return math.nan
    span = rising_edges_s[-1] - rising_edges_s[0]
    if span <= 0:
        return math.nan
    return (len(rising_edges_s) - 1) / span


def _coherent_frequency(rising_edges_s: Sequence[float]) -> tuple[float, float]:
    """Least-squares frequency from a regular rising-edge train.

    Fits ``t_k = t0 + period * k`` over *all* edges (the integer index ``k`` is
    exact; only the edge time carries noise) and returns ``(freq_hz, stderr_hz)``.
    Because it uses every edge rather than only the first and last, its standard
    error shrinks as ~N^1.5 instead of the span estimator's ~N, which is what
    makes a sub-millihertz in-vs-out comparison possible.

    Returns ``(nan, nan)`` when the train has a gap or a doubled edge (any
    interval outside 0.5x..1.5x the median): a missed or spurious edge breaks
    the integer index assignment, so the high-precision fit cannot be trusted.
    The caller falls back to the span estimator and skips the strict check.
    """
    n = len(rising_edges_s)
    if n < 3:
        return math.nan, math.nan
    intervals = [b - a for a, b in zip(rising_edges_s, rising_edges_s[1:])]
    med = statistics.median(intervals)
    if med <= 0:
        return math.nan, math.nan
    if any(d < 0.5 * med or d > 1.5 * med for d in intervals):
        return math.nan, math.nan
    mean_k = (n - 1) / 2.0
    s_xx = n * (n * n - 1) / 12.0
    mean_t = statistics.fmean(rising_edges_s)
    s_xt = math.fsum((k - mean_k) * (t - mean_t) for k, t in enumerate(rising_edges_s))
    if s_xx <= 0:
        return math.nan, math.nan
    period = s_xt / s_xx
    if period <= 0:
        return math.nan, math.nan
    freq = 1.0 / period
    intercept = mean_t - period * mean_k
    residuals = [t - (intercept + period * k) for k, t in enumerate(rising_edges_s)]
    resid_var = math.fsum(r * r for r in residuals) / (n - 2)
    slope_stderr = math.sqrt(resid_var / s_xx) if resid_var > 0 else 0.0
    freq_stderr = freq * freq * slope_stderr
    return freq, freq_stderr


def _frequency_match_check(
    output_hz: float,
    output_stderr_hz: float,
    commanded_output_hz: float | None,
    cfg: AnalysisConfig,
    metrics: dict[str, float | int | str],
) -> list[str]:
    """Strict check that the measured output equals the FPGA-commanded frequency.

    ``commanded_output_hz`` is ``phase_to_hz(phase_step)`` read back from the
    settled FPGA registers, i.e. the frequency the divider+NCO datapath intends
    to emit (measured input base, scaled, plus the exact shift). The coherent
    estimator resolves the measured output to well under 1 mHz, but the *pass*
    tolerance also carries a ppm term because the PicoScope and Red Pitaya run on
    independent clocks (so an absolute comparison cannot be tighter than that
    clock mismatch). The check engages only when the coherent standard error is
    small enough to resolve the statistical floor; otherwise it reports the
    error without failing, so an under-resolved capture never false-fails.
    """
    messages: list[str] = []
    if commanded_output_hz is None or not (math.isfinite(commanded_output_hz) and commanded_output_hz > 0):
        metrics["freq_match_resolved"] = 0
        return messages
    metrics["commanded_output_hz"] = commanded_output_hz
    if not math.isfinite(output_hz):
        metrics["freq_match_resolved"] = 0
        return messages

    err = output_hz - commanded_output_hz
    metrics["output_freq_error_hz"] = err
    match_tol = cfg.freq_match_abs_tol_hz + abs(commanded_output_hz) * cfg.freq_match_timebase_rel_tol
    metrics["freq_match_tolerance_hz"] = match_tol

    if not math.isfinite(output_stderr_hz) or output_stderr_hz > match_tol / 3.0:
        # Coherent estimate too uncertain to trust the comparison against the
        # tolerance; report the error but do not fail on an under-resolved capture.
        metrics["freq_match_resolved"] = 0
        return messages
    metrics["freq_match_resolved"] = 1
    if abs(err) > match_tol:
        messages.append(
            f"output frequency {output_hz:.6f} Hz differs from FPGA-commanded "
            f"{commanded_output_hz:.6f} Hz by more than {match_tol:.6f} Hz"
        )
    return messages


def _frequency_ratio_check(
    output_hz: float,
    output_stderr_hz: float,
    dio2_hz: float,
    dio2_stderr_hz: float,
    commanded_ratio: float | None,
    cfg: AnalysisConfig,
    metrics: dict[str, float | int | str],
) -> list[str]:
    """Clock-independent check: measured f_out / f_DIO2 == phase_step / trig_phase_step.

    DIO1 (output) and DIO2 are both NCOs on the Red Pitaya clock, so their
    *register* ratio (phase_step / trig_phase_step) is exact and clock-free.
    They are captured by the same PicoScope, so their *measured* ratio is also
    clock-free. The two must agree to the coherent statistical floor — there is
    no ppm timebase term here, which is what makes a genuine sub-millihertz
    datapath verification possible. ``commanded_ratio`` is phase_step /
    trig_phase_step from the settled FPGA registers.
    """
    messages: list[str] = []
    if commanded_ratio is None or not (math.isfinite(commanded_ratio) and commanded_ratio > 0):
        metrics["ratio_match_resolved"] = 0
        return messages
    if not (math.isfinite(output_hz) and math.isfinite(dio2_hz) and dio2_hz > 0):
        metrics["ratio_match_resolved"] = 0
        return messages
    expected_output_hz = commanded_ratio * dio2_hz
    err = output_hz - expected_output_hz
    metrics["dio2_hz_coherent"] = dio2_hz
    metrics["dio2_hz_coherent_stderr"] = dio2_stderr_hz
    metrics["commanded_out_over_dio2_ratio"] = commanded_ratio
    metrics["ratio_expected_output_hz"] = expected_output_hz
    metrics["ratio_output_freq_error_hz"] = err
    # The DIO2 error is scaled up by the ratio when projected onto the output.
    combined_se = math.hypot(output_stderr_hz, commanded_ratio * dio2_stderr_hz)
    metrics["ratio_output_stderr_hz"] = combined_se
    tol = cfg.freq_match_abs_tol_hz
    metrics["ratio_match_tolerance_hz"] = tol
    if not math.isfinite(combined_se) or combined_se > tol / 3.0:
        # Capture cannot resolve the floor (e.g. DIO2 too low, so its error is
        # amplified, or the span is too short). Report but do not fail.
        metrics["ratio_match_resolved"] = 0
        return messages
    metrics["ratio_match_resolved"] = 1
    if abs(err) > tol:
        messages.append(
            f"output/DIO2 ratio implies output {expected_output_hz:.6f} Hz but measured "
            f"{output_hz:.6f} Hz (>{tol:.6f} Hz, clock-independent)"
        )
    return messages


def _median_period(times_s: Sequence[float]) -> float:
    periods = [b - a for a, b in zip(times_s, times_s[1:]) if b > a]
    if not periods:
        return math.nan
    return statistics.median(periods)


def _duty_from_samples(volts: Sequence[float], threshold_v: float) -> float:
    if not volts:
        return math.nan
    return sum(1 for v in volts if v >= threshold_v) / len(volts)


def _duration(times_s: Sequence[float]) -> float:
    if len(times_s) < 2:
        return 0.0
    return times_s[-1] - times_s[0]


def build_default_suite(include_dio2: bool = False, dio2_hz: float = 1_000.0) -> list[HardwareTest]:
    tests = [
        HardwareTest("off_low", "off", ConstantExpectation(False), capture_seconds=0.02, settle_seconds=0.10),
        HardwareTest("force_high", "force_high", ConstantExpectation(True), capture_seconds=0.02, settle_seconds=0.10),
        # Pulse-mode shift is normally < 20 Hz. The clock-independent f_out/f_DIO2
        # ratio check resolves ~1 mHz, but only over a long edge train, so these
        # captures are 1.0 s.
        HardwareTest("pulse_identity_50pct", "pulse", PulseExpectation(1, 0.0, 0.50), capture_seconds=1.00, shift_hz=0.0, duty_frac=0.50),
        HardwareTest("pulse_plus_5hz_25pct", "pulse", PulseExpectation(1, 5.0, 0.25), capture_seconds=1.00, shift_hz=5.0, duty_frac=0.25),
        HardwareTest("pulse_plus_20hz_50pct", "pulse", PulseExpectation(1, 20.0, 0.50), capture_seconds=1.00, shift_hz=20.0, duty_frac=0.50),
        HardwareTest("harmonic_2x", "harmonic", PulseExpectation(2, 0.0, 0.50), capture_seconds=0.10, harmonic_n=2),
        HardwareTest("harmonic_3x_plus_10hz", "harmonic", PulseExpectation(3, 10.0, 0.50), capture_seconds=0.20, shift_hz=10.0, harmonic_n=3),
        HardwareTest(
            "osc_delay_5hz_p5pct_p0_25pct",
            "osc",
            OscExpectation(f_osc_hz=5.0, p_frac=0.05, p0_frac=0.25),
            capture_seconds=0.60,
            f_osc_hz=5.0,
            p_frac=0.05,
            p0_frac=0.25,
            duty_frac=0.02,
        ),
    ]
    if include_dio2:
        tests.append(HardwareTest("dio2_square", "dio2", PulseExpectation(0, dio2_hz, 0.50), capture_seconds=0.05, shift_hz=dio2_hz))
    return tests


def estimate_input_hz(rp: RedPitayaSSH, builder: RedPitayaCommandBuilder, settle_s: float) -> float:
    rp.run(builder.pulse_write(width_cycles=1, shift_hz=0.0))
    time.sleep(settle_s)
    status = rp.run(builder.read(harmonic=False))
    step_base = int(status.get("phase_step_base") or 0)
    hz = phase_to_hz(step_base)
    if hz <= 0:
        raise RuntimeError(f"Red Pitaya did not report a valid input frequency: {status}")
    return hz


def configure_test(
    rp: RedPitayaSSH,
    builder: RedPitayaCommandBuilder,
    test: HardwareTest,
    input_hz: float,
) -> dict[str, Any]:
    period_cycles = max(2, int(round(CLK_HZ / input_hz)))
    width_cycles = duty_to_cycles(test.duty_frac, period_cycles)
    if test.mode == "off":
        return rp.run(builder.pulse_control(0))
    if test.mode == "force_high":
        return rp.run(builder.pulse_control(CTRL_FORCE_HIGH))
    if test.mode == "pulse":
        return rp.run(builder.pulse_write(width_cycles, test.shift_hz))
    if test.mode == "harmonic":
        return rp.run(builder.harmonic_write(test.harmonic_n, test.shift_hz))
    if test.mode == "osc":
        shift_hz = f_shift_from_f_osc(test.f_osc_hz, test.p_frac)
        half_period = osc_half_period_cycles(test.p_frac, shift_hz)
        preload = osc_phase_preload(test.p0_frac, test.p_frac)
        status: dict[str, Any] = {}
        for command in builder.osc_apply(width_cycles, half_period, preload, shift_hz):
            status = rp.run(command)
        return status
    if test.mode == "dio2":
        return rp.run(builder.trig(test.shift_hz))
    raise ValueError(f"unknown test mode: {test.mode}")


def run_hardware_suite(args: argparse.Namespace) -> Path:
    channels = [args.input_channel.upper(), args.output_channel.upper()]
    if args.dio2_channel:
        channels.append(args.dio2_channel.upper())
    channels = sorted(set(channels))
    cfg = AnalysisConfig(
        threshold_v=args.threshold_v,
        freq_rel_tol=args.freq_rel_tol,
        freq_abs_tol_hz=args.freq_abs_tol_hz,
        duty_abs_tol=args.duty_abs_tol,
        osc_phase_abs_tol=args.osc_phase_abs_tol,
        freq_match_abs_tol_hz=args.freq_match_abs_tol_hz,
        freq_match_timebase_rel_tol=args.freq_match_timebase_rel_tol,
    )
    builder = RedPitayaCommandBuilder(args.base_addr)
    results: list[CheckResult] = []
    captures: dict[str, Capture] = {}
    redpitaya_status: dict[str, Any] = {}

    with RedPitayaSSH(args.host, args.user, args.port, args.key, args.password) as rp, Pico4000aScope(channels, args.sample_rate_hz, args.range_v) as scope:
        rp.run(builder.window(args.window_us, harmonic=False))
        rp.run(builder.window(args.window_us, harmonic=True))
        input_hz = args.input_hz or estimate_input_hz(rp, builder, args.settle_s)
        # DIO2 free-runs as the clock-independent reference for the f_out/f_DIO2
        # ratio check. Default it near the input frequency so the ratio is ~1 and
        # the DIO2 measurement error is not amplified when projected onto f_out.
        dio2_ref_hz = args.dio2_hz if args.dio2_hz else round(input_hz)
        if args.dio2_channel:
            rp.run(builder.trig(dio2_ref_hz))
        tests = build_default_suite(include_dio2=bool(args.dio2_channel), dio2_hz=dio2_ref_hz)
        for test in tests:
            status = configure_test(rp, builder, test, input_hz)
            redpitaya_status[test.name] = status
            time.sleep(test.settle_seconds if test.settle_seconds is not None else args.settle_s)
            # Read the settled registers so the strict pulse-mode check can
            # compare the measured output against the FPGA-commanded frequency.
            # (The write-time status is read before the measurement window
            # completes, so phase_step is not yet valid there.)
            commanded_output_hz: float | None = None
            commanded_ratio: float | None = None
            if test.mode == "pulse":
                settled = rp.run(builder.read(harmonic=False))
                redpitaya_status[f"{test.name}_settled"] = settled
                phase_step = int(settled.get("phase_step") or 0)
                if phase_step:
                    commanded_output_hz = phase_to_hz(phase_step)
                trig_phase_step = int(settled.get("trig_phase_step") or 0)
                if phase_step and trig_phase_step:
                    commanded_ratio = phase_step / trig_phase_step
            capture = scope.capture(test.capture_seconds)
            captures[test.name] = capture
            if test.mode == "dio2" and args.dio2_channel:
                result = analyze_capture(
                    test.name,
                    capture.times_s,
                    capture.channels_v,
                    input_channel=args.input_channel,
                    output_channel=args.dio2_channel,
                    expectation=test.expectation,
                    cfg=cfg,
                )
            else:
                result = analyze_capture(
                    test.name,
                    capture.times_s,
                    capture.channels_v,
                    input_channel=args.input_channel,
                    output_channel=args.output_channel,
                    expectation=test.expectation,
                    cfg=cfg,
                    commanded_output_hz=commanded_output_hz,
                    dio2_channel=args.dio2_channel,
                    commanded_ratio=commanded_ratio,
                )
            results.append(result)
            print(f"{result.status.value:4s} {test.name}: {'; '.join(result.messages) if result.messages else 'ok'}")
        rp.run(builder.pulse_control(0))
        if args.dio2_channel:
            rp.run(builder.trig(0.0))

    return write_debug_bundle(
        output_dir=args.output_dir,
        board=args.host,
        results=results,
        captures=captures,
        metadata={
            "sample_rate_hz": args.sample_rate_hz,
            "range_v": args.range_v,
            "threshold_v": args.threshold_v,
            "input_channel": args.input_channel.upper(),
            "output_channel": args.output_channel.upper(),
            "dio2_channel": args.dio2_channel.upper() if args.dio2_channel else None,
            "redpitaya_status": redpitaya_status,
        },
    )


def write_debug_bundle(
    output_dir: str | Path,
    board: str,
    results: Sequence[CheckResult],
    captures: dict[str, Capture],
    metadata: dict[str, Any],
) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    bundle = root / time.strftime("redpitaya_picosdk_%Y%m%d_%H%M%S")
    bundle.mkdir()
    summary = {
        "board": board,
        "created_local_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "metadata": metadata,
        "results": [result.to_jsonable() for result in results],
    }
    (bundle / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (bundle / "README.md").write_text(_bundle_readme(board, results), encoding="utf-8")
    capture_dir = bundle / "captures"
    capture_dir.mkdir()
    for name, capture in captures.items():
        _write_capture_csv(capture_dir / f"{name}.csv", capture)
    return bundle


def _write_capture_csv(path: Path, capture: Capture) -> None:
    channels = sorted(capture.channels_v)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", *[f"{ch}_v" for ch in channels]])
        for idx, t in enumerate(capture.times_s):
            writer.writerow([t, *[capture.channels_v[ch][idx] for ch in channels]])


def _bundle_readme(board: str, results: Sequence[CheckResult]) -> str:
    lines = [
        f"# Red Pitaya PicoSDK debug bundle for `{board}`",
        "",
        "Send this whole folder when asking for debugging help.",
        "",
        "## Summary",
        "",
    ]
    if not results:
        lines.append("No hardware tests were run in this bundle.")
    for result in results:
        msg = "; ".join(result.messages) if result.messages else "ok"
        lines.append(f"- `{result.status.value}` `{result.name}` — {msg}")
    lines.extend([
        "",
        "## Files",
        "",
        "- `summary.json`: machine-readable configuration, Red Pitaya register JSON, and test metrics.",
        "- `captures/*.csv`: captured PicoScope waveforms in volts.",
    ])
    return "\n".join(lines) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify Red Pitaya TTL generator modes with a PicoScope 4000A via PicoSDK.")
    parser.add_argument("--host", required=True, help="Red Pitaya hostname or IP, e.g. rp-xxxxxx.local")
    parser.add_argument("--user", default="root", help="SSH username")
    parser.add_argument("--port", type=int, default=22, help="SSH port")
    parser.add_argument("--key", default=None, help="SSH private key path")
    parser.add_argument("--password", default=None, help="SSH password, if keyless login is not configured")
    parser.add_argument("--base-addr", type=lambda s: int(s, 0), default=DEFAULT_BASE, help="AXI base address")
    parser.add_argument("--driver", choices=["ps4000a"], default="ps4000a", help="PicoSDK driver backend")
    parser.add_argument("--input-channel", default="A", choices=["A", "B", "C", "D"], help="PicoScope channel connected to DIO0_P input")
    parser.add_argument("--output-channel", default="B", choices=["A", "B", "C", "D"], help="PicoScope channel connected to DIO1_P output")
    parser.add_argument("--dio2-channel", default=None, choices=["A", "B", "C", "D"], help="Optional PicoScope channel connected to DIO2_P")
    parser.add_argument("--dio2-hz", type=float, default=0.0, help="DIO2 reference frequency when --dio2-channel is set (0 = auto: match the input frequency so the f_out/f_DIO2 ratio is ~1)")
    parser.add_argument("--sample-rate-hz", type=float, default=5_000_000.0, help="Requested PicoScope sample rate")
    parser.add_argument("--range-v", type=float, default=5.0, help="PicoScope input range in volts")
    parser.add_argument("--threshold-v", type=float, default=1.5, help="TTL threshold used for edge detection")
    parser.add_argument("--input-hz", type=float, default=None, help="Known input frequency. If omitted, the Red Pitaya measurement is used.")
    parser.add_argument("--window-us", type=int, default=100_000, help="Red Pitaya input measurement window")
    parser.add_argument("--settle-s", type=float, default=0.25, help="Default settling delay after configuration writes")
    parser.add_argument("--freq-rel-tol", type=float, default=0.002, help="Relative output-frequency tolerance")
    parser.add_argument("--freq-abs-tol-hz", type=float, default=2.0, help="Absolute output-frequency tolerance")
    parser.add_argument("--duty-abs-tol", type=float, default=0.05, help="Absolute duty-cycle tolerance")
    parser.add_argument("--osc-phase-abs-tol", type=float, default=0.025, help="Absolute oscillating-delay phase tolerance")
    parser.add_argument("--freq-match-abs-tol-hz", type=float, default=0.001, help="Statistical-resolution floor for the pulse-mode output-vs-commanded frequency check")
    parser.add_argument("--freq-match-timebase-rel-tol", type=float, default=1e-4, help="Scope/Red-Pitaya clock-mismatch allowance (relative) added to the frequency-match tolerance")
    parser.add_argument("--output-dir", default="hardware_test_results", help="Directory for generated debug bundles")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.driver != "ps4000a":
        parser.error("only ps4000a is implemented in this version")
    bundle = run_hardware_suite(args)
    print(f"\nDebug bundle written to: {bundle}")
    summary = json.loads((bundle / "summary.json").read_text(encoding="utf-8"))
    failed = [r for r in summary["results"] if r["status"] == CheckStatus.FAIL.value]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
