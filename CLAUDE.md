# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Desktop tools for controlling a custom Red Pitaya FPGA TTL signal generator over SSH.

Two operating modes are supported by a **single unified FPGA bitfile** and a
**single board-side C helper** (`rp_ctl.c`). The helper selects its mode from the
binary name it is invoked as — switching modes is instant, no re-flashing.

| Mode | Formula | Duty cycle |
|------|---------|------------|
| Pulse / Freq-Shift | f_out = f_in + f_shift | User-adjustable |
| Harmonic Generator | f_out = N × f_in + f_shift | Fixed 50 % |

Signal path (both modes): `DIO0_P` → FPGA period measurement + NCO → `DIO1_P`.
An independent free-running square wave is available on `DIO2_P`.

### Repository layout

| Path | Purpose |
|------|---------|
| `redpitaya_combined_gui_qt.py` | Two-tab PySide6 GUI; drives both modes through one SSH session. |
| `rp_math.py` | Qt-free hardware constants + frequency/duty conversion math (imported by the GUI; unit-tested). |
| `rp_ctl.c` | Unified board-side C helper (pulse + harmonic), compiled once and symlinked to two names. |
| `red_pitaya_top.bit.bin` | Unified FPGA bitstream uploaded by the GUI. |
| `requirements.txt` | Python dependencies (`PySide6-Essentials`, `paramiko`). |
| `tests/` | `unittest`/`pytest` suite for `rp_math`. |
| `Vivado files/` | RTL source: `red_pitaya_top.sv`, `pulse_gen.sv`, `axi4lite_pulse_regs.sv`. |
| `memory/` | Project notes (measured clock, fixed measurement bug). |

## Commands

```bash
# Install dependencies
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# Run the GUI (both modes, one window)
.venv/bin/python redpitaya_combined_gui_qt.py

# Run the math unit tests (no Qt/paramiko needed)
python3 -m unittest discover -s tests
# or, if pytest is installed:
pytest tests/

# Compile the board-side helper manually on the Red Pitaya
scp rp_ctl.c root@rp-xxxxxx.local:/root/rp_ctl.c
ssh root@rp-xxxxxx.local 'gcc -O2 -o /root/rp_ctl /root/rp_ctl.c && \
    ln -sf /root/rp_ctl /root/rp_pulse_ctl && \
    ln -sf /root/rp_ctl /root/rp_harmonic_ctl'

# Flash the bitstream (once per board boot)
scp red_pitaya_top.bit.bin root@rp-xxxxxx.local:/root/
ssh root@rp-xxxxxx.local '/opt/redpitaya/bin/fpgautil -b /root/red_pitaya_top.bit.bin'
```

## Architecture

### GUI (`redpitaya_combined_gui_qt.py`)

Two-layer design: Qt main thread for UI only; `SshBackend` for all SSH/SFTP work.

`SshBackend` maintains one persistent `paramiko` SSH+SFTP session. All remote work
is queued through a single background thread with these priorities:

- `P_USER = 0` — register writes and window changes.
- `P_UPLOAD = 1` — helper upload, bitstream upload, remote compile/load.
- `P_INIT = 2` — connect / disconnect.
- `P_POLL = 9` — periodic register reads.

A failed `P_POLL` job is logged and skipped without dropping the session; failures
of any other priority tear the session down and emit `sig_disconnected`.

`self._mode` ("pulse" | "harmonic") tracks which helper symlink is called for polls.
Results flow back via signals: `sig_connected`, `sig_disconnected`, `sig_status`,
`sig_log`, `sig_error`, `sig_mode_changed`.

`PulsePanel` and `HarmonicPanel` are the two tabs; both consume the same polled
status dict and ignore JSON whose `harmonic_mode` flag does not match their mode.

Assets are resolved relative to the script directory (`Path(__file__).resolve().parent`):
`rp_ctl.c` and `red_pitaya_top.bit.bin`.

### Math helpers (`rp_math.py`)

Pure, Qt-free functions shared by the GUI and covered by `tests/test_rp_math.py`:

- `hz_to_phase(delta_hz)` / `phase_to_hz(word)` — convert between Hz and the signed
  48-bit NCO offset word.
- `duty_to_cycles(frac, period)` — width fraction → clock cycles (pulse mode).
- `suggest_window(f_shift_hz)` — recommend one of the five reciprocal-counting windows.
- `trig_hz_to_phase_step(f_hz)` / `trig_phase_step_to_hz(step)` — DIO2
  frequency on the same 48-bit NCO grid used by frequency shift.
- formatting helpers (`fmt_freq`, `fmt_signed_freq`, `fmt_dur`).

Hardware constants also live here: `CLK_HZ = 124_999_999` (measured, not nominal),
`PHASE_BITS = 48`, `DEFAULT_BASE = 0x40600000`, `WINDOW_OPTIONS_US`/`WINDOW_NAMES`.

### FPGA register map

Base address: `0x40600000`. Register `0x08` is shared: `width_n` in pulse mode,
`mult_n` (1..5) in harmonic mode. Mode is selected by `control` bit 3 (`harmonic_mode`),
which the C helper sets/clears based on its invocation name.

| Offset | Register | Notes |
|--------|----------|-------|
| `0x00` | `control` | bit 0=enable, bit 1=soft_reset (self-clearing), bit 2=force_high, bit 3=harmonic_mode |
| `0x04/0x0C` | `trig_phase_step` | DIO2 48-bit NCO phase step (0=off) |
| `0x08` | `width_n` / `mult_n` | pulse width cycles (pulse) or harmonic order 1..5 (harmonic) |
| `0x10` | `status` | bit 0=busy, bit 1=period_valid, bit 2=period_stable, bit 3=timeout, bit 4=freerun_active |
| `0x14` | `raw_period` | edge count from last measurement window (legacy name) |
| `0x18` | `edge_cnt` | edge count from last measurement window |
| `0x1C/0x20` | `phase_step_offset` | signed 48-bit NCO frequency offset |
| `0x24/0x28` | `phase_step_base` | computed base step (read-only) |
| `0x2C/0x30` | `phase_step` | live `[N·]base + offset` (read-only) |
| `0x34` | `meas_time_us` | measurement window in µs (min 1000) |

The authoritative `status` bit order is the rdata concatenation in
`axi4lite_pulse_regs.sv` (the `4'd4` read case), **not** any prose comment.

### Board-side helper (`rp_ctl.c`)

One binary, two symlink names; mode detected from `argv[0]`:

| Symlink | Mode | Write signature |
|---------|------|----------------|
| `/root/rp_pulse_ctl` | Pulse | `write <width_cycles> <phase_step_offset> <control>` |
| `/root/rp_harmonic_ctl` | Harmonic | `write <mult_n> <phase_step_offset> <control>` |

Subcommands (both modes): `read`, `write`, `control <value>`, `window <us>`,
`trig <phase_step>`, `soft_reset`. Every subcommand prints one JSON object on stdout
that the GUI treats as the source of truth. 48-bit NCO values are split across two AXI
words; the helper writes the high word first for atomic latching.

## Development Guidance

- Keep all SSH, SCP, and polling work off the Qt main thread.
- Preserve the single-session, priority-queued backend model.
- Keep pure conversion math in `rp_math.py` (Qt-free) so it stays unit-testable; add
  a test in `tests/` when you touch the frequency/duty math.
- When updating the FPGA register map, reflect the change in all three places: the
  `.sv` file, `rp_ctl.c`, and the GUI parser/UI.
- Treat the RTL rdata concatenation (not comments) as the authoritative bit order.
- Keep helper JSON field names aligned with what the GUI parses.
- Use non-interactive remote commands with explicit paths.
- `red_pitaya_top.bit.bin` is uploaded by the GUI but is not guaranteed source-controlled
  design provenance.

## What Not To Do

- Do not move SSH/network work onto the Qt UI thread.
- Do not let a single failed poll tear down the SSH session.
- Do not duplicate the conversion math back into the GUI module.
- Do not document register semantics from stale prose comments without checking the
  RTL rdata concatenation and the C helper source.
- Do not commit `__pycache__/`, `.DS_Store`, logs, compiled helpers, or generated artifacts.
