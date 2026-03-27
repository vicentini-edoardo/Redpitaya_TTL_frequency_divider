# Red Pitaya Frequency Divider — Control GUI

A Python desktop GUI to control a custom frequency divider/pulse generator implemented on the Red Pitaya FPGA (STEMlab 125-14). The FPGA core measures the period of an external input signal, divides its frequency, and outputs a configurable pulse with controllable width and phase delay. It also supports a trigger-sampled phase modulation mode where the output delay follows a sinusoid over one full trigger period.

The preferred GUI is now the PySide6 version in `redpitaya_pulse_gui_qt.py`. The original Tkinter app is still kept in the repository as a legacy fallback during the transition.

![GUI screenshot](GUI.png)

```
External signal ──► DIO0_P ──► [FPGA: freq divider + pulse gen] ──► DIO1_P ──► Output pulse
                                              ▲
                                     SSH (this GUI)
```

## Hardware

- **Board:** Red Pitaya STEMlab 125-14
- **Tested OS:** Red Pitaya OS 2.07-48
- **FPGA clock:** 125 MHz
- **Input:** pin `DIO0_P` / `GND` (E2 connector)
- **Output:** pin `DIO1_P` / `GND` (E2 connector)
- **Input frequency range:** 1 Hz – 300 kHz
- **Divider range:** 1–32

### Register map (AXI base `0x40600000`)

| Offset | Register        | Description                                          |
|--------|-----------------|------------------------------------------------------|
| 0x00   | control         | Bit 0 = output enable, Bit 1 = soft reset, Bit 2 = phase modulation enable |
| 0x04   | divider         | Frequency divider value (1–32)                       |
| 0x08   | pulse width     | Pulse width in 125 MHz clock cycles                  |
| 0x0C   | delay           | Pulse delay in 125 MHz clock cycles                  |
| 0x10   | status          | Bit 0 = busy, Bit 1 = period_valid, Bit 2 = timeout  |
| 0x14   | period          | Last raw measured input period (cycles)              |
| 0x18   | period_avg      | Averaged trigger period (cycles)                     |
| 0x1C   | phase_freq      | DDS phase increment word for modulation frequency    |
| 0x20   | phase_amp_q15   | Q15 amplitude word for modulation sweep amplitude    |

Input frequency is measured directly from the hardware — no manual entry needed.
Frequency from period: `freq_hz = 125_000_000 / period_cycles`
Modulation word from frequency: `phase_freq_word = int(f_mod * 2**32 / 125e6)`
Amplitude word from sweep fraction: `phase_mod_amp_q15 = int(sweep_fraction * 32767)`

## Repository Contents

| File | Description |
|------|-------------|
| `redpitaya_pulse_gui_qt.py` | Preferred PySide6 GUI — run on your PC |
| `redpitaya_pulse_gui_c_helper.py` | Desktop GUI — run on your PC |
| `rp_pulse_ctl.c` | C helper binary — compiled on the board via the GUI |
| `red_pitaya_top.bit.bin` | FPGA bitfile — download from [Releases](../../releases) and place next to the GUI script |

## Requirements

**PC:**
- Python 3.10+
- `PySide6-Essentials` for the preferred GUI
- `tkinter` only if you want to run the legacy fallback GUI
- OpenSSH client (`ssh`, `scp`)
- macOS, Linux, or Windows with OpenSSH

**Red Pitaya:**
- OS 2.07-48 (other versions may work)
- SSH access as `root`
- `gcc` available on the board
- `fpgautil` at `/opt/redpitaya/bin/fpgautil`

## Getting Started

### 1. Find your board hostname

Each Red Pitaya has a unique hostname printed on the board sticker, in the form `rp-XXXXXX.local`. You can also find it by scanning your network or connecting via the Red Pitaya web interface. Update the **Host** field in the GUI accordingly.

### 2. Install the preferred GUI dependency

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install PySide6-Essentials
```

### 3. Run the GUI

```bash
.venv/bin/python redpitaya_pulse_gui_qt.py
```

Legacy fallback:

```bash
python3 redpitaya_pulse_gui_c_helper.py
```

### 4. First-time setup

If the C helper binary is not yet on the board, click **Upload & compile** — this copies `rp_pulse_ctl.c` via SCP and compiles it on the board with `gcc`.

If the FPGA bitfile needs updating, click **Upload bitfile** — this copies `red_pitaya_top.bit.bin` via SCP and reloads the FPGA with `fpgautil`.

### 5. Connect

Enter your board hostname and click **Connect**. This will:
1. Load the FPGA bitfile via `fpgautil`
2. Read back the current register state

Port, user, and base address can be changed under **▼ Advanced**.

### 6. Control parameters

| Parameter | Unit | Description |
|-----------|------|-------------|
| Divider   | integer 1–32  | Divides the input frequency |
| Width     | duty cycle 0–1 | Pulse width as fraction of the **input** period |
| Delay     | phase 0–180°  | Pulse delay as phase of the **input** period |
| Phase modulation | on/off | When enabled, delay is driven by an internal DDS-synthesized sinusoid |
| Modulation frequency | Hz, 0–5 kHz | Converted to `PHASE_FREQ`; DIO2 outputs a 50% duty TTL at the same frequency |
| Modulation amplitude | sweep / T, 0–1 | Converted to `PHASE_MOD_AMP_Q15`; practical values: `0.7T = 22937`, `0.8T = 26214`, full sweep `32767` |

The muted label next to each slider shows the equivalent absolute time (e.g. `4 us`).

The input frequency is read automatically from hardware and updated when it changes by more than 5%. Use **Force freq update** to apply the current hardware measurement immediately. A red warning is shown if no input signal is detected.

When phase modulation is active, the static `pulse_delay` register is ignored and the GUI greys out the delay control. If `period_valid = 0` or `timeout_flag = 1`, the GUI disables phase modulation and falls back to the static delay path.

### 7. Apply changes

| Button / control | Action |
|-----------------|--------|
| **Apply now** | Sends current values to hardware immediately |
| **Auto apply** | Sends values 300 ms after any slider/entry change |
| **Read registers** | Reads back hardware state without writing |
| **Soft reset** | Pulses the reset bit on the FPGA core |

Typical write order used by the GUI:
1. Set divider, width, delay, and modulation frequency
2. Write `PHASE_FREQ`
3. Enable pulse output
4. Enable phase modulation when trigger period is valid

## Architecture

Preferred PySide6 app:

```text
redpitaya_pulse_gui_qt.py
├── RemoteCtl                 SSH/SCP transport layer
├── MainWindow                Qt dashboard composition + state
├── CyberPanel                Custom painted section frame
├── StatCard                  Live stat tile
├── DividerControl            Stepped divider control
├── ParameterSlider           Custom width/delay slider control
├── WaveformPreview           Custom painted waveform preview
└── FunctionWorker            QThreadPool job wrapper for async backend calls
```

Legacy fallback:

```text
redpitaya_pulse_gui_c_helper.py
└── Original tkinter implementation
```

**Unit conversions** (all referenced to the input period, not the divided period):
- Width: `duty × input_period_cycles` → hardware cycles
- Delay: `(deg / 360) × input_period_cycles` → hardware cycles, clamped to half period

## License

MIT — see [LICENSE](LICENSE).
