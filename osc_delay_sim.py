"""
Stepped Strobe Mode — NCO simulation & verification.

Tick-accurate model of the pulse_gen.sv stepped-strobe logic (osc_mode):
  - accumulator += step_base each tick (zero effective offset in osc mode)
  - on every input rising edge the accumulator snaps to osc_target
    (edge-locked: each pulse fires at a constant phase after the edge)
  - every dwell_cycles ticks osc_target += step_word (mod 2^48), advancing
    the sampled phase by step_frac of the input period
  - after n_steps points strobe_done latches and the last phase is held

Hardware non-idealities modelled:
  - the mode is enabled at an arbitrary instant relative to the input
    edges (enable_frac); the RTL holds the preload until the first
    accepted edge (osc_run), so the scan still starts at start_frac
  - step_base comes from the reciprocal counter (quantized measurement);
    edge re-anchoring clears the error once per input period

Also verifies the edge_lock control bit (constant frequency shift,
pulse mode) — that path is unchanged in the RTL.

Degenerate case: a level at exactly phase 0 (pulse coincident with the
input edge) can lose its pulses — depending on the sign of the
reciprocal-counter error, the NCO carry lands just after the edge and the
re-anchor snap preempts it every period. Keep levels at least one NCO
tick away from 0 (the GUI warns about this).

Usage:
    python3 osc_delay_sim.py
"""

from __future__ import annotations

import sys

# ── Hardware constants ─────────────────────────────────────────────────────────
CLK_HZ     = 124_999_999
PHASE_BITS = 48
PHASE_WRAP = 2**PHASE_BITS

WINDOW_100MS = 12_500_000     # 100 ms measurement window in clock cycles

# ── Conversion helpers ─────────────────────────────────────────────────────────

def hz_to_phase_step(f_hz: float) -> int:
    return round(f_hz * PHASE_WRAP / CLK_HZ)

def phase_preload(start_frac: float) -> int:
    """Delay φ (fraction of T_in) → 48-bit target/preload word (1-φ)·2^48."""
    return int(round((1.0 - start_frac) % 1.0 * PHASE_WRAP)) & (PHASE_WRAP - 1)

def strobe_step_word(step_frac: float) -> int:
    """Per-step target increment, mod 2^48 (subtracts phase → delay grows)."""
    return (-int(round((step_frac % 1.0) * PHASE_WRAP))) % PHASE_WRAP

def measured_step_base(f_in_hz: float, window_cycles: int) -> int:
    """True reciprocal counter model: phase_step_base = (edges-1) << 48 // span,
    span quantized to ±1 clock at each end."""
    T_in_clks = CLK_HZ / f_in_hz
    n_rise = 1 + int(window_cycles / T_in_clks)   # rising edges in window
    span   = round((n_rise - 1) * T_in_clks)      # ±1 clk quantization
    if n_rise < 3 or span <= 0:
        return 0
    return ((n_rise - 1) << PHASE_BITS) // span

# ── Core NCO simulation ────────────────────────────────────────────────────────

def simulate_strobe_nco(
    f_in_hz:      float,
    start_frac:   float,   # first sampled phase, fraction of T_in
    step_frac:    float,   # phase increment per point
    n_steps:      int,
    dwell_cycles: int,     # clock ticks per point
    *,
    edge_locked:   bool = True,
    window_cycles: int | None = WINDOW_100MS,
    enable_frac:   float = 0.0,   # where inside T_in the mode is armed
    extra_ticks:   int = 0,       # ticks to keep running after the scan ends
    constant_shift: bool = False,  # model plain edge_lock bit instead
    f_shift_hz:    float = 0.0,    # only for constant_shift
    duration_ticks: int | None = None,
) -> dict:
    """Tick-accurate simulation of pulse_gen.sv osc (stepped strobe) mode.

    constant_shift=True models the edge_lock control bit in pulse mode
    instead: effective offset = +f_shift every tick, no stepping
    (that RTL path is untouched by the strobe rework).
    """
    import numpy as np

    step_base = (measured_step_base(f_in_hz, window_cycles)
                 if window_cycles else hz_to_phase_step(f_in_hz))
    step_word = strobe_step_word(step_frac)
    offset    = hz_to_phase_step(f_shift_hz) if constant_shift else 0
    T_in_clks = CLK_HZ / f_in_hz

    total_ticks = (duration_ticks if duration_ticks is not None
                   else int(n_steps * dwell_cycles + extra_ticks))

    acc    = phase_preload(start_frac)
    target = acc
    armed  = edge_locked   # RTL holds preload until first accepted edge
    dwell_cnt  = 0
    step_index = 0
    done       = False

    next_edge_f   = ((1.0 - enable_frac) % 1.0) * T_in_clks
    next_edge_int = round(next_edge_f)

    pulse_ticks = []
    run_start   = None    # tick of the first anchoring edge (dwell t=0)

    for tick in range(total_ticks + (int(next_edge_int) if edge_locked else 0)):
        if armed:
            if tick >= next_edge_int:
                armed = False
                run_start = tick
                next_edge_f  += T_in_clks
                next_edge_int = round(next_edge_f)
            continue

        eff = offset if constant_shift else 0

        # stepped strobe: hold for dwell_cycles ticks, then step the target
        if not constant_shift:
            if not done and dwell_cycles > 0 and dwell_cnt >= dwell_cycles - 1:
                dwell_cnt = 0
                if step_index + 1 >= n_steps:
                    done = True
                else:
                    step_index += 1
                    target = (target + step_word) % PHASE_WRAP
            elif not done:
                dwell_cnt += 1
        else:
            target = (target + eff) % PHASE_WRAP

        # accumulator step (full precision, detect carry)
        acc_new = acc + step_base + eff
        carry   = acc_new >= PHASE_WRAP
        acc     = acc_new % PHASE_WRAP

        if carry:
            pulse_ticks.append(tick)

        # edge re-anchor; carry above used the pre-snap value, matching the
        # RTL where nco_tick evaluates acc_sum before the snap
        if tick >= next_edge_int:
            if edge_locked:
                acc = target
            next_edge_f  += T_in_clks
            next_edge_int = round(next_edge_f)

    # Relative phase of each pulse vs the input edge grid (oscilloscope view)
    rel_phases = np.array([
        (t / T_in_clks + enable_frac) % 1.0
        for t in pulse_ticks
    ])

    return {
        "pulse_ticks": np.array(pulse_ticks),
        "rel_phases":  rel_phases,
        "run_start":   run_start if run_start is not None else 0,
        "T_in_clks":   T_in_clks,
        "step_base":   step_base,
        "done":        done,
    }


# ── Bounded edge-lock response simulation ─────────────────────────────────────

EDGE_LOCK_RESPONSE_SHIFTS = {
    "hard": None,
    "fast": 4,
    "balanced": 6,
    "smooth": 8,
}


def _shortest_signed_modular_error(target: int, phase: int) -> int:
    """Signed 48-bit distance from phase to target."""
    error = (target - phase) % PHASE_WRAP
    return error - PHASE_WRAP if error >= PHASE_WRAP // 2 else error


def simulate_edge_lock_response(response: str, *, period_clocks: int = 128,
                                phase_jump_clocks: int = 32,
                                jump_anchor: int = 8,
                                anchor_count: int = 80,
                                harmonic_n: int = 1,
                                phase_step_offset: int = 0,
                                preload: int = 0) -> dict:
    """Model one persistent delayed input edge and the selected lock response."""
    if response not in EDGE_LOCK_RESPONSE_SHIFTS:
        raise ValueError(f"unknown edge-lock response: {response}")
    if period_clocks <= 0 or anchor_count <= 0 or harmonic_n <= 0:
        raise ValueError("period_clocks, anchor_count, and harmonic_n must be positive")

    shift = EDGE_LOCK_RESPONSE_SHIFTS[response]
    step_base = PHASE_WRAP // period_clocks
    phase_step = harmonic_n * step_base + phase_step_offset
    if phase_step <= 0:
        raise ValueError("nominal phase_step must be positive")
    quantization_band = harmonic_n * (PHASE_WRAP - period_clocks * step_base)
    correction_limit = (0 if shift is None or phase_step <= 1 else
                        min(step_base >> shift, phase_step - 1))
    anchor_ticks = [(anchor + 1) * period_clocks - 1 +
                    (phase_jump_clocks if anchor >= jump_anchor else 0)
                    for anchor in range(anchor_count)]
    anchor_at_tick = {tick: anchor for anchor, tick in enumerate(anchor_ticks)}

    phase = preload % PHASE_WRAP
    target = phase
    continuous_phase = phase
    pending_residual = 0
    adjustments = [0] * anchor_count
    reference_displacements = [(-phase_jump_clocks * harmonic_n * step_base
                                if anchor >= jump_anchor else 0)
                               for anchor in range(anchor_count)]
    corrections = []
    increments = []
    unwrapped_phase = []
    phase_trace = []
    target_trace = []
    running_trace = []
    harmonic_output = []
    pulse_ticks = []
    converged_anchor = None
    accepted_anchor_tick = anchor_ticks[0]
    run_start_tick = accepted_anchor_tick + 1
    running = False

    for tick in range(anchor_ticks[-1] + period_clocks):
        anchor = anchor_at_tick.get(tick)
        if not running:
            corrections.append(0)
            increments.append(0)
            unwrapped_phase.append(continuous_phase)
            phase_trace.append(phase)
            target_trace.append(target)
            running_trace.append(False)
            harmonic_output.append(False)
            if anchor is not None:
                running = True
            continue

        correction = 0
        if shift is not None and pending_residual:
            correction = max(-correction_limit,
                             min(correction_limit, pending_residual))
            pending_residual -= correction

        increment = phase_step + correction
        if phase + increment >= PHASE_WRAP:
            pulse_ticks.append(tick)
        next_phase = (phase + increment) % PHASE_WRAP
        target_next = (target + phase_step_offset) % PHASE_WRAP
        continuous_phase += increment
        corrections.append(correction)
        increments.append(increment)
        unwrapped_phase.append(continuous_phase)

        if anchor is not None:
            pending_residual = _shortest_signed_modular_error(target_next,
                                                              next_phase)
            if shift is None:
                adjustments[anchor] = pending_residual
                next_phase = (next_phase + pending_residual) % PHASE_WRAP
                pending_residual = 0
            elif anchor >= jump_anchor and \
                    abs(pending_residual) <= quantization_band and \
                    converged_anchor is None:
                converged_anchor = anchor
        harmonic_high = bool(phase & (PHASE_WRAP // 2))
        phase = next_phase
        target = target_next
        phase_trace.append(phase)
        target_trace.append(target)
        running_trace.append(True)
        harmonic_output.append(harmonic_high)

    return {
        "response": response,
        "step_base": step_base,
        "phase_step": phase_step,
        "harmonic_n": harmonic_n,
        "phase_step_offset": phase_step_offset,
        "quantization_band": quantization_band,
        "correction_limit": correction_limit,
        "anchor_ticks": anchor_ticks,
        "accepted_anchor_tick": accepted_anchor_tick,
        "run_start_tick": run_start_tick,
        "anchor_adjustments": adjustments,
        "reference_displacements": reference_displacements,
        "corrections": corrections,
        "increments": increments,
        "unwrapped_phase": unwrapped_phase,
        "phase_trace": phase_trace,
        "target_trace": target_trace,
        "running": running_trace,
        "harmonic_output": harmonic_output,
        "continuous_wraps": continuous_phase // PHASE_WRAP,
        "pulse_ticks": pulse_ticks,
        "converged_anchor": converged_anchor,
    }


def check_edge_lock_responses() -> bool:
    """Check hard snap plus bounded gradual response behavior."""
    results = {name: simulate_edge_lock_response(name)
               for name in EDGE_LOCK_RESPONSE_SHIFTS}
    hard = results["hard"]
    hard_ok = (hard["anchor_adjustments"][8] != 0 and
               all(adjustment == 0 for adjustment in hard["anchor_adjustments"][9:]) and
               all(displacement == hard["reference_displacements"][8]
                   for displacement in hard["reference_displacements"][8:]))
    gradual_ok = all(
        all(abs(correction) <= result["correction_limit"]
            for correction in result["corrections"]) and
        all(increment > 0 for increment in
            result["increments"][result["run_start_tick"]:]) and
        len(result["pulse_ticks"]) == result["continuous_wraps"] and
        len(result["pulse_ticks"]) == len(set(result["pulse_ticks"]))
        for name, result in results.items() if name != "hard")
    converged = [results[name]["converged_anchor"]
                 for name in ("fast", "balanced", "smooth")]
    order_ok = (all(anchor is not None for anchor in converged) and
                converged[0] < converged[1] < converged[2])
    ok = hard_ok and gradual_ok and order_ok
    print("\nBounded edge-lock response check")
    print(f"  hard={'OK' if hard_ok else 'FAIL'}  "
          f"converged fast/balanced/smooth={converged}")
    print(f"  → {'PASS' if ok else 'FAIL'}")
    return ok

# ── Verification ───────────────────────────────────────────────────────────────

def verify_strobe(res, start_frac, step_frac, n_steps, dwell_cycles):
    """Every pulse in dwell window k must sit at (start + k·step) mod 1.

    Pulses within ~1 input period after a step boundary may still carry the
    previous phase (the new target is picked up at the next anchor edge) and
    are excluded.
    """
    ticks   = res["pulse_ticks"]
    phases  = res["rel_phases"]
    t0      = res["run_start"]
    T_in    = res["T_in_clks"]
    tol     = 2.5 / T_in + 1e-4     # anchor snap + NCO tick quantization

    ok = True
    n_checked = 0
    max_err = 0.0
    seen_steps = set()
    for t, ph in zip(ticks, phases):
        rel = t - t0
        k = min(int(rel // dwell_cycles), n_steps - 1) if rel >= 0 else 0
        boundary = t0 + k * dwell_cycles
        if 0 < k and (t - boundary) < T_in + 4:
            continue    # transition pulse: previous phase still legal
        expected = (start_frac + k * step_frac) % 1.0
        err = abs((ph - expected + 0.5) % 1.0 - 0.5)
        max_err = max(max_err, err)
        n_checked += 1
        seen_steps.add(k)
        if err > tol:
            ok = False
    levels_ok = seen_steps == set(range(n_steps))
    if not levels_ok:
        ok = False
    print(f"    {'OK  ' if ok else 'FAIL'} {n_checked} pulses checked, "
          f"max phase err {max_err*100:.4f}% (tol {tol*100:.4f}%), "
          f"steps sampled {len(seen_steps)}/{n_steps}, "
          f"done={res['done']}")
    if not res["done"]:
        print("    FAIL strobe_done not reached")
        ok = False
    return ok

# ── Plot ───────────────────────────────────────────────────────────────────────

def plot_results(res, start_frac, step_frac, n_steps, outpath, title=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t_ms = res["pulse_ticks"] / CLK_HZ * 1e3
    fig, axes = plt.subplots(2, 1, figsize=(13, 8))
    fig.suptitle(title, fontsize=11)

    ax = axes[0]
    ax.scatter(t_ms, res["rel_phases"] * 100, s=1.5, color="steelblue")
    for k in range(n_steps):
        ax.axhline((start_frac + k * step_frac) % 1.0 * 100,
                   color="green", lw=0.5, ls=":")
    ax.set_xlabel("Time (ms)")
    ax.set_ylabel("Relative phase (% of T_in)")
    ax.set_title("Output phase vs input edges — should be a staircase")
    ax.grid(True, alpha=0.3)

    ax2 = axes[1]
    ax2.hist(res["rel_phases"] * 100, bins=200, color="steelblue", edgecolor="none")
    ax2.set_xlabel("Relative phase (% of T_in)")
    ax2.set_ylabel("Count")
    ax2.set_title(f"Phase histogram — {n_steps} discrete levels")
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(outpath, dpi=150)
    print(f"  Plot → {outpath}")
    plt.close()

# ── Scenarios ──────────────────────────────────────────────────────────────────

def run_scenario(label, f_in, start, step, n_steps, dwell_ms,
                 enable_frac=0.0, plot=False, plot_path=None):
    dwell_cycles = round(dwell_ms * 1e-3 * CLK_HZ)
    print(f"\n{'='*60}")
    print(f"Scenario: {label}")
    print(f"  f_in={f_in:.0f} Hz  start={start*100:.2f}%  step={step*100:.3f}%  "
          f"n={n_steps}  dwell={dwell_ms:.2f} ms ({dwell_cycles} clks)  "
          f"enable offset={enable_frac:.2f}·T_in")

    T_in_clks = CLK_HZ / f_in
    min_step = 2.0 / T_in_clks
    if step < min_step:
        print(f"  [WARN] step {step*100:.4f}% below NCO/edge resolution "
              f"({min_step*100:.4f}%)")
    if dwell_cycles < 3 * T_in_clks:
        print(f"  [WARN] dwell shorter than 3 input periods")

    res = simulate_strobe_nco(f_in, start, step, n_steps, dwell_cycles,
                              enable_frac=enable_frac,
                              extra_ticks=dwell_cycles)
    f_meas = res["step_base"] * CLK_HZ / PHASE_WRAP
    print(f"  measured f_in = {f_meas:.3f} Hz  (error {f_meas - f_in:+.3f} Hz)")
    ok = verify_strobe(res, start, step, n_steps, dwell_cycles)
    print(f"  → {'PASS' if ok else 'FAIL'}")
    if plot and plot_path:
        plot_results(res, start, step, n_steps, plot_path, title=label)
    return ok


def check_hold_after_done(f_in=1_000_000, start=0.1, step=0.02, n_steps=5,
                          dwell_ms=0.5, enable_frac=0.44):
    """After the last step the output must hold the final phase indefinitely."""
    import numpy as np

    print(f"\n{'='*60}")
    print("Hold-after-done check")
    dwell_cycles = round(dwell_ms * 1e-3 * CLK_HZ)
    res = simulate_strobe_nco(f_in, start, step, n_steps, dwell_cycles,
                              enable_frac=enable_frac,
                              extra_ticks=10 * dwell_cycles)
    t0 = res["run_start"]
    scan_end = t0 + n_steps * dwell_cycles
    T_in = res["T_in_clks"]
    tail = res["rel_phases"][res["pulse_ticks"] > scan_end + T_in + 4]
    expected = (start + (n_steps - 1) * step) % 1.0
    tol = 2.5 / T_in + 1e-4
    errs = np.abs((tail - expected + 0.5) % 1.0 - 0.5)
    ok = bool(res["done"] and len(tail) > 0 and errs.max() <= tol)
    print(f"    {len(tail)} pulses after done, max err {errs.max()*100:.4f}% "
          f"(tol {tol*100:.4f}%), done={res['done']}")
    print(f"  → {'PASS' if ok else 'FAIL'}")
    return ok


def check_edge_lock_shift(f_in, f_shift, duration_s=0.05,
                          window_cycles=WINDOW_100MS, enable_frac=0.37):
    """
    Verify the edge_lock control bit (constant frequency shift, pulse mode):
    the beat f_out - f_in must equal f_shift exactly when edge-locked,
    whereas open-loop it equals f_shift + (measurement error).
    """
    import numpy as np

    print(f"\n{'='*60}")
    print(f"Edge-lock shift check: f_in={f_in:.0f} Hz  f_shift={f_shift:.1f} Hz  "
          f"duration={duration_s*1e3:.0f} ms")
    n_ticks = int(duration_s * CLK_HZ)
    errors = {}
    for label, locked in (("open-loop", False), ("edge-locked", True)):
        res = simulate_strobe_nco(f_in, 0.0, 0.0, 1, 0,
                                  edge_locked=locked,
                                  window_cycles=window_cycles,
                                  enable_frac=enable_frac,
                                  constant_shift=True, f_shift_hz=f_shift,
                                  duration_ticks=n_ticks)
        ph  = np.unwrap(res["rel_phases"], period=1.0)          # cycles
        t   = res["pulse_ticks"] / CLK_HZ                       # seconds
        # a faster output (f_out = f_in + f_shift) makes the relative phase
        # *decrease*, so beat = -slope
        beat = -np.polyfit(t, ph, 1)[0]
        errors[label] = beat - f_shift
        print(f"  {label:12s}: beat = {beat:+12.6f} Hz   "
              f"error vs f_shift = {beat - f_shift:+.6f} Hz")
    ok = bool(abs(errors["edge-locked"]) < 0.02 and
              abs(errors["edge-locked"]) < abs(errors["open-loop"]))
    print(f"  → {'PASS' if ok else 'FAIL'}  "
          "(edge-locked beat must equal f_shift; open-loop carries the "
          "measurement error)")
    return ok


if __name__ == "__main__":
    all_ok = True

    def _track(ok):
        global all_ok
        if ok is False:
            all_ok = False

    # 1. Nominal scan: 10 points over 10% of the period
    _track(run_scenario("Nominal: 10 × 1% from 5%", 999_983,
                        0.05, 0.01, 10, 0.5, enable_frac=0.37, plot=True,
                        plot_path="/tmp/strobe_nominal.png"))

    # 2. Fine scan: small steps over a narrow window
    _track(run_scenario("Fine: 20 × 0.1% from 25%", 1_000_000,
                        0.25, 0.001, 20, 0.2, enable_frac=0.61))

    # 3. Coarse full-period scan (start offset from 0: a level at exactly
    #    phase 0 is degenerate — see module docstring)
    _track(run_scenario("Coarse: 8 × 12.5% (full period)", 1_000_000,
                        0.02, 0.125, 8, 0.5, enable_frac=0.13, plot=True,
                        plot_path="/tmp/strobe_full.png"))

    # 4. Single point (n=1: degenerate constant-phase hold)
    _track(run_scenario("Single point n=1", 1_000_000,
                        0.30, 0.05, 1, 1.0, enable_frac=0.83))

    # 5. Scan crossing the period wrap (start + n·step > 100%); step chosen
    #    so no level lands exactly on phase 0
    _track(run_scenario("Wrap: 6 × 4% from 90%", 1_000_000,
                        0.90, 0.04, 6, 0.3, enable_frac=0.55))

    # 6. Slow input (10 kHz tip)
    _track(run_scenario("Slow input 10 kHz", 10_000,
                        0.10, 0.02, 8, 2.0, enable_frac=0.42))

    # 7. Hold-after-done (single shot semantics)
    _track(check_hold_after_done())

    # 8. edge_lock control bit unchanged (constant frequency shift)
    _track(check_edge_lock_shift(999_983, 1000.0))

    # 9. bounded edge-lock response profiles
    _track(check_edge_lock_responses())

    print(f"\n{'='*60}")
    print(f"Overall: {'PASS' if all_ok else 'FAIL'}")
    sys.exit(0 if all_ok else 1)
