"""
Oscillating Delay Mode — NCO simulation & verification.

Tick-accurate model of the pulse_gen.sv oscillating-delay logic:
  - accumulator += step_base + sign * step_offset   (frequency flip)
  - sign flips every osc_half_period_cycles ticks
  - osc_target integrates only the sign * step_offset (triangle) part
  - EDGE-LOCKED (corrected RTL): on every input rising edge the
    accumulator snaps to osc_target, anchoring the pulse delay to the
    physical input edge
  - legacy (open-loop) mode reproduces the original RTL for comparison

Hardware non-idealities modelled (both were missing from the original
idealized sim, which is why the mode "looked right" here but not on the
oscilloscope):
  - the mode is enabled at an arbitrary instant relative to the input
    edges (start_frac), so the legacy open-loop triangle is centred at a
    random phase instead of P0
  - step_base comes from the reciprocal counter,
    (edge_cnt - 1) << 48 // (2 * window_cycles), quantized to ±1 edge per
    window → frequency error up to f_clk / (2 * window_cycles) Hz, which
    makes the legacy open-loop phase drift continuously

Usage:
    python3 osc_delay_sim.py
"""

import math
import sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# ── Hardware constants ─────────────────────────────────────────────────────────
CLK_HZ     = 124_999_999
PHASE_BITS = 48
PHASE_WRAP = 2**PHASE_BITS

WINDOW_100MS = 12_500_000     # 100 ms measurement window in clock cycles

# ── Conversion helpers ─────────────────────────────────────────────────────────

def hz_to_phase_step(f_hz: float) -> int:
    return round(f_hz * PHASE_WRAP / CLK_HZ)

def fraction_to_acc(frac: float) -> int:
    """Fraction of T_in [0,1) → 48-bit accumulator preload word."""
    return int(frac * PHASE_WRAP) & (PHASE_WRAP - 1)

def osc_half_period_cycles(P_frac: float, f_shift_hz: float) -> int:
    """
    Ticks per half-oscillation.

    One half-period sweeps 2*P_frac of T_in (from extreme P0-P to P0+P).
    Phase accumulates at f_shift_hz cycles/s.
    Time for 2*P_frac cycles = 2*P_frac / f_shift_hz seconds.
    """
    return round(2 * P_frac * CLK_HZ / f_shift_hz)

def measured_step_base(f_in_hz: float, window_cycles: int) -> int:
    """
    Reciprocal-counter model (pulse_gen.sv): the window opens on an edge,
    both rising and falling edges are counted, and
    phase_step_base = (edge_cnt - 1) << 48 // (2 * window_cycles).
    The result is quantized to ±1 edge per window → frequency error up to
    f_clk / (2 * window_cycles) Hz.
    """
    edges_after_open = int(2.0 * f_in_hz * window_cycles / CLK_HZ)
    edge_cnt = 1 + edges_after_open
    return ((edge_cnt - 1) << PHASE_BITS) // (2 * window_cycles)

# ── Limit checker ──────────────────────────────────────────────────────────────

def check_limits(f_in_hz, f_shift_hz, P0_frac, P_frac):
    issues, warnings = [], []
    if f_shift_hz <= 0:
        issues.append("f_shift must be > 0")
    if f_shift_hz >= f_in_hz:
        issues.append(f"f_shift ({f_shift_hz:.1f} Hz) >= f_in ({f_in_hz:.1f} Hz): negative frequency impossible")
    if P_frac <= 0:
        issues.append("P must be > 0")
    if P_frac >= 0.5:
        issues.append(f"P={P_frac*100:.1f}% >= 50%: aliasing — output more than half-period offset from f_in")
    if not (0 <= P0_frac < 1.0):
        issues.append("P0 must be in [0, 1)")
    if P0_frac + P_frac > 0.5 or P0_frac - P_frac < -0.5:
        warnings.append(f"P0±P extends beyond ±50% of T_in: phase wrap may look discontinuous in plots")

    if f_shift_hz > 0 and P_frac > 0 and not issues:
        half_period  = osc_half_period_cycles(P_frac, f_shift_hz)
        T_in_clks    = CLK_HZ / f_in_hz
        pulses_half  = half_period / T_in_clks
        min_P_frac   = 2.0 / T_in_clks          # must be > 2 ticks for any resolution
        if P_frac < min_P_frac:
            issues.append(
                f"P={P_frac*100:.3f}% < {min_P_frac*100:.3f}% (= 2 clock ticks at f_in={f_in_hz:.0f} Hz): "
                "delay swing smaller than NCO step — unresolvable"
            )
        if pulses_half < 5:
            warnings.append(f"Only {pulses_half:.1f} pulses per half-oscillation (minimum ~5). "
                            "Reduce f_shift or increase P.")
        f_osc_hz = f_shift_hz / (4 * P_frac)
        warnings.append(
            f"Oscillation rate: {f_osc_hz:.3f} Hz  "
            f"(half-period = {half_period} clks = {half_period/CLK_HZ*1e3:.3f} ms)  "
            f"NCO resolution: {100/T_in_clks:.3f}% per tick"
        )
    return issues, warnings

# ── Core NCO simulation ────────────────────────────────────────────────────────

def simulate_osc_nco(
    f_in_hz:      float,
    f_shift_hz:   float,
    P0_frac:      float,  # centre phase [0,1) as fraction of T_in
    P_frac:       float,  # half-amplitude [0,0.5) as fraction of T_in
    n_osc_cycles: int = 8,
    *,
    edge_locked:   bool = True,
    window_cycles: int | None = WINDOW_100MS,
    start_frac:    float = 0.0,
) -> dict:
    """
    Tick-accurate NCO simulation of pulse_gen.sv osc mode.

    edge_locked=True  → corrected RTL: phase_acc snaps to osc_target on
                        every input rising edge (delay anchored to input).
    edge_locked=False → legacy RTL: open-loop dead reckoning from preload.
    window_cycles     → reciprocal-counter measurement window; the NCO base
                        step is the quantized measured value. None = exact
                        base step (the original idealized assumption).
    start_frac        → where inside the input period the mode is enabled
                        (hardware: arbitrary; 0 = enable coincides with an
                        input edge, the original idealized assumption).

    Relative phase of a pulse is measured against the *input edge grid*
    (what an oscilloscope triggered on the input shows) and should follow
    a triangle wave between P0-P and P0+P.
    """
    step_base = (measured_step_base(f_in_hz, window_cycles)
                 if window_cycles else hz_to_phase_step(f_in_hz))
    step_offset = hz_to_phase_step(f_shift_hz)
    half_period = osc_half_period_cycles(P_frac, f_shift_hz)
    T_in_clks   = CLK_HZ / f_in_hz

    total_ticks = n_osc_cycles * 2 * half_period + half_period // 2

    # Preload: first pulse arrives with delay P0-P (minimum of triangle wave).
    # φ_start = P0-P  →  acc = (1 - (P0-P)) % 1.0 * PHASE_WRAP
    # (high acc = close to next carry = early pulse = low delay lag)
    # sign=-1: f_out = f_in - f_shift (slower) → delay φ increases P0-P → P0+P
    acc     = int((1.0 - (P0_frac - P_frac)) % 1.0 * PHASE_WRAP)
    target  = acc                                  # osc_target (corrected RTL)
    sign    = -1                                   # first sweep: φ increases toward P0+P
    counter = 0
    armed   = edge_locked  # corrected RTL: NCO held until first anchoring edge

    # Input rising edges: tick t is an edge when (t/T_in + start_frac) ≡ 0 (mod 1)
    next_edge_f   = ((1.0 - start_frac) % 1.0) * T_in_clks
    next_edge_int = round(next_edge_f)

    pulse_ticks  = []
    flip_ticks   = []

    for tick in range(total_ticks):
        if armed:
            # hold the preload until the first input rising edge so the
            # triangle wave starts anchored to the input (osc_run in RTL)
            if tick >= next_edge_int:
                armed = False
                next_edge_f  += T_in_clks
                next_edge_int = round(next_edge_f)
            continue

        # sign flip
        if counter >= half_period:
            counter = 0
            sign    = -sign
            flip_ticks.append(tick)

        eff    = sign * step_offset
        target = (target + eff) % PHASE_WRAP

        # accumulator step (full precision, detect carry)
        acc_new = acc + step_base + eff
        carry   = acc_new >= PHASE_WRAP
        acc     = acc_new % PHASE_WRAP

        if carry:
            pulse_ticks.append(tick)

        # edge re-anchor (corrected RTL); carry above used the pre-snap value,
        # matching the RTL where nco_tick evaluates acc_sum before the snap
        if tick >= next_edge_int:
            if edge_locked:
                acc = target
            next_edge_f  += T_in_clks
            next_edge_int = round(next_edge_f)

        counter += 1

    # ── Relative phase: pulse vs input edge grid (oscilloscope view) ──────────
    rel_phases = np.array([
        (t / T_in_clks + start_frac) % 1.0
        for t in pulse_ticks
    ])

    # Wrap to [-0.5, 0.5): relative phase should stay near P0±P
    rel_phases = (rel_phases + 0.5) % 1.0 - 0.5

    return {
        "pulse_ticks":  np.array(pulse_ticks),
        "rel_phases":   rel_phases,
        "flip_ticks":   np.array(flip_ticks),
        "half_period":  half_period,
        "T_in_clks":    T_in_clks,
        "step_base":    step_base,
        "step_offset":  step_offset,
    }

# ── Verification ───────────────────────────────────────────────────────────────

def verify(res, P0_frac, P_frac):
    phases    = res["rel_phases"]
    T_in      = res["T_in_clks"]
    ok        = True

    # Tolerance = 2 ticks (quantisation floor) + 0.1% absolute
    tol_abs   = 2.0 / T_in + 0.001
    tol_std   = 0.5 / T_in + 0.001     # std is very precise

    measured_max  = phases.max()
    measured_min  = phases.min()
    measured_mean = phases.mean()
    measured_std  = phases.std()

    expected_max  = P0_frac + P_frac
    expected_min  = P0_frac - P_frac
    expected_mean = P0_frac
    expected_std  = P_frac / math.sqrt(3)   # uniform triangle → std = A/√3

    checks = [
        ("Phase max",  measured_max,  expected_max,  tol_abs),
        ("Phase min",  measured_min,  expected_min,  tol_abs),
        ("Phase mean", measured_mean, expected_mean, tol_abs),
        ("Phase std",  measured_std,  expected_std,  tol_std),
    ]
    for name, meas, exp, t in checks:
        err = abs(meas - exp)
        status = "OK" if err <= t else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"    {status}  {name}: {meas*100:+7.3f}%  "
              f"(expected {exp*100:+7.3f}%,  err {err*100:.3f}%,  tol {t*100:.3f}%)")

    print(f"    NCO tick resolution: {100/T_in:.3f}% of T_in")
    return ok

# ── Plot ───────────────────────────────────────────────────────────────────────

def plot_results(res, f_in_hz, f_shift_hz, P0_frac, P_frac, outpath, title=""):
    pulse_ticks = res["pulse_ticks"]
    rel_phases  = res["rel_phases"]
    flip_ticks  = res["flip_ticks"]

    t_ms = pulse_ticks / CLK_HZ * 1e3

    fig, axes = plt.subplots(2, 1, figsize=(13, 8))
    fig.suptitle(title or
                 f"f_in={f_in_hz:.0f} Hz  f_shift={f_shift_hz:.0f} Hz  "
                 f"P0={P0_frac*100:.1f}%  P={P_frac*100:.1f}%", fontsize=11)

    ax = axes[0]
    ax.scatter(t_ms, rel_phases * 100, s=1.5, color="steelblue", label="output pulses")
    for ft in flip_ticks / CLK_HZ * 1e3:
        ax.axvline(ft, color="orange", lw=0.4, alpha=0.4)
    ax.axhline(P0_frac      * 100, color="gray",  lw=1.0, ls="--", label=f"P0={P0_frac*100:.1f}%")
    ax.axhline((P0_frac+P_frac)*100, color="green", lw=0.8, ls=":",  label=f"P0+P={( P0_frac+P_frac)*100:.1f}%")
    ax.axhline((P0_frac-P_frac)*100, color="green", lw=0.8, ls=":",  label=f"P0-P={(P0_frac-P_frac)*100:.1f}%")
    ax.set_xlabel("Time (ms)")
    ax.set_ylabel("Relative phase (% of T_in)")
    ax.set_title("Output phase relative to input edges — should be triangle wave")
    ax.legend(loc="upper right", markerscale=4)
    ax.grid(True, alpha=0.3)

    # Histogram of phases
    ax2 = axes[1]
    ax2.hist(rel_phases * 100, bins=100, color="steelblue", edgecolor="none")
    ax2.axvline((P0_frac+P_frac)*100, color="red",   lw=1.5, ls="--", label="P0+P")
    ax2.axvline((P0_frac-P_frac)*100, color="red",   lw=1.5, ls="--", label="P0-P")
    ax2.axvline(P0_frac*100,          color="orange", lw=1.5, ls="-",  label="P0")
    ax2.set_xlabel("Relative phase (% of T_in)")
    ax2.set_ylabel("Count")
    ax2.set_title("Phase histogram — triangle wave → flat distribution between P0±P")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(outpath, dpi=150)
    print(f"  Plot → {outpath}")
    plt.close()

# ── Run scenarios ──────────────────────────────────────────────────────────────

def run_scenario(label, f_in, f_shift, P0, P, n_osc=8, plot=False, plot_path=None,
                 edge_locked=True, window_cycles=WINDOW_100MS, start_frac=0.0,
                 expect_fail=False):
    print(f"\n{'='*60}")
    print(f"Scenario: {label}")
    print(f"  f_in={f_in:.0f} Hz  f_shift={f_shift:.1f} Hz  P0={P0*100:.1f}%  P={P*100:.1f}%")
    print(f"  logic={'edge-locked' if edge_locked else 'legacy open-loop'}  "
          f"window={window_cycles if window_cycles else 'ideal'}  "
          f"enable offset={start_frac:.2f}·T_in")

    issues, warnings = check_limits(f_in, f_shift, P0, P)
    for w in warnings:
        print(f"  [WARN]  {w}")
    for e in issues:
        print(f"  [ERROR] {e}")
    if issues:
        print("  → Skipped")
        return None

    res = simulate_osc_nco(f_in, f_shift, P0, P, n_osc_cycles=n_osc,
                           edge_locked=edge_locked, window_cycles=window_cycles,
                           start_frac=start_frac)
    if window_cycles:
        f_meas = res["step_base"] * CLK_HZ / PHASE_WRAP
        print(f"  measured f_in = {f_meas:.3f} Hz  (error {f_meas - f_in:+.3f} Hz)")
    ok  = verify(res, P0, P_frac=P)
    print(f"  → {'PASS' if ok else 'FAIL'}"
          + ("  (expected: legacy open-loop logic)" if expect_fail and not ok else ""))

    if plot and plot_path:
        plot_results(res, f_in, f_shift, P0, P, plot_path)
    return ok


if __name__ == "__main__":
    all_ok = True

    def _track(ok):
        global all_ok
        if ok is False:
            all_ok = False

    # ── A. Legacy vs corrected on realistic hardware conditions ───────────────
    # Enable happens mid-period (start_frac) and the base step carries the
    # reciprocal-counter quantization error: the legacy open-loop logic drifts
    # and is centred at a random phase; the edge-locked logic stays put.
    legacy_ok = run_scenario(
        "HW conditions — LEGACY open-loop (demonstrates the bug)",
        999_983, 100, 0.20, 0.05, start_frac=0.37,
        edge_locked=False, expect_fail=True, plot=True,
        plot_path="/tmp/osc_legacy.png")
    if legacy_ok:
        print("  [WARN] legacy logic unexpectedly passed — non-idealities too mild?")

    _track(run_scenario(
        "HW conditions — EDGE-LOCKED (corrected logic)",
        999_983, 100, 0.20, 0.05, start_frac=0.37, plot=True,
        plot_path="/tmp/osc_locked.png"))

    # ── B. Verification suite (corrected logic, hardware non-idealities) ──────
    # 1. Nominal
    _track(run_scenario("Nominal: small P", 1_000_000, 100, 0.0, 0.05,
                        start_frac=0.61, plot=True,
                        plot_path="/tmp/osc_nominal.png"))

    # 2. Non-zero P0
    _track(run_scenario("Non-zero P0", 1_000_000, 100, 0.20, 0.10,
                        start_frac=0.13, plot=True,
                        plot_path="/tmp/osc_P0.png"))

    # 3. Large P
    _track(run_scenario("Large P=40%", 1_000_000, 100, 0.0, 0.40, start_frac=0.83))

    # 4. P=50% — alias limit
    _track(run_scenario("P=50% alias limit (expect error)", 1_000_000, 100, 0.0, 0.50))

    # 5. Illegal: f_shift >= f_in
    _track(run_scenario("f_shift >= f_in (expect error)", 1_000_000, 1_500_000, 0.0, 0.05))

    # 6. Very small P (precision test)
    _track(run_scenario("Small P=0.1%", 1_000_000, 100, 0.0, 0.001))

    # 7. Slow oscillation (f_shift = 1 Hz)
    _track(run_scenario("Slow: f_shift=1 Hz", 1_000_000, 1, 0.0, 0.05, n_osc=3,
                        start_frac=0.42))

    # 8. P0 near boundary (P0+P approaching 50%)
    _track(run_scenario("P0+P near 50% boundary", 1_000_000, 100, 0.35, 0.10,
                        start_frac=0.55))

    # 9. High f_shift
    _track(run_scenario("High f_shift=10000 Hz", 1_000_000, 10_000, 0.0, 0.05,
                        start_frac=0.29))

    print(f"\n{'='*60}")
    print(f"Overall: {'PASS' if all_ok else 'FAIL'} (edge-locked scenarios)")
    sys.exit(0 if all_ok else 1)
