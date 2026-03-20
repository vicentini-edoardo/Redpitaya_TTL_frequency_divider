# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the GUI

```bash
python3 redpitaya_pulse_gui_c_helper.py
```

Requires Python 3 with `tkinter` (standard library). No external dependencies.

## Architecture

This is a single-file tkinter GUI (`redpitaya_pulse_gui_c_helper.py`) that controls a Red Pitaya FPGA board's frequency divider/pulse generator over SSH.

**Two-layer design:**

- `RemoteCtl` — SSH transport. Connects to the board and executes a pre-compiled C binary (`/root/rp_pulse_ctl`) on the remote side. All commands go through this binary, which returns JSON. The binary is **not** part of this repo and must be compiled separately on the Red Pitaya.
- `App` — tkinter UI. Manages three hardware parameters: **divider** (1–32), **width** (cycles), and **delay** (cycles). Slider and entry box are kept in sync via `updating_widgets` guard flag to prevent feedback loops.

**Hardware constants:**
- FPGA clock: 125 MHz (`CLOCK_HZ`)
- Default AXI base address: `0x40600000` (`BASE_ADDR`)
- Divider range: 1–32; width/delay lower bound: 1 cycle

**Control flow for writes:**
`App.apply_now()` → `RemoteCtl.helper(base_addr, "write", divider, width, delay, enable)` → SSH → `/root/rp_pulse_ctl <base_addr> write <args>` → JSON response → `_update_readback()`

**Limit recomputation:** `recompute_limits()` derives max width/delay from the approximate input frequency (user-supplied in kHz) and current divider setting, then updates slider bounds. Called whenever divider or input frequency changes.

**Remote binary interface** (expected by `RemoteCtl.helper`):
- `rp_pulse_ctl <base_addr> read` → JSON with keys: `control`, `divider`, `width`, `delay`, `status`
- `rp_pulse_ctl <base_addr> write <divider> <width> <delay> <enable>` → same JSON
- `rp_pulse_ctl <base_addr> soft_reset` → same JSON
