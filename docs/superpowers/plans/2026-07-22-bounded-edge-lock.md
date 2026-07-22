# Bounded Edge-Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Hard/Fast/Balanced/Smooth edge-lock response selector that preserves legacy snapping while allowing monotonic bounded phase correction, with Balanced as the reset and GUI default.

**Architecture:** Reuse control bits `[7:6]` end-to-end: shared Python constants and GUI selection, the existing C helper/control register, and one new two-bit RTL port. Keep strobe mode on its existing hard-anchor path; for gradual pulse/harmonic locking, retain a signed modulo-2^48 phase error and consume it through a per-clock correction capped below the nominal positive NCO step.

**Tech Stack:** SystemVerilog, C11, Python 3, PySide6, `unittest`, existing tick-accurate Python NCO simulator

---

## File map

- Modify `rp_math.py`: define the four control-bit values and the Balanced default once for GUI/backend reuse.
- Modify `tests/test_rp_math.py`: lock down the two-bit mapping and default.
- Modify `osc_delay_sim.py`: model all four response modes and expose a deterministic phase-jump regression.
- Create `tests/test_edge_lock_sim.py`: verify hard snap, correction bounds, monotonicity, pulse accounting, and convergence order.
- Modify `Vivado files/axi4lite_pulse_regs.sv`: store/read bits `[7:6]`, reset them to Balanced, and expose the selector to the datapath.
- Modify `Vivado files/red_pitaya_top.sv`: wire the two-bit selector between the register block and `pulse_gen`.
- Modify `Vivado files/pulse_gen.sv`: add bounded signed phase-error consumption and corrected carry detection while retaining hard anchoring for Hard and strobe modes.
- Modify `rp_ctl.c`: accept bits `[7:6]` and report the decoded `edge_lock_response` string.
- Modify `redpitaya_combined_gui_qt.py`: add the shared selector, include its bits in every Pulse/Harmonic control write, and sync it from raw control readback.
- Modify `tests/test_gui_layout.py`: verify default, enablement, readback, and control-write propagation.
- Modify `README.md` and `CLAUDE.md`: document the extended register contract and bounded behavior.

### Task 1: Define the shared control-bit contract

**Files:**
- Modify: `rp_math.py:19-25`
- Modify: `tests/test_rp_math.py:15-24`
- Test: `tests/test_rp_math.py`

- [ ] **Step 1: Write the failing mapping test**

Add the new names to the import in `tests/test_rp_math.py` and add this test class:

```python
from rp_math import (  # noqa: E402
    CTRL_EDGE_RESPONSE_MASK, CTRL_EDGE_RESPONSE_HARD,
    CTRL_EDGE_RESPONSE_FAST, CTRL_EDGE_RESPONSE_BALANCED,
    CTRL_EDGE_RESPONSE_SMOOTH, DEFAULT_EDGE_LOCK_RESPONSE,
    EDGE_LOCK_RESPONSES,
)


class TestEdgeLockResponseBits(unittest.TestCase):
    def test_control_bit_mapping_and_default(self):
        self.assertEqual(
            EDGE_LOCK_RESPONSES,
            (
                ("Hard", 0x00),
                ("Fast", 0x40),
                ("Balanced", 0x80),
                ("Smooth", 0xC0),
            ),
        )
        self.assertEqual(CTRL_EDGE_RESPONSE_MASK, 0xC0)
        self.assertEqual(CTRL_EDGE_RESPONSE_HARD, 0x00)
        self.assertEqual(CTRL_EDGE_RESPONSE_FAST, 0x40)
        self.assertEqual(CTRL_EDGE_RESPONSE_BALANCED, 0x80)
        self.assertEqual(CTRL_EDGE_RESPONSE_SMOOTH, 0xC0)
        self.assertEqual(DEFAULT_EDGE_LOCK_RESPONSE, CTRL_EDGE_RESPONSE_BALANCED)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_rp_math.TestEdgeLockResponseBits -v`

Expected: `ImportError` because the response constants do not exist yet.

- [ ] **Step 3: Add the minimal constants**

Add directly below `CTRL_EDGE_LOCK` in `rp_math.py`:

```python
CTRL_EDGE_RESPONSE_MASK     = 0xC0
CTRL_EDGE_RESPONSE_HARD     = 0x00
CTRL_EDGE_RESPONSE_FAST     = 0x40
CTRL_EDGE_RESPONSE_BALANCED = 0x80
CTRL_EDGE_RESPONSE_SMOOTH   = 0xC0

EDGE_LOCK_RESPONSES = (
    ("Hard", CTRL_EDGE_RESPONSE_HARD),
    ("Fast", CTRL_EDGE_RESPONSE_FAST),
    ("Balanced", CTRL_EDGE_RESPONSE_BALANCED),
    ("Smooth", CTRL_EDGE_RESPONSE_SMOOTH),
)
DEFAULT_EDGE_LOCK_RESPONSE = CTRL_EDGE_RESPONSE_BALANCED
```

- [ ] **Step 4: Run the focused and full math tests**

Run: `python3 -m unittest tests.test_rp_math.TestEdgeLockResponseBits -v`

Expected: `OK`.

Run: `python3 -m unittest tests.test_rp_math -v`

Expected: all existing and new math tests pass.

- [ ] **Step 5: Commit the contract**

```bash
git add rp_math.py tests/test_rp_math.py
git commit -m "feat: define edge-lock response bits"
```

### Task 2: Add the tick-accurate phase-jump regression

**Files:**
- Modify: `osc_delay_sim.py:40-170,298-377`
- Create: `tests/test_edge_lock_sim.py`
- Test: `tests/test_edge_lock_sim.py`

- [ ] **Step 1: Write the failing behavioral tests**

Create `tests/test_edge_lock_sim.py`:

```python
#!/usr/bin/env python3
import unittest

from osc_delay_sim import PHASE_WRAP, simulate_edge_lock_response


class TestBoundedEdgeLock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {
            response: simulate_edge_lock_response(response)
            for response in ("hard", "fast", "balanced", "smooth")
        }

    def test_hard_applies_the_displacement_at_one_anchor(self):
        result = self.results["hard"]
        jump = result["phase_jump_words"]
        self.assertEqual(result["anchor_adjustments"][result["jump_anchor"]], -jump)
        self.assertTrue(all(
            adjustment == 0
            for adjustment in result["anchor_adjustments"][result["jump_anchor"] + 1:]
        ))

    def test_gradual_modes_respect_bound_and_remain_monotonic(self):
        for response in ("fast", "balanced", "smooth"):
            with self.subTest(response=response):
                result = self.results[response]
                self.assertTrue(result["corrections"])
                self.assertLessEqual(
                    max(abs(value) for value in result["corrections"]),
                    result["correction_limit"],
                )
                self.assertTrue(all(step > 0 for step in result["increments"]))
                self.assertTrue(all(
                    later > earlier
                    for earlier, later in zip(
                        result["unwrapped_phase"], result["unwrapped_phase"][1:]
                    )
                ))

    def test_gradual_pulse_count_matches_continuous_phase(self):
        for response in ("fast", "balanced", "smooth"):
            with self.subTest(response=response):
                result = self.results[response]
                expected = result["unwrapped_phase"][-1] // PHASE_WRAP
                self.assertEqual(len(result["pulse_ticks"]), expected)
                self.assertEqual(len(result["pulse_ticks"]), len(set(result["pulse_ticks"])))

    def test_convergence_order_is_fast_balanced_smooth(self):
        convergence = [
            self.results[response]["converged_anchor"]
            for response in ("fast", "balanced", "smooth")
        ]
        self.assertTrue(all(value is not None for value in convergence))
        self.assertLess(convergence[0], convergence[1])
        self.assertLess(convergence[1], convergence[2])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_edge_lock_sim -v`

Expected: `ImportError` because `simulate_edge_lock_response` does not exist.

- [ ] **Step 3: Add the four-mode tick model**

Add below `measured_step_base` in `osc_delay_sim.py`:

```python
EDGE_LOCK_RESPONSE_SHIFTS = {
    "hard": None,
    "fast": 4,
    "balanced": 6,
    "smooth": 8,
}


def _signed_phase_error(target: int, phase: int) -> int:
    return ((target - phase + PHASE_WRAP // 2) % PHASE_WRAP) - PHASE_WRAP // 2


def simulate_edge_lock_response(
    response: str,
    *,
    period_clocks: int = 128,
    phase_jump_clocks: int = 32,
    jump_anchor: int = 8,
    anchor_count: int = 80,
) -> dict:
    """Tick model of pulse-mode edge lock with a persistent reference phase jump."""
    shift = EDGE_LOCK_RESPONSE_SHIFTS[response]
    step_base = PHASE_WRAP // period_clocks
    correction_limit = 0 if shift is None else step_base >> shift
    edge_ticks = [
        index * period_clocks + (phase_jump_clocks if index >= jump_anchor else 0)
        for index in range(1, anchor_count + 1)
    ]
    edge_index = {tick: index for index, tick in enumerate(edge_ticks)}

    acc = 0
    error = 0
    target = 0
    unwrapped = 0
    pulse_ticks = []
    corrections = []
    increments = []
    unwrapped_phase = []
    anchor_errors = []
    anchor_adjustments = []

    for tick in range(1, edge_ticks[-1] + period_clocks + 1):
        if shift is None or error == 0:
            correction = 0
        else:
            magnitude = min(abs(error), correction_limit)
            correction = -magnitude if error < 0 else magnitude

        increment = step_base + correction
        total = acc + increment
        if total >= PHASE_WRAP:
            pulse_ticks.append(tick)
        next_acc = total % PHASE_WRAP
        unwrapped += increment

        if tick in edge_index:
            if shift is None:
                adjustment = _signed_phase_error(target, next_acc)
                next_acc = target
                error = 0
            else:
                adjustment = 0
                error = _signed_phase_error(target, next_acc)
            anchor_adjustments.append(adjustment)
            anchor_errors.append(abs(error))
        elif shift is not None:
            error -= correction

        acc = next_acc
        corrections.append(correction)
        increments.append(increment)
        unwrapped_phase.append(unwrapped)

    converged_anchor = next(
        (
            index
            for index, error_words in enumerate(anchor_errors)
            if index >= jump_anchor and error_words == 0
        ),
        None,
    )
    return {
        "pulse_ticks": pulse_ticks,
        "corrections": corrections,
        "increments": increments,
        "unwrapped_phase": unwrapped_phase,
        "anchor_errors": anchor_errors,
        "anchor_adjustments": anchor_adjustments,
        "correction_limit": correction_limit,
        "phase_jump_words": phase_jump_clocks * step_base,
        "jump_anchor": jump_anchor - 1,
        "converged_anchor": converged_anchor,
    }
```

Keep the strobe simulator unchanged. Add this standalone check near `check_edge_lock_shift()` and call `_track(check_edge_lock_responses())` from `__main__`:

```python
def check_edge_lock_responses():
    results = {
        response: simulate_edge_lock_response(response)
        for response in ("hard", "fast", "balanced", "smooth")
    }
    hard = results["hard"]
    hard_ok = (
        hard["anchor_adjustments"][hard["jump_anchor"]]
        == -hard["phase_jump_words"]
        and all(
            adjustment == 0
            for adjustment in hard["anchor_adjustments"][hard["jump_anchor"] + 1:]
        )
    )
    gradual_ok = all(
        max(abs(value) for value in results[name]["corrections"])
        <= results[name]["correction_limit"]
        and all(step > 0 for step in results[name]["increments"])
        and len(results[name]["pulse_ticks"])
        == results[name]["unwrapped_phase"][-1] // PHASE_WRAP
        for name in ("fast", "balanced", "smooth")
    )
    convergence = [
        results[name]["converged_anchor"]
        for name in ("fast", "balanced", "smooth")
    ]
    convergence_ok = all(value is not None for value in convergence) and (
        convergence[0] < convergence[1] < convergence[2]
    )
    ok = hard_ok and gradual_ok and convergence_ok
    print(f"Bounded edge-lock phase jump: {'PASS' if ok else 'FAIL'}")
    return ok
```

- [ ] **Step 4: Run the focused regression**

Run: `python3 -m unittest tests.test_edge_lock_sim -v`

Expected: four tests pass, including `fast < balanced < smooth` convergence.

Run: `python3 osc_delay_sim.py`

Expected: existing strobe scenarios and the new bounded edge-lock check end with `Overall: PASS`.

- [ ] **Step 5: Commit the executable model**

```bash
git add osc_delay_sim.py tests/test_edge_lock_sim.py
git commit -m "test: model bounded edge lock"
```

### Task 3: Carry the selector through the FPGA register path

**Files:**
- Modify: `Vivado files/axi4lite_pulse_regs.sv:7-12,79-92,143-168,218-227`
- Modify: `Vivado files/red_pitaya_top.sv:91-103,199-218,241-280`
- Modify: `Vivado files/pulse_gen.sv:79-100`

- [ ] **Step 1: Extend the AXI register output and reset/write behavior**

In `axi4lite_pulse_regs.sv`, document bits `[7:6]`, add the port, derive it from `reg_control`, reset to Balanced, and store the two new bits on byte-0 writes:

```systemverilog
// [5] edge_lock, [7:6] edge_lock_response
//     00=Hard, 01=Fast, 10=Balanced, 11=Smooth
output logic [1:0]  edge_lock_response,

assign edge_lock_response = reg_control[7:6];

// enable=1, edge_lock_response=Balanced (2'b10)
reg_control <= 32'h00000081;

reg_control[5]   <= wdata_latched[5];   // edge_lock
reg_control[7:6] <= wdata_latched[7:6]; // edge_lock_response
```

Do not change the existing control-register readback expression: it already returns all stored bits except the self-clearing bit 1.

- [ ] **Step 2: Wire the new port through the top level**

Add one signal and the two named-port connections in `red_pitaya_top.sv`:

```systemverilog
logic [1:0] edge_lock_response;

// pulse_gen_i
.edge_lock_response (edge_lock_response),

// regs_i
.edge_lock_response (edge_lock_response),
```

Add the matching input beside `edge_lock` in `pulse_gen.sv`:

```systemverilog
input logic [1:0] edge_lock_response, // 00 Hard, 01 Fast, 10 Balanced, 11 Smooth
```

- [ ] **Step 3: Review the port chain mechanically**

Run:

```bash
rg -n "edge_lock_response|00000081|reg_control\[7:6\]" \
  'Vivado files/axi4lite_pulse_regs.sv' \
  'Vivado files/red_pitaya_top.sv' \
  'Vivado files/pulse_gen.sv'
```

Expected: one AXI output, one top-level signal, both instance connections, one `pulse_gen` input, the Balanced reset value, and the `[7:6]` write assignment are present.

- [ ] **Step 4: Commit the register plumbing**

```bash
git add 'Vivado files/axi4lite_pulse_regs.sv' \
        'Vivado files/red_pitaya_top.sv' \
        'Vivado files/pulse_gen.sv'
git commit -m "feat: route edge-lock response bits"
```

### Task 4: Implement bounded continuous correction in the RTL

**Files:**
- Modify: `Vivado files/pulse_gen.sv:29-51,363-420,424-520,522-534`
- Test: `tests/test_edge_lock_sim.py`

- [ ] **Step 1: Add response limits and signed correction arithmetic**

Keep `phase_step` as the reported nominal step. Replace the single `acc_sum` declaration/assignment with the following combinational datapath:

```systemverilog
localparam logic [1:0] EDGE_RESPONSE_HARD     = 2'b00;
localparam logic [1:0] EDGE_RESPONSE_FAST     = 2'b01;
localparam logic [1:0] EDGE_RESPONSE_BALANCED = 2'b10;
localparam logic [1:0] EDGE_RESPONSE_SMOOTH   = 2'b11;

logic [47:0] phase_acc;
logic [48:0] acc_sum;
logic [48:0] corrected_acc_sum;
logic signed [47:0] phase_error;
logic [47:0] correction_limit_raw;
logic [47:0] correction_limit;
logic [47:0] correction_magnitude;
logic signed [47:0] phase_correction;
logic signed [48:0] corrected_phase_step;
logic gradual_lock;

assign acc_sum = {1'b0, phase_acc} + {1'b0, phase_step[47:0]};
assign gradual_lock = edge_lock && !osc_mode &&
                      edge_lock_response != EDGE_RESPONSE_HARD;

always_comb begin
  case (edge_lock_response)
    EDGE_RESPONSE_FAST:     correction_limit_raw = phase_step_base[47:0] >> 4;
    EDGE_RESPONSE_BALANCED: correction_limit_raw = phase_step_base[47:0] >> 6;
    EDGE_RESPONSE_SMOOTH:   correction_limit_raw = phase_step_base[47:0] >> 8;
    default:                correction_limit_raw = 48'd0;
  endcase

  if (phase_step <= 48'sd1)
    correction_limit = 48'd0;
  else if (correction_limit_raw >= phase_step[47:0])
    correction_limit = phase_step[47:0] - 48'd1;
  else
    correction_limit = correction_limit_raw;

  correction_magnitude = phase_error[47]
      ? (~phase_error[47:0] + 48'd1) : phase_error[47:0];
  if (correction_magnitude > correction_limit)
    correction_magnitude = correction_limit;
  phase_correction = phase_error[47]
      ? -$signed(correction_magnitude) : $signed(correction_magnitude);
end

assign corrected_phase_step = $signed({phase_step[47], phase_step}) +
                              $signed({phase_correction[47], phase_correction});
assign corrected_acc_sum = {1'b0, phase_acc} +
                           {1'b0, corrected_phase_step[47:0]};
```

The `phase_step <= 1` branch is the monotonicity guard: a negative correction can never reduce a positive nominal increment below one phase word per clock.

- [ ] **Step 2: Initialize and clear the pending error with existing lock state**

Add `phase_error <= 48'sd0;` in the hard-reset, soft-reset/disable, initial lock acquisition, lock-entry/re-arm, and unlocked cleanup branches that already initialize `phase_acc`/`osc_target`. This prevents a correction from surviving disable, re-arm, or a mode change.

- [ ] **Step 3: Replace the accumulator update in the running branch**

Replace the current hard-snap assignment at the start of the final running branch with:

```systemverilog
if (lock_en && anchor_rise && !gradual_lock) begin
  // Legacy hard response and osc/strobe mode retain exact per-edge placement.
  phase_acc   <= osc_target_next;
  phase_error <= 48'sd0;
end else if (gradual_lock) begin
  // Advance continuously even on an anchor. The new anchor replaces any old
  // residual with the shortest signed modulo-2^48 target error.
  phase_acc <= corrected_acc_sum[47:0];
  if (anchor_rise)
    phase_error <= $signed(osc_target_next - corrected_acc_sum[47:0]);
  else
    phase_error <= phase_error - phase_correction;
end else begin
  phase_acc   <= acc_sum[47:0];
  phase_error <= 48'sd0;
end
```

Leave the existing `osc_target`, dwell counter, step-index, and strobe-done updates below this block intact. In particular, strobe still sets `osc_mode`, so `gradual_lock` is false and every accepted edge follows the legacy snap branch.

- [ ] **Step 4: Use the corrected continuous sum for pulse carry detection**

Replace the current `nco_tick` assignment with:

```systemverilog
assign nco_tick = (gradual_lock ? corrected_acc_sum[48] : acc_sum[48]) &
                  freerun_active & (~lock_en | osc_run);
```

Keep harmonic output as `phase_acc[47]`; gradual harmonic edges then follow the same continuous corrected accumulator without direct output transitions.

- [ ] **Step 5: Update the nearby RTL comments and run the executable model**

Update the `FREERUN` and edge-lock comments so they describe the four modes, shortest signed error, `>>4/>>6/>>8` limits, monotonic cap, corrected pulse carry, and hard strobe anchoring.

Run: `python3 -m unittest tests.test_edge_lock_sim -v`

Expected: all bounded edge-lock model tests pass.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 6: Commit the RTL behavior**

```bash
git add 'Vivado files/pulse_gen.sv'
git commit -m "feat: bound edge-lock phase correction"
```

### Task 5: Extend the board helper contract

**Files:**
- Modify: `rp_ctl.c:17-50,87-96,105-129,167-218`

- [ ] **Step 1: Accept and decode response bits**

Add the mask, include it in `CTRL_USER_MASK`, and add the decoder:

```c
#define CTRL_EDGE_RESPONSE_MASK 0xC0u /* bits 7:6 — Hard/Fast/Balanced/Smooth */

#define CTRL_USER_MASK (CTRL_ENABLE | CTRL_FORCE_HIGH | CTRL_OSC_MODE | \
                        CTRL_EDGE_LOCK | CTRL_EDGE_RESPONSE_MASK)

static const char *edge_lock_response_name(uint32_t control) {
    switch ((control & CTRL_EDGE_RESPONSE_MASK) >> 6) {
        case 0u: return "hard";
        case 1u: return "fast";
        case 2u: return "balanced";
        default: return "smooth";
    }
}
```

Update the usage text and register-map comment to state `bits 7:6=edge_lock_response (0=hard, 1=fast, 2=balanced, 3=smooth)`.

- [ ] **Step 2: Publish the decoded JSON string**

In `print_json`, derive the value and change the exact format prefix/argument sequence shown below; leave the later fields and arguments byte-for-byte unchanged:

```c
const char *edge_lock_response = edge_lock_response_name(control);

- printf("{\"control\":%u,\"harmonic_mode\":%u,\"osc_mode\":%u,\"edge_lock\":%u,\"force_high\":%u,"
+ printf("{\"control\":%u,\"harmonic_mode\":%u,\"osc_mode\":%u,\"edge_lock\":%u,"
+        "\"edge_lock_response\":\"%s\",\"force_high\":%u,"

  control,
  harmonic_mode,
  osc_mode,
  edge_lock,
+ edge_lock_response,
  force_high,
```

Also add `edge_lock_response` to the documented JSON field list. Do not add a command or register; the raw `control` value remains authoritative.

- [ ] **Step 3: Compile-check the helper**

Run: `gcc -std=c11 -Wall -Wextra -Werror -fsyntax-only rp_ctl.c`

Expected: exit 0 with no warnings or output.

- [ ] **Step 4: Commit the helper update**

```bash
git add rp_ctl.c
git commit -m "feat: expose edge-lock response"
```

### Task 6: Add the shared GUI selector and propagate every control write

**Files:**
- Modify: `redpitaya_combined_gui_qt.py:33-64,343-379,558-595,981-1005,1192-1216,1265-1303,1327-1379,1516-1542,1619-1643`
- Modify: `tests/test_gui_layout.py:14-33,190-256`
- Test: `tests/test_gui_layout.py`

- [ ] **Step 1: Extend the fake backend and write failing GUI tests**

Import `QComboBox` in the test and extend `_FakeBackend`:

```python
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QWidget  # noqa: E402

class _FakeBackend(QObject):
    def __init__(self):
        super().__init__()
        self.mode = "pulse"
        self.window_calls = []
        self.pulse_calls = []
        self.harmonic_calls = []
        self.control_calls = []

    def apply_pulse(self, *args, **kwargs):
        self.pulse_calls.append((args, kwargs))

    def apply_harmonic(self, *args, **kwargs):
        self.harmonic_calls.append((args, kwargs))

    def set_control_pulse(self, control):
        self.control_calls.append(control)

    def set_control_harmonic(self, control):
        self.control_calls.append(control)
```

Add:

```python
class TestEdgeLockResponseSelector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.backend = _FakeBackend()
        self.panel = gui.PulsePanel(self.backend, lambda _msg: None)
        self.panel._live = True
        self.panel._period_c = 128
        self.addCleanup(self.panel.close)

    def test_defaults_to_balanced_and_is_only_enabled_while_modulated(self):
        self.assertIsInstance(self.panel._edge_response, QComboBox)
        self.assertEqual(
            self.panel._edge_response.currentData(),
            gui.CTRL_EDGE_RESPONSE_BALANCED,
        )
        self.panel._output_mode = "off"
        self.panel._update_mode_controls()
        self.assertFalse(self.panel._edge_response.isEnabled())
        self.panel._output_mode = "modulated"
        self.panel._update_mode_controls()
        self.assertTrue(self.panel._edge_response.isEnabled())

    def test_apply_forwards_selected_response(self):
        index = self.panel._edge_response.findData(gui.CTRL_EDGE_RESPONSE_FAST)
        self.panel._edge_response.setCurrentIndex(index)
        self.panel._do_apply()
        self.assertEqual(
            self.backend.pulse_calls[-1][1]["edge_response"],
            gui.CTRL_EDGE_RESPONSE_FAST,
        )

    def test_off_and_on_control_writes_preserve_selected_response(self):
        self.panel._set_output_mode("off")
        self.assertEqual(
            self.backend.control_calls[-1], gui.CTRL_EDGE_RESPONSE_BALANCED
        )
        self.panel._set_output_mode("on")
        self.assertEqual(
            self.backend.control_calls[-1],
            gui.CTRL_FORCE_HIGH | gui.CTRL_EDGE_RESPONSE_BALANCED,
        )

    def test_status_readback_syncs_selector_from_raw_control(self):
        self.panel._on_status({
            "harmonic_mode": 0,
            "control": gui.CTRL_ENABLE | gui.CTRL_EDGE_RESPONSE_SMOOTH,
            "phase_step_base": 0,
        })
        self.assertEqual(
            self.panel._edge_response.currentData(),
            gui.CTRL_EDGE_RESPONSE_SMOOTH,
        )
```

- [ ] **Step 2: Run the GUI tests to verify they fail**

Run: `QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui_layout.TestEdgeLockResponseSelector -v`

Expected: `ERROR` because `_edge_response` and the new constants are not imported by the GUI/backend API yet.

- [ ] **Step 3: Import the combo and shared response constants**

Add `QComboBox` to the Qt widget import and these names to the `rp_math` import:

```python
CTRL_EDGE_RESPONSE_MASK, CTRL_EDGE_RESPONSE_BALANCED,
CTRL_EDGE_RESPONSE_FAST, CTRL_EDGE_RESPONSE_SMOOTH,
DEFAULT_EDGE_LOCK_RESPONSE, EDGE_LOCK_RESPONSES,
```

- [ ] **Step 4: Add the selector once in `_NcoPanel`**

After the Edge lock checkbox in `_build_ui`, add:

```python
auto_row.addSpacing(12)
auto_row.addWidget(_dim_label("Response:"))
self._edge_response = QComboBox()
for label, control_bits in EDGE_LOCK_RESPONSES:
    self._edge_response.addItem(label, control_bits)
self._edge_response.setCurrentIndex(
    self._edge_response.findData(DEFAULT_EDGE_LOCK_RESPONSE)
)
self._edge_response.setToolTip(
    "Hard snaps at the next accepted edge. Fast, Balanced, and Smooth "
    "apply progressively smaller continuous phase corrections."
)
self._edge_response.currentIndexChanged.connect(self._param_changed)
auto_row.addWidget(self._edge_response)
```

Add the selector to `_update_mode_controls`' enabled widgets. It depends only on `self._output_mode == "modulated"`, not on the Edge lock checkbox.

Add this helper:

```python
def _edge_response_bits(self) -> int:
    return int(self._edge_response.currentData()) & CTRL_EDGE_RESPONSE_MASK
```

Include `"edge_lock_response": self._edge_response.currentText().lower()` in both `get_params()` dictionaries.

- [ ] **Step 5: Preserve the selected bits for all Pulse/Harmonic writes**

Change the backend APIs and control construction:

```python
def apply_pulse(self, width_cycles: int, offset_word: int,
                edge_lock: bool = False, preload: Optional[int] = None,
                edge_response: int = DEFAULT_EDGE_LOCK_RESPONSE):
    if self._live:
        ctrl = CTRL_ENABLE | (edge_response & CTRL_EDGE_RESPONSE_MASK)
        ctrl |= CTRL_EDGE_LOCK if edge_lock else 0

def apply_harmonic(self, mult_n: int, offset_word: int,
                   edge_lock: bool = False, preload: Optional[int] = None,
                   edge_response: int = DEFAULT_EDGE_LOCK_RESPONSE):
    if self._live:
        ctrl = CTRL_ENABLE | (edge_response & CTRL_EDGE_RESPONSE_MASK)
        ctrl |= CTRL_EDGE_LOCK if edge_lock else 0
```

In both locked apply helpers, preserve the response while temporarily clearing only Edge lock:

```python
self._exec(
    f"/root/rp_pulse_ctl 0x{self._base:08X} control {ctrl & ~CTRL_EDGE_LOCK}"
)
```

In `_do_apply_harmonic_locked`, use:

```python
self._exec(
    f"/root/rp_harmonic_ctl 0x{self._base:08X} control {ctrl & ~CTRL_EDGE_LOCK}"
)
```

Pass `edge_response=self._edge_response_bits()` from both panel `_do_apply()` methods. In `_set_output_mode`, preserve those bits for constant modes too:

```python
response = self._edge_response_bits()
if mode == "off":
    self._be_set_control(response)
elif mode == "on":
    self._be_set_control(CTRL_FORCE_HIGH | response)
else:
    self._do_apply()
```

This satisfies “every pulse or harmonic control write”; the strobe-only control path remains hard and does not use this selector.

- [ ] **Step 6: Sync selector state from authoritative raw control readback**

In `_on_status`, after reading `ctrl`, add:

```python
response_index = self._edge_response.findData(ctrl & CTRL_EDGE_RESPONSE_MASK)
if response_index >= 0 and response_index != self._edge_response.currentIndex():
    self._edge_response.blockSignals(True)
    self._edge_response.setCurrentIndex(response_index)
    self._edge_response.blockSignals(False)
```

Append ` · {self._edge_response.currentText().lower()}` to the output subtitle when Edge lock is active, so readback visibly confirms the chosen response.

- [ ] **Step 7: Run focused and full GUI tests**

Run: `QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui_layout.TestEdgeLockResponseSelector -v`

Expected: four selector tests pass.

Run: `QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui_layout -v`

Expected: all GUI smoke tests pass. If PySide6 is unavailable in the execution environment, report this command as not run; do not weaken or delete the tests.

- [ ] **Step 8: Commit the GUI behavior**

```bash
git add redpitaya_combined_gui_qt.py tests/test_gui_layout.py
git commit -m "feat: select edge-lock response"
```

### Task 7: Document and verify the complete change

**Files:**
- Modify: `README.md:173-204`
- Modify: `CLAUDE.md:104-145`

- [ ] **Step 1: Update user and maintainer documentation**

Add bits `[7:6]` to both register tables with the exact mapping:

```markdown
| 7:6 | edge_lock_response | 00=Hard snap, 01=Fast (1/16 cycle/period), 10=Balanced (1/64, default), 11=Smooth (1/256) |
```

State that Pulse/Harmonic gradual modes consume the shortest signed phase error without snapping, that correction is capped below the nominal step to keep phase monotonic, and that strobe mode always hard-anchors. Update any prose that still says Edge lock always snaps.

- [ ] **Step 2: Run all locally available verification**

```bash
python3 -m unittest tests.test_rp_math tests.test_edge_lock_sim -v
QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui_layout -v
python3 osc_delay_sim.py
gcc -std=c11 -Wall -Wextra -Werror -fsyntax-only rp_ctl.c
git diff --check
git status --short
```

Expected:

- math, phase-jump, GUI, and standalone simulator checks pass;
- the C helper compiles without warnings;
- `git diff --check` is silent;
- status lists only the intended source, test, and documentation changes.

- [ ] **Step 3: Record unavailable hardware verification explicitly**

Do not claim RTL synthesis or hardware validation locally. Record in the handoff that Vivado synthesis/timing and Red Pitaya oscilloscope checks remain required before replacing `red_pitaya_top.bit.bin`; specifically verify no missing/duplicate pulses across a reference phase jump in all gradual modes and confirm Fast/Balanced/Smooth settling order on hardware.

- [ ] **Step 4: Commit the documentation**

```bash
git add README.md CLAUDE.md
git commit -m "docs: describe bounded edge lock"
```
