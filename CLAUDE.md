# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Desktop tools for controlling a custom Red Pitaya FPGA TTL pulse/NCO shifter over SSH.

Signal path: `DIO0_P` -> FPGA period measurement + NCO pulse generation -> `DIO1_P`.

Active components:

1. `redpitaya_pulse_gui_qt.py` - primary PySide6 desktop GUI.
2. `redpitaya_register_monitor.py` - CLI-only live register monitor using the system `ssh` binary.
3. `rp_pulse_ctl.c` - board-side C helper uploaded to `/root/rp_pulse_ctl`; reads/writes FPGA registers via `/dev/mem`.
4. `Vivado files/axi4lite_pulse_regs.sv` - AXI4-Lite register bank.
5. `Vivado files/pulse_gen.sv` - pulse/NCO datapath.
6. `Vivado files/red_pitaya_top.sv` - top-level Red Pitaya integration.
7. `PHASE1_RECIPROCAL_COUNTING.md` - measurement design notes.

The repository no longer contains the older PLL-era flow or any Tk fallback GUI. Do not reintroduce removed legacy surfaces unless explicitly asked.

## Commands

Install dependencies on the host PC:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Run the Qt GUI:

```bash
.venv/bin/python redpitaya_pulse_gui_qt.py
```

Run the CLI register monitor:

```bash
python3 redpitaya_register_monitor.py --host rp-xxxxxx.local --interval 0.5 --count 20
```

Compile the board-side helper manually on the Red Pitaya:

```bash
scp rp_pulse_ctl.c root@rp-xxxxxx.local:/root/rp_pulse_ctl.c
ssh root@rp-xxxxxx.local 'gcc -O2 -o /root/rp_pulse_ctl /root/rp_pulse_ctl.c'
```

Manual readback:

```bash
ssh root@rp-xxxxxx.local '/root/rp_pulse_ctl 0x40600000 read'
```

Manual register write:

```bash
ssh root@rp-xxxxxx.local '/root/rp_pulse_ctl 0x40600000 write <width_cycles> <phase_step_offset> <control>'
```

Manual measurement-window selection:

```bash
ssh root@rp-xxxxxx.local '/root/rp_pulse_ctl 0x40600000 window <0|1|2|3>'
```

## Architecture

### Qt GUI (`redpitaya_pulse_gui_qt.py`)

Two-layer design: Qt main thread for UI only, plus `SshBackend` for all SSH/SFTP work.

`SshBackend` maintains one persistent `paramiko` SSH+SFTP session. All remote work is queued through a single background thread:

- `P_USER = 0` - user-triggered register writes and window changes.
- `P_UPLOAD = 1` - helper upload, bitstream upload, and remote compile/load.
- `P_INIT = 2` - connect and disconnect.
- `P_POLL = 9` - periodic register reads.

Results flow back to the Qt thread via signals: `sig_connected`, `sig_disconnected`, `sig_status`, `sig_log`, and `sig_error`.

Important module-level helpers:

- `hz_to_phase(delta_hz)` / `phase_to_hz(word)` convert between Hz and the signed 48-bit NCO offset word.
- `duty_to_cycles(frac, period)` converts width fraction to clock cycles, clamped to the valid range.
- `suggest_window(f_shift_hz)` recommends one of the four reciprocal-counting windows.

Behavioral notes:

- Auto-Apply debounces changes with a 300 ms `QTimer`.
- `Ctrl+Return` applies immediately.
- The GUI initializes the FPGA measurement window to the combo-box selection after connect.
- The current shift value is used to recommend a measurement window in the UI.

### Board-side helper (`rp_pulse_ctl.c`)

Invocation:

```text
rp_pulse_ctl <base_addr> read
rp_pulse_ctl <base_addr> write <width> <phase_step_offset> <control>
rp_pulse_ctl <base_addr> window <0|1|2|3>
rp_pulse_ctl <base_addr> soft_reset
```

The helper prints a single JSON object on stdout. The GUI treats that JSON payload as the source of truth.

48-bit NCO values are split across two AXI words. `wr48` writes the high word first and the low word second so the live latch sees a consistent update.

### FPGA register map

Base address defaults to `0x40600000`.

| Offset | Register | Notes |
|--------|----------|-------|
| `0x00` | `control` | bit 0 = output enable, bit 1 = soft reset strobe |
| `0x08` | `width` | pulse width in 125 MHz clock cycles |
| `0x10` | `status` | bit 0 = busy, bit 1 = period_valid, bit 2 = period_stable, bit 3 = timeout, bit 4 = freerun_active |
| `0x14` | `raw_period` | last measured input period in cycles |
| `0x18` | `period_avg` | filtered/reciprocal-counted period in cycles |
| `0x1C/0x20` | `phase_step_offset` | signed 48-bit NCO offset word |
| `0x24/0x28` | `phase_step_base` | computed base step, read-only |
| `0x2C/0x30` | `phase_step` | live phase step, read-only |
| `0x34` | `window_select` | `0=10 ms`, `1=100 ms`, `2=500 ms`, `3=1000 ms` |

Key conversions:

```text
input_hz           = 125_000_000 / period_avg
frequency_offset   = phase_step_offset * 125_000_000 / 2^48
phase_step_offset  = round(frequency_offset * 2^48 / 125_000_000)
width_cycles       = round(width_fraction * period_avg)
```

NCO resolution is about `0.44 mHz/LSB` at a 125 MHz clock.

### CLI monitor (`redpitaya_register_monitor.py`)

The monitor shells out to the system `ssh` client instead of using `paramiko`. It expects `/root/rp_pulse_ctl` to already exist on the board.

Treat the helper JSON schema in `rp_pulse_ctl.c` as the authoritative interface when changing register payloads. If the FPGA/helper payload changes, update the monitor to match.

## Development Guidance

- Keep all SSH, SCP, and polling work off the Qt main thread.
- Preserve the single-session, priority-queued backend model unless there is a strong reason to change it.
- Keep the GUI and `rp_pulse_ctl.c` aligned on register names, status bits, and JSON payload fields.
- When updating the FPGA register map, reflect the change in all three places:
  `axi4lite_pulse_regs.sv`, `rp_pulse_ctl.c`, and the GUI parser/UI.
- Use non-interactive remote commands with explicit paths.
- `red_pitaya_top.bit.bin` may exist locally and be uploaded by the GUI, but it should not be treated as guaranteed source-controlled design provenance.

## What Not To Do

- Do not re-add PLL-era helper binaries, removed GUI variants, or obsolete deployment scripts.
- Do not move SSH/network work onto the Qt UI thread.
- Do not document register semantics from stale code without checking `rp_pulse_ctl.c` and the RTL.
- Do not commit `__pycache__/`, `.DS_Store`, logs, or generated artifacts unless explicitly requested.
