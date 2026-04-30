# CLAUDE.md — harmonic_generator

Guidance for Claude Code when working in this subfolder.

## Project Overview

Harmonic variant of the Red Pitaya TTL pulse generator. Produces a 50% duty-cycle
square wave at `f_out = N * f_input + f_shift` where N ∈ {1..5} is user-selectable.

Signal path: `DIO0_P` → FPGA reciprocal counter + harmonic NCO → `DIO1_P`.

Active components:

1. `redpitaya_harmonic_gui_qt.py` — primary PySide6 desktop GUI.
2. `redpitaya_register_monitor.py` — CLI live register monitor.
3. `rp_harmonic_ctl.c` — board-side C helper at `/root/rp_harmonic_ctl`.
4. `Vivado files/axi4lite_pulse_regs.sv` — AXI4-Lite register bank.
5. `Vivado files/pulse_gen.sv` — reciprocal counter + harmonic NCO.
6. `Vivado files/red_pitaya_top.sv` — top-level Red Pitaya integration.

## Commands

```bash
# Install dependencies
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# Run GUI
.venv/bin/python redpitaya_harmonic_gui_qt.py

# Run CLI monitor
python3 redpitaya_register_monitor.py --host rp-xxxxxx.local --interval 0.5 --count 20

# Compile board-side helper
scp rp_harmonic_ctl.c root@rp-xxxxxx.local:/root/rp_harmonic_ctl.c
ssh root@rp-xxxxxx.local 'gcc -O2 -o /root/rp_harmonic_ctl /root/rp_harmonic_ctl.c'

# Manual read
ssh root@rp-xxxxxx.local '/root/rp_harmonic_ctl 0x40600000 read'

# Manual write
ssh root@rp-xxxxxx.local '/root/rp_harmonic_ctl 0x40600000 write <mult_n> <phase_step_offset> <control>'

# Set measurement window
ssh root@rp-xxxxxx.local '/root/rp_harmonic_ctl 0x40600000 window <microseconds>'
```

## Architecture

### Register map

Base address: `0x40600000`

| Offset | Register | Notes |
|--------|----------|-------|
| `0x00` | `control` | bit 0 = output enable, bit 1 = soft reset strobe |
| `0x04` | reserved | address stability placeholder |
| `0x08` | `mult_n` | harmonic order, 3-bit [1..5], default 1 |
| `0x0C` | reserved | address stability placeholder |
| `0x10` | `status` | bit 0=busy, bit 1=period_valid, bit 2=period_stable, bit 3=timeout, bit 4=freerun_active |
| `0x14` | `raw_period` | last measured input period in cycles |
| `0x18` | `period_avg` | reciprocal-counted period in cycles |
| `0x1C/0x20` | `phase_step_offset` | signed 48-bit NCO offset |
| `0x24/0x28` | `phase_step_base` | `2^48 / period_avg` (read-only) |
| `0x2C/0x30` | `phase_step` | live `N·base + offset` (read-only) |
| `0x34` | `meas_time_us` | measurement window in µs (min 1000) |

`mult_n` writes outside [1,5] are clamped by the FPGA: 0 → 1, >5 → 5.
Software (`rp_harmonic_ctl.c`) also clamps before writing.

### Key differences from parent project

- No `pulse_width` register (0x08 is now `mult_n`).
- Output is always 50% duty: driven from `phase_acc[47]` (MSB of NCO accumulator).
- `phase_step = N * phase_step_base + phase_step_offset` (51-bit intermediate, truncated to 48).
- Helper binary is `/root/rp_harmonic_ctl` (not `rp_pulse_ctl`).
- Write signature: `write <mult_n> <phase_step_offset> <control>`.

### Key conversions

```
input_hz   = 125_000_000 / period_avg
output_hz  = N * input_hz + phase_step_offset * 125_000_000 / 2^48
mult_n     = register at 0x08, values 1..5
NCO res    ≈ 0.44 mHz / LSB at 125 MHz
```

### GUI (`redpitaya_harmonic_gui_qt.py`)

Same two-layer design as the parent: Qt main thread for UI, `SshBackend` for SSH.
Priority queue unchanged. `apply(mult_n, offset_word, enable)` replaces the old
`apply(width_cycles, offset_word, enable)`. Helper path is `/root/rp_harmonic_ctl`.

## Development Guidance

- Keep SSH/network work off the Qt main thread.
- Keep GUI and `rp_harmonic_ctl.c` aligned on JSON field names.
- When updating the register map, update all three: `axi4lite_pulse_regs.sv`,
  `rp_harmonic_ctl.c`, and the GUI parser.
- Do not reintroduce `pulse_width` or width-counter logic.
- Output is 50% by NCO MSB — do not add a width register.
