#!/usr/bin/env python3
"""
rp_math.py — Pure hardware constants and frequency/duty math for the Red Pitaya
NCO control GUIs.

This module has **no Qt or paramiko dependency** so the conversion math can be
imported and unit-tested in isolation (see tests/test_rp_math.py). The GUI
imports everything it needs from here.
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Hardware constants
# ─────────────────────────────────────────────────────────────────────────────
CLK_HZ        = 124_999_999      # measured FPGA clock (not nominal 125 MHz)
PHASE_BITS    = 48
DEFAULT_BASE  = 0x40600000

# control register bits
CTRL_ENABLE     = 0x01   # bit 0 — enable output + NCO
CTRL_FORCE_HIGH = 0x04   # bit 2 — force output HIGH (constant 1)
# bit 3 (harmonic_mode) is enforced by the respective C helper

_PHASE_MAX    = 2 ** (PHASE_BITS - 1)
PHASE_RES_HZ  = CLK_HZ / 2**PHASE_BITS
MAX_SHIFT_HZ  = (_PHASE_MAX - 1) * CLK_HZ / 2**PHASE_BITS

WINDOW_OPTIONS_US = [1_000, 10_000, 100_000, 500_000, 1_000_000]
WINDOW_NAMES      = ["1 ms", "10 ms", "100 ms", "500 ms", "1000 ms"]

# ─────────────────────────────────────────────────────────────────────────────
# Math helpers
# ─────────────────────────────────────────────────────────────────────────────

def hz_to_phase(delta_hz: float) -> int:
    v = int(round(delta_hz * 2**PHASE_BITS / CLK_HZ))
    return max(-_PHASE_MAX, min(_PHASE_MAX - 1, v))


def phase_to_hz(word: int) -> float:
    return word * CLK_HZ / 2**PHASE_BITS


def duty_to_cycles(frac: float, period: int) -> int:
    return max(1, min(period - 1, int(round(frac * period))))


def fmt_freq(hz: float) -> str:
    if hz <= 0:
        return "---"
    if hz < 1e3:
        return f"{hz:.6f} Hz"
    if hz < 1e6:
        return f"{hz / 1e3:.6f} kHz"
    return f"{hz / 1e6:.6f} MHz"


def fmt_signed_freq(hz: float) -> str:
    if abs(hz) < PHASE_RES_HZ / 2:
        return "+0.000000 Hz"
    sign = "+" if hz >= 0 else "-"
    return f"{sign}{fmt_freq(abs(hz))}"


def suggest_window(f_shift_hz: float) -> int:
    if f_shift_hz <= 0:
        return 2
    if f_shift_hz < 1:
        return 4
    if f_shift_hz < 10:
        return 3
    if f_shift_hz < 100:
        return 2
    if f_shift_hz < 1000:
        return 1
    return 0


def trig_hz_to_half_period(f_hz: float) -> int:
    if f_hz <= 0:
        return 0
    return round(CLK_HZ / (2.0 * f_hz))


def fmt_dur(s: float) -> str:
    if s <= 0:
        return "---"
    if s < 1e-6:
        return f"{s * 1e9:.3f} ns"
    if s < 1e-3:
        return f"{s * 1e6:.3f} µs"
    if s < 1.0:
        return f"{s * 1e3:.3f} ms"
    return f"{s:.6f} s"
