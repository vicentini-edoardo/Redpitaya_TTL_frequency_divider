# CLAUDE.md — AI Assistant Guide for Redpitaya_TTL_shifter

## Project Overview

This project implements a software **Phase-Locked Loop (PLL)** for the
**Red Pitaya STEMlab 125-14** board. It has two main components:

1. **`rp_pll.c`** — C program that runs directly on the Red Pitaya board,
   performs signal acquisition, PLL control, and exposes a TCP server.
2. **`gui/rp_gui.py`** — Python tkinter GUI that runs on a PC, connects over
   TCP/IP, sends control commands, and plots live telemetry.

---

## Repository Structure

```
rp_pll/
├── CLAUDE.md           # This file
├── README.md           # User-facing setup, wiring, and tuning guide
├── rp_pll.c            # C PLL program (runs on Red Pitaya ARM board)
├── Makefile            # Compiles rp_pll.c on the board (gcc -lrp -lm -lpthread)
├── deploy.sh           # Copies source to board via scp, compiles remotely via ssh
└── gui/
    └── rp_gui.py       # Python 3 tkinter GUI (runs on PC, zero extra dependencies)
```

---

## Hardware Context

| Parameter        | Value                                      |
|------------------|--------------------------------------------|
| Board            | Red Pitaya STEMlab 125-14 v1.0             |
| OS               | Debian Linux on ARM Cortex-A9              |
| ADC              | 125 MSPS, 14-bit                           |
| Decimation       | RP_DEC_8 → effective 15.625 MSPS           |
| Buffer size      | 16384 samples                              |
| Input signal     | TTL square wave ~15 kHz on IN1             |
| Output signal    | TTL-compatible square wave on OUT1 (±1V)   |

The `rp.h` library is **only available on the board** at
`/opt/redpitaya/include`. The C code **cannot be compiled on a PC**.

---

## C Program (`rp_pll.c`)

### Build

```bash
# On the board:
make
# or manually:
gcc -O2 -Wall -I/opt/redpitaya/include -o rp_pll rp_pll.c -lrp -lm -lpthread
```

### Run

```bash
./rp_pll [phase_deg] [duty_cycle] [tcp_port]
# example:
./rp_pll 90 0.3 5555
```

### Key Algorithms and Constants

| Symbol      | Value | Role                                              |
|-------------|-------|---------------------------------------------------|
| KP          | 0.3   | Proportional gain of PI controller               |
| KI          | 0.01  | Integral gain of PI controller                   |
| WINDUP_CLAMP| ±45°  | Anti-windup integrator clamp                     |
| alpha       | 0.05  | EMA pre-filter coefficient on measured frequency  |
| THRESHOLD   | 0.1 V | Rising-edge detection threshold                  |
| LOOP_SLEEP  | 5 ms  | Sleep between acquisition buffers                |
| STATUS_INTERVAL | 100 ms | TCP status push interval                  |

### Threading Model

- **Main thread**: PLL acquisition/control loop — reads ADC buffer, detects
  rising edges, measures frequency, runs PI controller, writes output.
- **TCP thread**: accepts one client at a time; reads commands, pushes `STATUS`
  JSON every 100 ms. Uses a `pthread_mutex` to guard the shared status struct
  and `_Atomic` variables for all cross-thread PLL state.

### TCP Protocol (plain text, newline-terminated, port 5555)

Commands (PC → board):

| Command               | Effect                                   |
|-----------------------|------------------------------------------|
| `SET_PHASE <degrees>` | Set phase offset, range −360 to +360     |
| `SET_DUTY <0.0-1.0>`  | Set duty cycle                           |
| `GET_STATUS`          | Request immediate STATUS response        |
| `STOP`                | Stop PLL cleanly and exit                |

Responses (board → PC):

| Response         | Meaning                            |
|------------------|------------------------------------|
| `OK`             | Command acknowledged               |
| `ERR <message>`  | Command failed                     |
| `STATUS <json>`  | Pushed automatically every 100 ms  |

Status JSON fields: `freq`, `phase_target`, `phase_applied`, `phase_error`,
`duty`, `locked`, `uptime_s`.

### Error output

All errors and warnings go to **stderr only**. stdout is intentionally quiet
(used only during startup for informational messages if needed, otherwise silent).

---

## Python GUI (`gui/rp_gui.py`)

### Requirements

- **Python 3** with **standard library only** (`tkinter`, `socket`, `threading`,
  `json`, `time`, `collections`).
- **No pip installs required.** Must run on Windows, macOS, and Linux.

### Run

```bash
python3 gui/rp_gui.py
```

### Key Design Points

- Dark theme with industrial/instrument aesthetic.
- Phase error colour coding: green < 2°, orange < 5°, red ≥ 5°.
- Rolling 10-second phase-error chart drawn on a `tkinter.Canvas` (no matplotlib).
- Auto-reconnect if TCP connection drops.
- Controls: phase slider (−360° to +360°, 0.1° steps), duty slider (1–99%,
  0.1% steps), IP/port fields, Connect/Disconnect button.

---

## Deployment (`deploy.sh`)

```bash
./deploy.sh rp-xxxxxx.local   # or use the board IP address
```

What it does:
1. `scp rp_pll.c Makefile root@<ip>:/root/rp_pll/`
2. `ssh root@<ip> "cd /root/rp_pll && make"`
3. Prints success or error.

---

## Development Conventions

### C Code

- Standard: C11 (`-std=c11` implied by `_Atomic` usage).
- Use `_Atomic double` / `_Atomic int` / `_Atomic bool` for all shared PLL
  state (no mutex needed for individual reads/writes to atomics).
- Use `pthread_mutex_t` only to guard the composite status struct when building
  a consistent snapshot for TCP.
- Keep error messages short, prefixed with the function name, on stderr.
- No dynamic memory allocation after startup (avoid malloc in the hot loop).

### Python Code

- Target Python 3.8+ for maximum OS compatibility.
- No f-strings that require 3.8+ features beyond basic use — keep compatible.
- All GUI updates must happen on the main (tkinter) thread; use
  `widget.after(0, callback)` to marshal from background threads.
- The TCP receive loop runs in a daemon thread.

### Git Conventions

- Commit messages: imperative mood, short subject line (≤ 72 chars).
- Branch naming: `claude/<short-description>-<id>` for AI-assisted work.
- Do not commit build artifacts (`rp_pll` binary, `__pycache__`).

---

## Common Tasks for AI Assistants

| Task                             | Files to touch                    |
|----------------------------------|-----------------------------------|
| Tune PLL gains                   | `rp_pll.c` (KP, KI constants)     |
| Change TCP port default          | `rp_pll.c`, `gui/rp_gui.py`       |
| Add a new TCP command            | `rp_pll.c` (parse + handle), `gui/rp_gui.py` (send) |
| Adjust GUI layout / colours      | `gui/rp_gui.py`                   |
| Extend status JSON               | `rp_pll.c` (build JSON), `gui/rp_gui.py` (parse + display) |
| Change ADC decimation            | `rp_pll.c` (RP_DEC_* constant)    |
| Update deploy target path        | `deploy.sh`                       |

---

## What NOT to Do

- Do not try to compile `rp_pll.c` locally — `rp.h` is board-only.
- Do not add Python dependencies beyond the standard library.
- Do not use `global` mutable state in the Python GUI; use instance attributes
  on the `App` class.
- Do not busy-wait in the TCP thread; use `select` or blocking `recv` with a
  timeout.
- Do not remove the phase-error wrap to `[-180, +180]` — it is essential for
  the PI controller to take the shortest path when correcting phase.
