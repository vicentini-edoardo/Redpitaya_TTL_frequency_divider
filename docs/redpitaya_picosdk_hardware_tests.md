# Red Pitaya PicoSDK Hardware Verification

This procedure verifies the Red Pitaya TTL frequency-divider/generator with a
PicoScope 4000A-family acquisition card using PicoSDK. The Python harness drives
the Red Pitaya over SSH, captures the input and output waveforms directly from
the PicoScope, analyzes the result, and writes a debug bundle.

## What the test covers

The default suite checks:

- `OFF`: `DIO1_P` is held low.
- `ON`: `DIO1_P` is forced high.
- Pulse / frequency-shift mode: `f_out = f_in + f_shift`, including duty cycle.
- Harmonic mode: `f_out = N × f_in + f_shift`, with 50% duty.
- Oscillating delay mode: output delay sweeps around `P0` with amplitude `P`.
- Optional `DIO2_P`: free-running trigger square wave frequency, and — when
  captured — a clock-independent `f_out / f_DIO2` ratio check that verifies the
  pulse-mode output to ~1 mHz.

## Required equipment

- Red Pitaya running the repository bitstream and `/root/rp_ctl` helper.
- PicoScope 4000A-family acquisition card supported by the PicoSDK `ps4000a`
  driver.
- PicoSDK system driver/library installed from Pico Technology.
- Python packages from `requirements-picosdk.txt`.
- External TTL source connected to the Red Pitaya input.

## Wiring

Use a shared ground between the Red Pitaya, TTL source, and PicoScope.

| Signal | Red Pitaya pin | PicoScope channel | Notes |
| --- | --- | --- | --- |
| TTL input reference | `DIO0_P` | Channel A | This is the waveform the FPGA measures. |
| Generated output | `DIO1_P` | Channel B | This is the output being verified. |
| Common ground | Red Pitaya `GND` | PicoScope ground clip | Required. |
| Optional trigger output | `DIO2_P` | Channel C | Only needed if running `--dio2-channel C`. |

Keep all probes in DC coupling. Use x1/x10 probe settings consistently with the
PicoScope software and the voltage range passed to the script. For normal 3.3 V
TTL, `--range-v 5 --threshold-v 1.5` is a good starting point.

## Install dependencies

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-picosdk.txt
```

You must also install the PicoSDK system package for your OS. The Python wrapper
cannot talk to the scope without Pico Technology's native driver libraries.

## Prepare the Red Pitaya

Install the helper and bitstream if you have not already done so:

```bash
scp rp_ctl.c root@rp-xxxxxx.local:/root/rp_ctl.c
ssh root@rp-xxxxxx.local 'gcc -O2 -o /root/rp_ctl /root/rp_ctl.c && \
    ln -sf /root/rp_ctl /root/rp_pulse_ctl && \
    ln -sf /root/rp_ctl /root/rp_harmonic_ctl'

scp red_pitaya_top.bit.bin root@rp-xxxxxx.local:/root/
ssh root@rp-xxxxxx.local '/opt/redpitaya/bin/fpgautil -b /root/red_pitaya_top.bit.bin'
```

Confirm the input TTL is present on `DIO0_P`.

## Run the default verification suite

```bash
.venv/bin/python hardware_tests/redpitaya_picosdk_verify.py \
  --host rp-xxxxxx.local \
  --input-channel A \
  --output-channel B \
  --sample-rate-hz 5000000 \
  --range-v 5 \
  --threshold-v 1.5
```

To also test `DIO2_P` on PicoScope channel C:

```bash
.venv/bin/python hardware_tests/redpitaya_picosdk_verify.py \
  --host rp-xxxxxx.local \
  --input-channel A \
  --output-channel B \
  --dio2-channel C
```

If SSH needs a key:

```bash
.venv/bin/python hardware_tests/redpitaya_picosdk_verify.py \
  --host rp-xxxxxx.local \
  --key ~/.ssh/id_rsa
```

If the Red Pitaya input-frequency measurement is not stable yet, pass the known
input frequency explicitly:

```bash
.venv/bin/python hardware_tests/redpitaya_picosdk_verify.py \
  --host rp-xxxxxx.local \
  --input-hz 1000
```

## Output bundle

Each run writes a folder like:

```text
hardware_test_results/redpitaya_picosdk_YYYYMMDD_HHMMSS/
├── README.md
├── summary.json
└── captures/
    ├── off_low.csv
    ├── force_high.csv
    ├── pulse_identity_50pct.csv
    └── ...
```

Send the whole generated folder when asking for debugging help. The most useful
file is `summary.json`; it contains:

- exact test status and metrics;
- Red Pitaya register JSON after each configuration write;
- PicoScope sample rate/range/threshold metadata;
- enough information to reproduce the analysis.

The `captures/*.csv` files contain the raw PicoScope waveforms in volts, so we
can re-run or improve the analysis without another lab capture.

## Interpreting common failures

| Failure | Likely cause |
| --- | --- |
| Output frequency off by a small % but `input_edges == output_edges` (×N for harmonic) | Not a hardware error: under-sampled capture. Check `output_samples_per_period` in `summary.json`; if it is small (≲20), raise `--sample-rate-hz` or shorten the capture. Frequency is reported from the edge span, but very low oversampling still degrades edge timing. |
| Too few input rising edges | No TTL on `DIO0_P`, bad ground, threshold too high, or capture too short. |
| OFF/ON test wrong | Output pin wiring issue, helper not installed, or wrong bitstream. |
| Pulse frequency wrong by the shift amount | `phase_step_offset` write path or signed conversion issue. |
| Harmonic frequency equals pulse mode | Harmonic helper symlink/control bit not taking effect. |
| Oscillating delay amplitude wrong | `f_shift = 4·f_osc·P`, half-period, or sign-toggle path is wrong. |
| Oscillating delay center wrong | Preload/start phase is not aligned to the physical input edge. |
| PicoSDK import/open error | Native PicoSDK missing, wrong driver family, or scope already open elsewhere. |
| `output frequency differs from FPGA-commanded` | Datapath error (wrong base, shift, or NCO word) larger than the clock-mismatch tolerance. Check `output_freq_error_hz` vs `freq_match_tolerance_hz`; small offsets are just the scope/Red Pitaya clock difference. |
| `output/DIO2 ratio implies ...` | Clock-independent datapath error at the 1 mHz level: the measured `f_out/f_DIO2` disagrees with `phase_step/trig_phase_step`. This is a genuine NCO/divider/shift fault (the scope and Red Pitaya clocks both cancel here). |

## Frequency-match precision and its hard limits

The pulse-mode tests (`pulse_identity_50pct`, `pulse_plus_5hz_25pct`,
`pulse_plus_20hz_50pct`) carry a strict check: the coherently-measured output
frequency must equal the FPGA-commanded frequency (`phase_to_hz(phase_step)`,
read back from the settled registers). It uses a least-squares estimate over the
whole edge train (`input_hz_coherent` / `output_hz_coherent`), which resolves the
frequency to well under 1 mHz (`output_hz_coherent_stderr`) — far tighter than
the span estimate.

Two physical walls bound how tightly you can *verify* frequency, independent of
the estimator:

- **Output frequency quantization (≈ 5 Hz at the default window).** The FPGA
  regenerates the output from a fixed-time edge count, so the output frequency
  moves in steps of `1 / (2 · meas_window)`. At the default 100 ms window
  (`--window-us 100000`) that is ~5 Hz, so `DIO0` and `DIO1` can only physically
  agree to ~±2.5 Hz (`output_minus_input_hz` shows this residual). To shrink it,
  lengthen the window: 1 s → 0.5 Hz, 10 s → 0.05 Hz, 100 s → 0.005 Hz. True
  0.001 Hz agreement would need a ~500 s window and is not practical.
- **Scope vs Red Pitaya clock mismatch (tens of ppm).** Any *absolute* frequency
  comparison is limited by the two independent sample clocks. That is why the
  absolute pass tolerance (`freq_match_tolerance_hz`) includes a relative
  `--freq-match-timebase-rel-tol` term (default 1e-4) on top of the 1 mHz floor;
  `output_freq_error_hz` is dominated by this clock offset, not by the NCO.

### True sub-millihertz: the DIO2 ratio check

Capture `DIO2` (`--dio2-channel C`) to escape the clock-mismatch wall. DIO1 and
DIO2 are both NCOs on the Red Pitaya clock, so their register ratio
`phase_step / trig_phase_step` is exact and clock-free, and the PicoScope measures
both, so the measured `f_out / f_DIO2` is clock-free too. The pulse tests then
verify `f_out == (phase_step / trig_phase_step) · f_DIO2` to the 1 mHz floor with
**no ppm term** (`ratio_*` metrics: `ratio_output_freq_error_hz`,
`ratio_match_tolerance_hz`, `ratio_match_resolved`). The harness auto-sets DIO2
near the input frequency (`--dio2-hz 0`) so the ratio is ~1 and DIO2's own error
is not amplified; pass an explicit `--dio2-hz` to override.

Both checks self-gate: `freq_match_resolved` / `ratio_match_resolved` are 1 only
when the capture actually resolves the tolerance; when 0 the error is reported but
the check does not fail, so a short or coarse capture never produces a false
failure. Resolving the 1 mHz floor needs a long, well-oversampled edge train (the
default pulse captures are 1.0 s); raise `--sample-rate-hz` if
`output_samples_per_period` is small.

## Current limitation

This first harness implements the PicoSDK `ps4000a` backend. If your exact
PicoScope 4000 card uses the legacy `ps4000` driver, run the script once and send
the error text plus the exact PicoScope model number; the capture backend is
isolated so we can add that driver without changing the Red Pitaya tests or
analysis.
