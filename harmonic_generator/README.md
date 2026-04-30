# harmonic_generator

Red Pitaya FPGA harmonic generator: produces a 50% duty-cycle TTL square wave at

```
f_out = N × f_input + f_shift
```

where **N** is 1–5 (selectable in the GUI) and **f_shift** is a signed NCO frequency offset.

Signal path: `DIO0_P` → FPGA period measurement + harmonic NCO → `DIO1_P`.

## Files

| File | Description |
|------|-------------|
| `redpitaya_harmonic_gui_qt.py` | PySide6 desktop GUI |
| `redpitaya_register_monitor.py` | CLI live register monitor |
| `rp_harmonic_ctl.c` | Board-side C helper (`/root/rp_harmonic_ctl`) |
| `Vivado files/axi4lite_pulse_regs.sv` | AXI4-Lite register bank |
| `Vivado files/pulse_gen.sv` | Reciprocal counter + harmonic NCO |
| `Vivado files/red_pitaya_top.sv` | Top-level Red Pitaya integration |

## Quick start

### 1. Build the FPGA bitstream (Vivado)

Open the existing Red Pitaya Vivado project, replace the three `.sv` files in
`Vivado files/` with these, re-synthesize and implement, then export the bitstream:

```bash
cp harmonic_generator/Vivado\ files/*.sv <vivado_project>/src/
# synthesize & implement in Vivado...
cp <output>/red_pitaya_top.bit.bin harmonic_generator/
```

### 2. Install host dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r harmonic_generator/requirements.txt
```

### 3. Compile the board-side helper

```bash
scp harmonic_generator/rp_harmonic_ctl.c root@rp-xxxxxx.local:/root/
ssh root@rp-xxxxxx.local 'gcc -O2 -o /root/rp_harmonic_ctl /root/rp_harmonic_ctl.c'
```

Or use the **Upload & Compile** button in the GUI (it will also load the bitstream if
`red_pitaya_top.bit.bin` is present next to the GUI script).

### 4. Run the GUI

```bash
.venv/bin/python harmonic_generator/redpitaya_harmonic_gui_qt.py
```

### 5. Run the CLI monitor

```bash
python3 harmonic_generator/redpitaya_register_monitor.py --host rp-xxxxxx.local --interval 0.5 --count 20
```

## Register map

Base address: `0x40600000`

| Offset | Register | Notes |
|--------|----------|-------|
| `0x00` | `control` | bit 0 = output enable, bit 1 = soft reset strobe |
| `0x08` | `mult_n` | harmonic order [1..5] (3-bit, clamped by FPGA) |
| `0x10` | `status` | bit 0=busy, bit 1=period_valid, bit 2=period_stable, bit 3=timeout, bit 4=freerun |
| `0x14` | `raw_period` | last measured input period in cycles |
| `0x18` | `period_avg` | reciprocal-counted period in cycles |
| `0x1C/0x20` | `phase_step_offset` | signed 48-bit NCO offset |
| `0x24/0x28` | `phase_step_base` | `2^48 / period_avg` (read-only) |
| `0x2C/0x30` | `phase_step` | live `N·base + offset` (read-only) |
| `0x34` | `meas_time_us` | measurement window in µs (min 1000) |

## Key formulas

```
input_hz   = 125_000_000 / period_avg
output_hz  = N × input_hz + phase_step_offset × 125_000_000 / 2^48
NCO res    ≈ 0.44 mHz / LSB
```

Output duty cycle is exactly **50%** by construction (MSB of the 48-bit NCO accumulator).
