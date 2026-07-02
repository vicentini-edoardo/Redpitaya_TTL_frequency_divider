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
CTRL_OSC_MODE   = 0x10   # bit 4 — oscillating delay mode
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


def measured_edges_to_phase_step(edge_count: int, span_cycles: int) -> int:
    """phase_step_base from a true reciprocal measurement: edge_count rising
    edges enclose edge_count - 1 whole input periods over span_cycles clock
    cycles (first→last rising edge). Mirrors the pulse_gen.sv divider."""
    if edge_count < 3 or span_cycles <= 0:
        return 0
    return ((edge_count - 1) << PHASE_BITS) // span_cycles


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


def trig_hz_to_phase_step(f_hz: float) -> int:
    if f_hz <= 0:
        return 0
    return hz_to_phase(f_hz)


def trig_phase_step_to_hz(step: int) -> float:
    if step <= 0:
        return 0.0
    return phase_to_hz(step)


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


# ─────────────────────────────────────────────────────────────────────────────
# Oscillating delay mode helpers
# ─────────────────────────────────────────────────────────────────────────────

def f_shift_from_f_osc(f_osc_hz: float, P_frac: float) -> float:
    """NCO frequency shift derived from oscillation rate and phase amplitude."""
    return 4.0 * f_osc_hz * P_frac


def f_osc_from_params(f_shift_hz: float, P_frac: float) -> float:
    """Oscillation frequency from NCO f_shift and phase amplitude."""
    return f_shift_hz / (4.0 * P_frac) if P_frac > 0 else 0.0


def osc_half_period_cycles(P_frac: float, f_shift_hz: float) -> int:
    """Clock ticks per half-oscillation (sweeps 2·P of phase at f_shift rate)."""
    if f_shift_hz <= 0:
        return 0
    return round(2 * P_frac * CLK_HZ / f_shift_hz)


def osc_phase_preload(P0_frac: float, P_frac: float) -> int:
    """48-bit accumulator preload so the first output pulse has delay = P0 − P."""
    return int((1.0 - (P0_frac - P_frac)) % 1.0 * 2**PHASE_BITS)
