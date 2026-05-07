# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Desktop tools for controlling a custom Red Pitaya FPGA TTL signal generator over SSH.

Two FPGA modes are supported, each in its own self-contained subfolder:

| Subfolder | Mode | Formula | Duty cycle |
|-----------|------|---------|------------|
| `pulse_generator/` | Pulse / Freq-Shift | f_out = f_in + f_shift | User-adjustable |
| `harmonic_generator/` | Harmonic Generator | f_out = N × f_in + f_shift | Fixed 50 % |

Signal path (both modes): `DIO0_P` → FPGA period measurement + NCO → `DIO1_P`.

### Top-level files

| File | Purpose |
|------|---------|
| `redpitaya_combined_gui_qt.py` | Combined two-tab PySide6 GUI; drives both modes through one SSH session. Reads helper-C and bitstreams from the two subfolders. |
| `requirements.txt` | Shared Python dependencies (`PySide6`, `paramiko`). |
| `PHASE1_RECIPROCAL_COUNTING.md` | Reciprocal-counting measurement design notes. |

Each subfolder contains its own `README.md` and `CLAUDE.md` with mode-specific detail.

## Commands

```bash
# Install dependencies
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# Run combined GUI (both modes, one window)
.venv/bin/python redpitaya_combined_gui_qt.py

# Run standalone GUIs
.venv/bin/python pulse_generator/redpitaya_pulse_gui_qt.py
.venv/bin/python harmonic_generator/redpitaya_harmonic_gui_qt.py

# Run CLI register monitors
python3 pulse_generator/redpitaya_register_monitor.py --host rp-xxxxxx.local --interval 0.5 --count 20
python3 harmonic_generator/redpitaya_register_monitor.py --host rp-xxxxxx.local --interval 0.5 --count 20

# Compile board-side helpers manually on the Red Pitaya
scp pulse_generator/rp_pulse_ctl.c root@rp-xxxxxx.local:/root/rp_pulse_ctl.c
ssh root@rp-xxxxxx.local 'gcc -O2 -o /root/rp_pulse_ctl /root/rp_pulse_ctl.c'

scp harmonic_generator/rp_harmonic_ctl.c root@rp-xxxxxx.local:/root/rp_harmonic_ctl.c
ssh root@rp-xxxxxx.local 'gcc -O2 -o /root/rp_harmonic_ctl /root/rp_harmonic_ctl.c'
```

## Architecture

### Combined GUI (`redpitaya_combined_gui_qt.py`)

Two-layer design: Qt main thread for UI only; `SshBackend` for all SSH/SFTP work.

`SshBackend` maintains one persistent `paramiko` SSH+SFTP session. All remote work
is queued through a single background thread with these priorities:

- `P_USER = 0` — register writes and window changes.
- `P_UPLOAD = 1` — helper upload, bitstream upload, remote compile/load.
- `P_INIT = 2` — connect / disconnect.
- `P_POLL = 9` — periodic register reads.

`self._mode` ("pulse" | "harmonic") tracks which binary is active on the FPGA.
Results flow back via signals: `sig_connected`, `sig_disconnected`, `sig_status`,
`sig_log`, `sig_error`, `sig_mode_changed`.

Asset paths resolved by the combined GUI:

| Asset | Path |
|-------|------|
| Pulse C helper | `pulse_generator/rp_pulse_ctl.c` |
| Pulse bitstream | `pulse_generator/red_pitaya_top.bit.bin` |
| Harmonic C helper | `harmonic_generator/rp_harmonic_ctl.c` |
| Harmonic bitstream | `harmonic_generator/red_pitaya_top.bit.bin` |

### Standalone GUIs

Both standalone GUIs use the same two-layer design as the combined GUI. Each resolves
its C helper and bitstream via `Path(__file__).resolve().parent` — they must stay in
the same folder as their respective assets.

### Math helpers (shared across all GUIs)

- `hz_to_phase(delta_hz)` / `phase_to_hz(word)` — convert between Hz and the signed
  48-bit NCO offset word.
- `duty_to_cycles(frac, period)` — convert width fraction to clock cycles (pulse mode).
- `suggest_window(f_shift_hz)` — recommend one of the five reciprocal-counting windows.

### FPGA register map (both modes share the same base)

Base address: `0x40600000`

| Offset | Pulse mode | Harmonic mode |
|--------|-----------|---------------|
| `0x00` | `control` (bit 0=enable, bit 1=soft reset) | same |
| `0x08` | `width` (pulse cycles) | `mult_n` (harmonic order 1..5) |
| `0x10` | `status` (busy/valid/stable/timeout/freerun) | same |
| `0x14` | `raw_period` | same |
| `0x18` | `period_avg` | same |
| `0x1C/0x20` | `phase_step_offset` (signed 48-bit) | same |
| `0x24/0x28` | `phase_step_base` (read-only) | same |
| `0x2C/0x30` | `phase_step` (live, read-only) | same |
| `0x34` | `meas_time_us` | same |

The key difference: register `0x08` is `width` in pulse mode and `mult_n` in harmonic
mode — this is how the GUIs detect which bitfile is loaded (JSON payload key).

### Board-side helpers

| Binary | Mode | Write signature |
|--------|------|----------------|
| `/root/rp_pulse_ctl` | Pulse | `write <width_cycles> <phase_step_offset> <control>` |
| `/root/rp_harmonic_ctl` | Harmonic | `write <mult_n> <phase_step_offset> <control>` |

Both helpers print a single JSON object on stdout. The GUIs treat that payload as
the source of truth. 48-bit NCO values are split across two AXI words; helpers write
the high word first for atomic latching.

## Development Guidance

- Keep all SSH, SCP, and polling work off the Qt main thread.
- Preserve the single-session, priority-queued backend model.
- When updating the FPGA register map, reflect the change in all three places for the
  relevant mode: the `.sv` file, the C helper, and the GUI parser/UI.
- Keep helper JSON field names aligned with what the GUI parses.
- Use non-interactive remote commands with explicit paths.
- `red_pitaya_top.bit.bin` files exist in each subfolder and are uploaded by the GUIs,
  but should not be treated as guaranteed source-controlled design provenance.

## What Not To Do

- Do not move SSH/network work onto the Qt UI thread.
- Do not merge pulse and harmonic register layouts — the `0x08` difference is intentional.
- Do not add `width` logic to the harmonic mode or `mult_n` to the pulse mode.
- Do not commit `__pycache__/`, `.DS_Store`, logs, or generated artifacts.
- Do not document register semantics from stale code without checking the C helper
  source and the RTL.
