# Red Pitaya Software PLL

A software Phase-Locked Loop for the **Red Pitaya STEMlab 125-14** that locks
an output square wave to an input TTL signal with a configurable phase offset
and duty cycle. A Python GUI runs on a PC and controls the board over TCP/IP.

---

## Hardware Wiring

### Input (IN1) — TTL to ±1V voltage divider

The Red Pitaya fast analog inputs accept ±1V. A standard 3.3V or 5V TTL signal
must be scaled down with a resistive voltage divider before connecting to IN1.

```
TTL source ──┬── R1 ──┬── IN1
             │        │
             GND     R2
                      │
                     GND
```

| TTL level | R1    | R2    | Output at IN1 |
|-----------|-------|-------|---------------|
| 3.3 V     | 2.3 kΩ | 1 kΩ | ~1.0 V peak  |
| 5.0 V     | 4.0 kΩ | 1 kΩ | ~1.0 V peak  |

Connect the divider output to the SMA connector labeled **IN1** on the board.
Also connect signal ground to the board's GND.

### Output (OUT1) — level shifter note

OUT1 produces a ±1V square wave (2 Vpp). Most TTL/CMOS logic expects 0–3.3V or
0–5V. Use a single-supply comparator (e.g. LM393) or a dedicated level-shift
IC to convert OUT1 to the required logic level.

---

## Project Structure

```
rp_pll/
├── rp_pll.c      # C PLL program — runs on the board
├── Makefile      # Build on the board with gcc -lrp -lm -lpthread
├── deploy.sh     # Copy + compile on board via scp/ssh
├── gui/
│   └── rp_gui.py # Python 3 GUI — runs on PC
└── README.md
```

---

## Deploying to the Board

```bash
./deploy.sh rp-xxxxxx.local
# or with IP address:
./deploy.sh 192.168.1.50
```

The script:
1. Creates `/root/rp_pll/` on the board.
2. Copies `rp_pll.c` and `Makefile` via `scp`.
3. Runs `make` on the board via `ssh`.

> **Note:** `rp.h` is only available on the board. Do not attempt to compile
> `rp_pll.c` on a PC.

---

## Running the C Program on the Board

SSH into the board and run:

```bash
cd /root/rp_pll
./rp_pll [phase_deg] [duty_cycle] [tcp_port]
```

| Argument    | Default | Description                                |
|-------------|---------|--------------------------------------------|
| phase_deg   | 0       | Initial phase offset in degrees (−360–360) |
| duty_cycle  | 0.5     | Initial duty cycle (0.01–0.99)             |
| tcp_port    | 5555    | TCP port for the remote control server     |

Example — 90° phase shift, 30% duty, default port:

```bash
./rp_pll 90 0.3 5555
```

The program outputs nothing to stdout under normal operation. Errors go to
stderr. Stop with `Ctrl+C` or send the `STOP` TCP command.

---

## Running the Python GUI on the PC

```bash
python3 gui/rp_gui.py
```

1. Enter the board's IP address (or hostname) and port.
2. Click **Connect**.
3. Adjust the **Phase Shift** and **Duty Cycle** sliders.
4. Watch live readouts and the rolling phase-error chart.

The GUI uses only the Python standard library (tkinter). No `pip install`
required. Works on Windows, macOS, and Linux.

---

## TCP Protocol Reference

The board listens for plain-text, newline-terminated commands on the configured
TCP port (default 5555). One client is served at a time; the board waits for a
new client if the connection drops.

### Commands (PC → board)

| Command               | Description                                    |
|-----------------------|------------------------------------------------|
| `SET_PHASE <degrees>` | Set phase offset. Range: −360 to +360.         |
| `SET_DUTY <0.0-1.0>`  | Set duty cycle. Range: 0.01 to 0.99.           |
| `GET_STATUS`          | Request an immediate STATUS response.          |
| `STOP`                | Stop the PLL and exit the program cleanly.     |

### Responses (board → PC)

| Response         | Description                                         |
|------------------|-----------------------------------------------------|
| `OK`             | Command accepted.                                   |
| `ERR <message>`  | Command rejected; reason in message.                |
| `STATUS <json>`  | Pushed automatically every 100 ms, and on request. |

### Status JSON

```json
{
  "freq":          15000.12,
  "phase_target":  90.0,
  "phase_applied": 89.8,
  "phase_error":   0.2,
  "duty":          0.3,
  "locked":        true,
  "uptime_s":      42
}
```

| Field           | Type    | Description                                          |
|-----------------|---------|------------------------------------------------------|
| freq            | float   | EMA-filtered input frequency (Hz)                   |
| phase_target    | float   | Requested phase offset (°)                           |
| phase_applied   | float   | Current output phase (°), converges to target        |
| phase_error     | float   | `target − applied`, wrapped to [−180, +180] (°)      |
| duty            | float   | Current duty cycle (0–1)                             |
| locked          | bool    | `true` if rising edges were detected in last buffer  |
| uptime_s        | integer | Seconds since PLL started                            |

---

## PLL Tuning Guide

The PI controller runs in the main acquisition loop (every 5 ms). Default
constants are in `rp_pll.c`:

| Constant         | Default | Effect                                              |
|------------------|---------|-----------------------------------------------------|
| `KP`             | 0.3     | Proportional gain — speed of initial response       |
| `KI`             | 0.01    | Integral gain — eliminates steady-state phase error |
| `WINDUP_CLAMP`   | 45°     | Integrator clamp — prevents integral wind-up        |
| `EMA_ALPHA`      | 0.05    | Frequency pre-filter — smooths noisy freq estimate  |

### If the phase oscillates (hunting)

Reduce `KP` (e.g. 0.1). The loop is over-damped. Optionally increase
`WINDUP_CLAMP` slightly if the integrator saturates too early.

### If phase converges slowly or drifts

Increase `KI` (e.g. 0.05). If `KI` is increased significantly, also reduce
`WINDUP_CLAMP` proportionally to avoid integral wind-up on large step changes.

### If the frequency readout is noisy

Decrease `EMA_ALPHA` (e.g. 0.01) for more smoothing, at the cost of slower
response to actual frequency changes.

### Lock indicator is `false` intermittently

The signal level may be too low after the voltage divider. Check that the peak
voltage reaching IN1 comfortably exceeds the `THRESHOLD_V` constant (0.1 V).
Adjust the divider ratio or change `THRESHOLD_V`.
