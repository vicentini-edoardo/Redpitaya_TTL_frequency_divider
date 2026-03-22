# CLAUDE.md — AI Assistant Guide for Redpitaya_TTL_shifter

## Project Overview

This project implements a software **Phase-Locked Loop (PLL)** for the
**Red Pitaya STEMlab 125-14** board. It has two main components:

1. **`rp_pll.c`** — C++ program that runs directly on the Red Pitaya board,
   performs signal acquisition, PLL control, and exposes a TCP server.
2. **`gui/rp_gui.py`** — Python tkinter GUI that runs on a PC, connects over
   TCP/IP, sends control commands, and plots live telemetry.

---

## Repository Structure

```
rp_pll/
├── CLAUDE.md           # This file
├── README.md           # User-facing setup, wiring, and tuning guide
├── rp_pll.c            # C++ PLL program (runs on Red Pitaya ARM board)
├── Makefile            # Compiles rp_pll.c on the board (g++ -lrp -lm -lpthread)
├── deploy.sh           # Copies source to board via scp, compiles remotely via ssh
└── gui/
    ├── rp_gui.py       # Python 3 tkinter GUI (runs on PC, zero extra dependencies)
    └── scope_gui.py    # Oscilloscope view GUI (IN1 + OUT1 traces)
```

---

## Hardware Context

| Parameter        | Value                                      |
|------------------|--------------------------------------------|
| Board            | Red Pitaya STEMlab 125-14 v1.0             |
| Firmware         | 2.07                                       |
| OS               | Ubuntu 22.04 on ARM Cortex-A9              |
| ADC              | 125 MSPS, 14-bit                           |
| Decimation       | RP_DEC_1024 → effective 122 kSPS (~1074 cycles/buf at 8 kHz) |
| Buffer size      | 16384 samples                              |
| Input signal     | TTL square wave ~8 kHz on IN1              |
| Output signal    | PWM square wave on OUT1 (±1V)              |

The `rp.h` library is **only available on the board** at `/boot/include`.
The C++ code **cannot be compiled on a PC**.

---

## Firmware 2.07 API Changes (from 0.98)

| Item | Old (0.98) | New (2.07) |
|------|-----------|-----------|
| Include path | `/boot/include/redpitaya/rp.h` | `/boot/include/rp.h` |
| Compiler | `gcc` (C11) | `g++ -std=c++20` (C++20, required by `rp_acq_axi.h`) |
| Atomics | `_Atomic double` / `stdatomic.h` | `std::atomic<double>` / `<atomic>` |
| Trigger level channel | `RP_CH_1` | `RP_T_CH_1` (`rp_channel_trigger_t`) |
| Waveform for PWM | `RP_WAVEFORM_SQUARE` | `RP_WAVEFORM_PWM` |
| Decimation values | Only 1,8,64,1024,8192,65536 | Full power-of-2 range 1–65536 |
| FPGA load | `cat fpga_0.94.bit > /dev/xdevcfg` | `/opt/redpitaya/sbin/overlay.sh v0.94` |
| Library path | `/boot/lib/librp.so` | `/boot/lib/librp.so` (unchanged) |

---

## C++ Program (`rp_pll.c`)

### Build

```bash
# On the board:
make
# or manually:
g++ -O2 -Wall -std=c++20 -I/boot/include -o rp_pll rp_pll.c -L/boot/lib -Wl,-rpath,/boot/lib -lrp -lm -lpthread
```

### Run

```bash
# Load FPGA first (required before each run):
/opt/redpitaya/sbin/overlay.sh v0.94

# Then run:
./rp_pll [phase_deg] [duty_cycle] [tcp_port]
# example:
./rp_pll 0 0.1 5555

# Frequency stability diagnostic (no generator/TCP):
./rp_pll --test-freq [duration_s]
./rp_pll --test-freq 10
```

### Key Algorithms and Constants

| Symbol        | Value      | Role                                              |
|---------------|------------|---------------------------------------------------|
| KP            | 0.3        | Proportional gain of PI controller               |
| KI            | 0.01       | Integral gain of PI controller                   |
| WINDUP_CLAMP  | ±45°       | Anti-windup integrator clamp                     |
| MEDIAN_WIN    | 9          | Median filter window for frequency measurement   |
| THRESHOLD_V   | 0.1 V      | Rising-edge detection threshold                  |
| LOOP_SLEEP_MS | 5 ms       | Sleep between acquisition buffers                |
| STATUS_INTERVAL | 100 ms   | TCP status push interval                         |
| DECIMATION    | RP_DEC_1024 | 122 kSPS — ~1074 cycles/buf at 8 kHz            |

### Frequency Measurement

- Adaptive hysteresis: `vmid = (vmax+vmin)/2`, `hyst = (vmax-vmin)*0.3`
- Sub-sample linear interpolation for edge timing
- Median filter (window=9) replaces EMA — reduces white noise by ~3×
- With DEC_1024: std ≈ 0.8 Hz (103 ppm) vs 61 Hz (7570 ppm) with DEC_64

### Threading Model

- **Main thread**: PLL acquisition/control loop — reads ADC buffer, detects
  rising edges, measures frequency, runs PI controller, writes output.
- **TCP thread**: accepts one client at a time; reads commands, pushes `STATUS`
  JSON every 100 ms. Uses a `pthread_mutex` to guard the shared status struct
  and `std::atomic<>` for all cross-thread PLL state.

### TCP Protocol (plain text, newline-terminated, port 5555)

Commands (PC → board):

| Command               | Effect                                   |
|-----------------------|------------------------------------------|
| `SET_PHASE <degrees>` | Set phase offset, range −360 to +360     |
| `SET_DUTY <0.0-1.0>`  | Set duty cycle                           |
| `GET_STATUS`          | Request immediate STATUS response        |
| `GET_SCOPE`           | Request scope snapshot (SCOPE response)  |
| `STOP`                | Stop PLL cleanly and exit                |

Responses (board → PC):

| Response         | Meaning                            |
|------------------|------------------------------------|
| `OK`             | Command acknowledged               |
| `ERR <message>`  | Command failed                     |
| `STATUS <json>`  | Pushed automatically every 100 ms  |
| `SCOPE <json>`   | Scope snapshot on request          |

Status JSON fields: `freq`, `phase_target`, `phase_applied`, `phase_error`,
`duty`, `locked`, `uptime_s`.

### Error output

All errors and warnings go to **stderr only**.

---

## Python GUI (`gui/rp_gui.py`)

### Requirements

- **Python 3** with **standard library only** (`tkinter`, `socket`, `threading`,
  `json`, `time`, `collections`).
- **No pip installs required.** Must run on Windows, macOS, and Linux.

### Run

```bash
python3 gui/rp_gui.py
python3 gui/scope_gui.py   # oscilloscope view
```

---

## Deployment (`deploy.sh`)

```bash
./deploy.sh rp-xxxxxx.local   # or use the board IP address
```

What it does:
1. `scp rp_pll.c Makefile root@<ip>:/root/rp_pll/`
2. `ssh root@<ip> "cd /root/rp_pll && make"`
3. Prints success or error.

After deployment, start with:
```bash
ssh root@rp-xxxxxx.local '/opt/redpitaya/sbin/overlay.sh v0.94; sleep 1; nohup /root/rp_pll/rp_pll 0 0.1 5555 >/tmp/pll.out 2>/tmp/pll.err &'
```

---

## Development Conventions

### C++ Code

- Standard: C++20 (required by `rp_acq_axi.h` which uses `std::span`).
- Use `std::atomic<double>` / `std::atomic<bool>` for all shared PLL state.
- Use `pthread_mutex_t` only to guard the composite status struct.
- Keep error messages short, prefixed with the function name, on stderr.
- No dynamic memory allocation after startup (avoid malloc in the hot loop).
- Cast all `malloc()` return values (C++ requires explicit cast from `void*`).
- Use `memset` + field assignment instead of C99 designated initialisers.

### Python Code

- Target Python 3.8+ for maximum OS compatibility.
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
| Change ADC decimation            | `rp_pll.c` (RP_DEC_* + SAMPLE_RATE_HZ) |
| Update deploy target path        | `deploy.sh`                       |

---

## What NOT to Do

- Do not try to compile `rp_pll.c` locally — `rp.h` is board-only.
- Do not use `gcc` — must use `g++ -std=c++20` due to `rp_acq_axi.h`.
- Do not use `_Atomic` or `stdatomic.h` — use `std::atomic<>` instead.
- Do not use C99 designated initialisers (`{.field = val}`) — not valid in C++.
- Do not add Python dependencies beyond the standard library.
- Do not use `global` mutable state in the Python GUI; use instance attributes
  on the `App` class.
- Do not busy-wait in the TCP thread; use `select` or blocking `recv` with a
  timeout.
- Do not remove the phase-error wrap to `[-180, +180]` — it is essential for
  the PI controller to take the shortest path when correcting phase.
- Do not load the FPGA with `cat > /dev/xdevcfg` — use `overlay.sh v0.94`.
