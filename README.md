# Red Pitaya TTL Frequency Generator

Desktop tools for controlling a custom Red Pitaya FPGA TTL signal generator over SSH.

Two FPGA modes are supported by a **single unified FPGA bitfile** and a **single
board-side C helper** (`rp_ctl.c`). The helper detects its operating mode from the
binary name it is called as.

| Mode | Formula | Duty cycle |
|------|---------|------------|
| **Pulse / Freq-Shift** | f_out = f_in + f_shift | User-adjustable |
| **Harmonic Generator** | f_out = N × f_in + f_shift | Fixed 50 % |

Both modes run from the same bitstream. Switching modes is instant — no re-flashing
needed.

Signal path for both modes:

```text
External TTL signal → DIO0_P → FPGA (period measurement + NCO) → DIO1_P → TTL output
                                         ^
                                         |
                            SSH/SFTP from the desktop GUI
```

---

## GUI

![GUI screenshot](GUI.png)

---

## Project layout

```
Redpitaya_TTL_frequency_divider/
├── redpitaya_combined_gui_qt.py   # Two-tab PySide6 GUI (both modes, one SSH session)
├── rp_math.py                     # Qt-free math helpers (frequency/duty conversion)
├── rp_ctl.c                       # Unified board-side C helper (pulse + harmonic)
├── red_pitaya_top.bit.bin         # Unified FPGA bitstream
├── requirements.txt               # Python dependencies (PySide6, paramiko)
├── launch_gui.vbs                 # Windows double-click launcher
├── tests/
│   └── test_rp_math.py            # Unit tests for rp_math
└── Vivado files/                  # RTL source files
    ├── red_pitaya_top.sv
    ├── pulse_gen.sv
    └── axi4lite_pulse_regs.sv
```

---

## Installation

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

On Windows, double-click `launch_gui.vbs` after installing Python 3.11.

---

## Running the GUI

```bash
.venv/bin/python redpitaya_combined_gui_qt.py
```

Select the **Pulse / Freq-Shift** or **Harmonic Generator** tab.
Click **Upload & Compile** on either tab to upload `rp_ctl.c`, compile it on the
board, and flash the bitfile — this is needed once per board or after a firmware update.
Switching between tabs changes the active mode instantly with no re-flashing.

---

## Running the tests

```bash
python3 -m unittest discover -s tests
# or with pytest:
pytest tests/
```

## Hardware verification with PicoSDK

A PicoScope 4000A-family card can be used to validate the Red Pitaya output
directly from captured input/output waveforms:

```bash
python3 -m pip install -r requirements-picosdk.txt
python3 hardware_tests/redpitaya_picosdk_verify.py --host rp-xxxxxx.local
```

See [`docs/redpitaya_picosdk_hardware_tests.md`](docs/redpitaya_picosdk_hardware_tests.md)
for wiring, PicoSDK setup, run commands, and how to send the generated debug
bundle for analysis.

---

## Output modes

Each tab has three output mode buttons:

| Button | Effect |
|--------|--------|
| **■ LASER OFF** | Forces output LOW (constant 0). NCO stops. |
| **~ MODULATED** | Normal NCO operation — f_out follows the configured formula. |
| **● LASER ON** | Forces output HIGH (constant 1), overriding the NCO. |

The hardware control register is polled continuously; the GUI always reflects the
current board state.

---

## Board-side helper

`rp_ctl.c` compiles to a single binary that serves both modes. It is installed as
`/root/rp_ctl` and symlinked to both `/root/rp_pulse_ctl` and `/root/rp_harmonic_ctl`.
The binary detects its mode from the name it is called as.

### Manual install

```bash
# Upload and compile
scp rp_ctl.c root@rp-xxxxxx.local:/root/rp_ctl.c
ssh root@rp-xxxxxx.local 'gcc -O2 -o /root/rp_ctl /root/rp_ctl.c && \
    ln -sf /root/rp_ctl /root/rp_pulse_ctl && \
    ln -sf /root/rp_ctl /root/rp_harmonic_ctl'

# Flash the bitfile (needed once per board boot)
scp red_pitaya_top.bit.bin root@rp-xxxxxx.local:/root/
ssh root@rp-xxxxxx.local '/opt/redpitaya/bin/fpgautil -b /root/red_pitaya_top.bit.bin'
```

### Subcommands (same for both modes)

```bash
# Pulse mode
ssh root@rp-xxxxxx.local '/root/rp_pulse_ctl 0x40600000 read'
ssh root@rp-xxxxxx.local '/root/rp_pulse_ctl 0x40600000 write <width_cycles> <phase_step_offset> <control>'
ssh root@rp-xxxxxx.local '/root/rp_pulse_ctl 0x40600000 control 0'   # Laser OFF
ssh root@rp-xxxxxx.local '/root/rp_pulse_ctl 0x40600000 control 4'   # Laser ON
ssh root@rp-xxxxxx.local '/root/rp_pulse_ctl 0x40600000 trig <trig_phase_step>'

# Harmonic mode
ssh root@rp-xxxxxx.local '/root/rp_harmonic_ctl 0x40600000 read'
ssh root@rp-xxxxxx.local '/root/rp_harmonic_ctl 0x40600000 write <mult_n> <phase_step_offset> <control>'
ssh root@rp-xxxxxx.local '/root/rp_harmonic_ctl 0x40600000 control 0'   # Laser OFF
ssh root@rp-xxxxxx.local '/root/rp_harmonic_ctl 0x40600000 control 4'   # Laser ON
```

---

## FPGA register map

Base address: `0x40600000`

| Offset | Register | Notes |
|--------|----------|-------|
| `0x00` | `control` | See bits below |
| `0x04/0x0C` | `trig_phase_step` | DIO2 48-bit NCO phase step (0 = off) |
| `0x08` | `width_n` / `mult_n` | Pulse width in clock cycles (pulse) or harmonic order 1..5 (harmonic) |
| `0x10` | `status` | bit 0=busy, bit 1=period_valid, bit 2=period_stable, bit 3=timeout, bit 4=freerun_active |
| `0x14` | `meas_span` | Clock cycles between first and last rising edge of last window |
| `0x18` | `edge_cnt` | Rising-edge count from last window; f_in = CLK_HZ·(edge_cnt−1)/meas_span |
| `0x1C/0x20` | `phase_step_offset` | Signed 48-bit NCO frequency offset |
| `0x24/0x28` | `phase_step_base` | Computed base step (read-only) |
| `0x2C/0x30` | `phase_step` | Live `[N·]base + offset` (read-only) |
| `0x34` | `meas_time_us` | Measurement window in µs (min 1000) |
| `0x38` | `osc_half_period` | Clock ticks per half-oscillation (osc mode) |
| `0x3C/0x40` | `osc_phase_preload` | 48-bit accumulator preload (osc mode) |

### control register bits

| Bit | Name | Function |
|-----|------|----------|
| 0 | enable | Start/stop the NCO and period measurement |
| 1 | soft_reset | Self-clearing reset; clears the NCO and restarts measurement |
| 2 | force_high | Override output HIGH regardless of NCO state (LASER ON) |
| 3 | harmonic_mode | 0 = pulse mode, 1 = harmonic mode (set by binary name) |
| 4 | osc_mode | Oscillating delay mode (phase sweep P0±P, pulse mode only) |
| 5 | edge_lock | Anchor NCO phase to input rising edges: f_out − [N·]f_in is exactly f_shift, beat coherent indefinitely |

---

## Hardware assumptions

- Board: Red Pitaya STEMlab 125-14 or compatible 125 MHz target.
- FPGA clock: ~125 MHz (measured: 124,999,999 Hz).
- Input TTL signal: `DIO0_P` / `GND`.
- Output TTL signal: `DIO1_P` / `GND`.
- Free-running square wave (optional): `DIO2_P` / `GND`.
- SSH access as `root`; `gcc` available on the board.
- `/opt/redpitaya/bin/fpgautil` available for bitstream loading.

---

## Connecting

1. Enter the Red Pitaya hostname (e.g. `rp-xxxxxx.local`), port `22`, user `root`.
2. Optionally select an SSH private-key file.
3. Click **Connect**.
4. Click **Upload & Compile** on the desired tab if the board helpers are not yet installed.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| PySide6 import error | `pip install -r requirements.txt` |
| Connect fails | Check hostname/IP, SSH credentials, and board reachability. |
| Register reads fail | Re-run **Upload & Compile** to reinstall the C helper. |
| FPGA image does not load | Confirm `fpgautil` exists on the board and the `.bit.bin` matches the board OS. |
| Input frequency shows `---` | Verify TTL input on `DIO0_P` and shared ground. |
| Output stuck LOW after mode switch | Click **~ MODULATED** to re-enable the NCO. |

---

## License

MIT — see [LICENSE](LICENSE).
