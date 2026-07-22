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
CTRL_OSC_MODE   = 0x10   # bit 4 — stepped strobe scan
CTRL_EDGE_LOCK  = 0x20   # bit 5 — anchor NCO phase to input edges
CTRL_EDGE_RESPONSE_MASK     = 0xC0  # bits 7:6 — edge-lock response
CTRL_EDGE_RESPONSE_HARD     = 0x00
CTRL_EDGE_RESPONSE_FAST     = 0x40
CTRL_EDGE_RESPONSE_BALANCED = 0x80
CTRL_EDGE_RESPONSE_SMOOTH   = 0xC0
DEFAULT_EDGE_LOCK_RESPONSE  = CTRL_EDGE_RESPONSE_BALANCED
EDGE_LOCK_RESPONSES = (
    CTRL_EDGE_RESPONSE_HARD,
    CTRL_EDGE_RESPONSE_FAST,
    CTRL_EDGE_RESPONSE_BALANCED,
    CTRL_EDGE_RESPONSE_SMOOTH,
)
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
# Stepped strobe mode helpers
# ─────────────────────────────────────────────────────────────────────────────

def strobe_step_word(step_frac: float) -> int:
    """Signed per-step increment for osc_target (phase_step_offset register).

    Delay φ maps to target word (1 − φ)·2^48, so advancing the delay by
    ``step_frac`` of the input period means *subtracting* round(step_frac·2^48)
    from the target — the FPGA adds this word mod 2^48 at each dwell boundary.
    Start phase itself comes from :func:`phase_offset_to_preload`.
    """
    return -int(round((step_frac % 1.0) * 2**PHASE_BITS))


def dwell_s_to_cycles(dwell_s: float) -> int:
    """Dwell time per strobe point → 32-bit clock-tick count.

    Clamped to [1, 2^32 − 1]: ceiling ≈ 34.4 s/point at 125 MHz.
    """
    return max(1, min(2**32 - 1, int(round(dwell_s * CLK_HZ))))


# ─────────────────────────────────────────────────────────────────────────────
# Edge-locked phase offset (pulse mode)
# ─────────────────────────────────────────────────────────────────────────────

def phase_offset_to_preload(offset_turns: float) -> int:
    """48-bit accumulator preload for a constant edge-lock phase offset.

    In edge-locked pulse mode the output pulse fires one NCO overflow after each
    anchored input rising edge. Seeding phase_acc with this preload makes that
    pulse lag the input edge by ``offset_turns`` of one output period. Because
    the pulse fires on the carry out of 2^48, the preload is the complement of
    the delay: (1 − offset_turns) mod 1, scaled onto the 48-bit NCO grid.

    offset_turns is a fraction of one output period (turns); 0.25 == 90°.
    Reduces mod 1, so any real value is accepted.
    """
    word = int(round((1.0 - offset_turns) % 1.0 * 2**PHASE_BITS))
    return word & (2**PHASE_BITS - 1)


def preload_to_phase_offset(word: int) -> float:
    """Inverse of :func:`phase_offset_to_preload`: preload word → offset turns.

    Returns the phase offset in turns, in the half-open interval [0, 1).
    """
    return (1.0 - (word & (2**PHASE_BITS - 1)) / 2**PHASE_BITS) % 1.0


def harmonic_phase_offset_to_preload(offset_turns: float) -> int:
    """48-bit accumulator preload for a constant edge-lock phase offset in
    *harmonic* mode.

    The harmonic output is the accumulator MSB (a 50%-duty square wave), so its
    rising edge occurs when phase_acc crosses 2^47 — not the 2^48 wrap the pulse
    carry uses. Seeding phase_acc with (0.5 − offset_turns) mod 1 makes the
    output rising edge lag each anchored input edge by ``offset_turns`` of one
    output period; offset 0 (preload = 2^47) aligns them.
    """
    word = int(round((0.5 - offset_turns) % 1.0 * 2**PHASE_BITS))
    return word & (2**PHASE_BITS - 1)


def harmonic_preload_to_phase_offset(word: int) -> float:
    """Inverse of :func:`harmonic_phase_offset_to_preload` → offset turns [0, 1)."""
    return (0.5 - (word & (2**PHASE_BITS - 1)) / 2**PHASE_BITS) % 1.0
