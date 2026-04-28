# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Desktop tools for controlling a custom Red Pitaya FPGA TTL frequency divider / NCO pulse generator over SSH.

Signal path: `DIO0_P` → FPGA → `DIO1_P`, where the FPGA measures the input period and generates a shifted/divided output pulse.

Active components:

1. `redpitaya_pulse_gui_qt.py` — preferred PySide6 desktop GUI (runs on the host PC).
2. `redpitaya_register_monitor.py` — CLI-only live register monitor using the system `ssh` binary.
3. `redpitaya_pulse_gui_c_helper.py` — legacy Tkinter fallback; not the preferred surface.
4. `rp_pulse_ctl.c` — board-side C helper uploaded to `/root/rp_pulse_ctl`; reads/writes FPGA registers via `/dev/mem`.
5. `Vivado files/` — SystemVerilog FPGA sources (`pulse_gen.sv`, `axi4lite_pulse_regs.sv`, `red_pitaya_top.sv`).

The legacy PLL implementation and its older GUIs have been removed and must not be reintroduced.

## Commands

**Install dependencies (host PC):**
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# requirements.txt: PySide6-Essentials>=6.6, paramiko>=3.0
```

**Run the Qt GUI:**
```bash
.venv/bin/python redpitaya_pulse_gui_qt.py
```

**Run the CLI register monitor:**
```bash
python3 redpitaya_register_monitor.py --host rp-xxxxxx.local [--interval 0.5] [--count 20]
```

**Compile the board-side helper manually (on the Red Pitaya):**
```bash
scp rp_pulse_ctl.c root@rp-xxxxxx.local:/root/rp_pulse_ctl.c
ssh root@rp-xxxxxx.local 'gcc -O2 -o /root/rp_pulse_ctl /root/rp_pulse_ctl.c'
```

**Manual register readback:**
```bash
ssh root@rp-xxxxxx.local '/root/rp_pulse_ctl 0x40600000 read'
```

## Architecture

### Qt GUI (`redpitaya_pulse_gui_qt.py`)

Two-layer design: Qt main thread (UI only) ↔ `SshBackend` worker thread (all SSH I/O).

**`SshBackend`** maintains a single persistent `paramiko` SSH+SFTP session. All SSH work is enqueued into a `PriorityQueue` and executed by one background thread:
- `P_USER = 0` — register writes triggered by the user (highest priority)
- `P_UPLOAD = 1` — file upload and remote compile
- `P_INIT = 2` — connect / disconnect
- `P_POLL = 9` — periodic register reads (lowest priority)

Results flow back to the Qt thread via Qt signals: `sig_connected`, `sig_disconnected`, `sig_status` (parsed JSON dict), `sig_log`, `sig_error`.

**`MainWindow`** owns composition, state, and all signal wiring. Custom widgets: `CyberPanel`, `StatCard`, `DividerControl`, `ParameterSlider`, `WaveformPreview`.

**Key math helpers** (all in the module top-level):
- `hz_to_phase(delta_hz)` / `phase_to_hz(word)` — converts between Hz and the signed 48-bit NCO phase-step offset word.
- `duty_to_cycles(frac, period)` — converts duty fraction to clock cycles, clamped to `[1, period-1]`.

**Auto-Apply** debounces UI changes with a 300 ms `QTimer` before sending. `Ctrl+Return` applies immediately.

### Board-side helper (`rp_pulse_ctl.c`)

Invoked as: `rp_pulse_ctl <base_addr> read|write|soft_reset [args…]`

Outputs a single JSON line on stdout that the GUI parses. The 48-bit NCO registers are split across two 32-bit AXI words (lo/hi); `wr48` writes hi first so the FPGA latches a consistent value from lo.

### FPGA register map (base `0x40600000`)

| Offset | Register | Notes |
|--------|----------|-------|
| `0x00` | `control` | bit 0 = output enable; bit 1 = soft reset (self-clearing strobe) |
| `0x08` | `width` | pulse width in 125 MHz clock cycles |
| `0x10` | `status` | bit 0 busy, bit 1 period_valid, bit 2 period_stable, bit 3 timeout, bit 4 freerun_active |
| `0x14` | `raw_period` | last measured input period (cycles) |
| `0x18` | `period_avg` | IIR-filtered input period (cycles) |
| `0x1C/0x20` | `phase_step_offset` lo/hi | signed 48-bit NCO frequency offset word |
| `0x24/0x28` | `phase_step_base` lo/hi | computed base phase step, read-only |
| `0x2C/0x30` | `phase_step` lo/hi | live phase step, read-only |

Key conversions:
```
input_hz         = 125_000_000 / period_avg
frequency_offset = phase_step_offset * 125_000_000 / 2^48   (NCO resolution ≈ 0.44 mHz/LSB)
width_cycles     = round(width_fraction * period_avg)
```

### CLI monitor (`redpitaya_register_monitor.py`)

Shells out to the system `ssh` binary (not paramiko). Requires `/root/rp_pulse_ctl` to exist on the board. Use `--interval` and `--count` to control polling.

## Development Guidance

- Keep all SSH/SCP/polling work off the Qt main thread — use `SshBackend` and signals.
- Width and delay in the preview are always referenced to the input period; do not change this unless asked.
- Keep backend semantics identical between the Qt and Tk apps.
- Remote commands must be non-interactive and use full binary paths.
- The bitfile `red_pitaya_top.bit.bin` is not tracked in git; it comes from Releases.

## What Not To Do

- Do not re-add PLL-era files (`rp_pll.c`, old `gui/` scripts, old `deploy.sh`).
- Do not introduce non-Qt GUI packages in the new app.
- Do not commit `__pycache__/`, `.DS_Store`, logs, or the bitfile.
- Do not hardcode interactive SSH flows.
