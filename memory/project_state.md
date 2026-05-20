---
name: project_state
description: Hardware facts about the Red Pitaya board — clock frequency, firmware status, known measurement issues
metadata:
  type: project
---

## Red Pitaya clock

Actual measured FPGA clock: **124,999,999 Hz** (not 125,000,000 Hz).
The RTL already accounts for this — `CLK_HZ = 32'd124_999_999` in `pulse_gen.sv`.

**Why:** The user measured it directly. This 1 Hz difference from the nominal 125 MHz is real and must be used in any frequency calculation.

**How to apply:** Use 124,999,999 as the clock reference in all NCO math, window sizing, and frequency readback formulas. Do not round to 125 MHz.

## Measurement bug (FIXED)

The old formula `period_avg_cycles = window_cycles / (edge_cnt >> 1)` used integer division, truncating fractional periods (e.g. 300 kHz → 416.67 cycles → truncated to 416 → +1602 ppm error).

**Fix applied:** The iterative divider now computes `phase_step_base = 2^48 * (edge_cnt/2) / window_cycles` directly, initialising rem = edge_half and shifting in 48 zero-bits with divisor = window_cycles. This eliminates the intermediate truncation. Error is now <0.01 ppm (limited only by the 1 Hz clock quantization).

Register 0x18 now reports `edge_cnt` (raw edge count) instead of `period_avg_cycles`. The GUI derives `f_in` from `phase_step_base` instead. Needs Vivado rebuild + bitstream upload to take effect on hardware.
