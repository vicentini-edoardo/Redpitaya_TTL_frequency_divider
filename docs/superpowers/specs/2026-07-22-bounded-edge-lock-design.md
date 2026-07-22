# Bounded Edge-Lock Design

## Goal

Prevent missing pulses and abrupt harmonic phase transitions by replacing the
mandatory per-edge accumulator snap with a selectable bounded phase correction.
Keep the current hard-snap behavior available for diagnostics and backwards
comparison.

## User interface

Pulse and Harmonic panels gain an **Edge-lock response** selector beside the
existing Edge lock control:

| Response | Control bits `[7:6]` | Maximum correction |
|---|---:|---:|
| Hard | `00` | Existing accumulator snap |
| Fast | `01` | 1/16 input cycle per input period |
| Balanced | `10` | 1/64 input cycle per input period |
| Smooth | `11` | 1/256 input cycle per input period |

Balanced is the GUI and FPGA-reset default. The selector is enabled only while
the output is modulated. Its value is included in every pulse or harmonic
control write, so changing output mode does not require a new AXI register.

## Register and helper contract

The existing control register is extended:

- bits `[7:6]`: edge-lock response as defined above;
- bit `5`: edge-lock enable remains unchanged;
- all lower control bits retain their current meanings.

`rp_ctl.c` accepts bits `[7:6]` in its user mask and reports an
`edge_lock_response` JSON string. The raw `control` value remains authoritative,
so existing state publication already records the selected response.

## FPGA behavior

Hard mode preserves the current behavior: every accepted anchor edge replaces
`phase_acc` with the edge-referenced target.

Fast, Balanced, and Smooth modes never overwrite a running accumulator with the
target. At every accepted anchor edge, the FPGA calculates the shortest signed
modulo-2^48 difference between the target and the accumulator's next continuous
phase. Subsequent clock cycles add or subtract a bounded correction from the
nominal NCO phase step until that error is consumed or a new anchor measurement
replaces it.

The per-clock bound is derived from `phase_step_base >> response_shift`, where
the shifts are 4, 6, and 8. This normalizes the maximum accumulated correction
to approximately 1/16, 1/64, or 1/256 phase cycle per input period. The bound is
also capped below the positive nominal `phase_step`, keeping the accumulator
monotonic even when a negative correction is required.

Pulse carry detection uses the corrected continuous sum. Harmonic output
continues to use the accumulator MSB. Therefore gradual correction changes edge
timing without directly inserting or deleting an output transition.

Initial edge-lock acquisition still preloads and waits for the first accepted
input edge before emitting pulses. Oscillating/strobe mode retains hard
anchoring regardless of the selector because its sampled phase levels require
exact per-edge placement.

## Verification

Extend the tick-accurate Python NCO simulation with all four response modes.
Add a regression that applies a sudden reference phase displacement and checks:

- Hard mode applies the displacement at one anchor;
- gradual modes limit correction to their configured bound;
- gradual phase remains monotonic;
- pulse counts contain no missing or duplicate cycle caused by correction;
- convergence ordering is Fast, then Balanced, then Smooth.

Run the existing Python test suite and simulator checks. RTL synthesis and
hardware oscilloscope validation remain required before replacing the deployed
bitstream because Vivado and physical Red Pitaya hardware are not available in
the local test environment.
