#!/usr/bin/env python3
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hardware_tests.redpitaya_picosdk_verify import (  # noqa: E402
    AnalysisConfig,
    CheckStatus,
    OscExpectation,
    PulseExpectation,
    RedPitayaCommandBuilder,
    analyze_capture,
    analyze_osc_delay,
    detect_edges,
    square_wave,
    write_debug_bundle,
)
from rp_math import DEFAULT_BASE, hz_to_phase, osc_phase_preload  # noqa: E402


class TestWaveformAnalysis(unittest.TestCase):
    def test_detect_edges_finds_rising_and_falling_crossings(self):
        times = [i * 1e-6 for i in range(10)]
        volts = [0, 0, 3.3, 3.3, 0, 0, 3.3, 3.3, 0, 0]

        edges = detect_edges(times, volts, threshold_v=1.5)

        self.assertEqual([(round(e.time_s, 7), e.rising) for e in edges],
                         [(0.0000015, True), (0.0000035, False),
                          (0.0000055, True), (0.0000075, False)])

    def test_pulse_analysis_passes_for_expected_frequency_and_duty(self):
        times, input_v = square_wave(freq_hz=1_000.0, duration_s=0.05, sample_rate_hz=200_000)
        _, output_v = square_wave(freq_hz=1_100.0, duration_s=0.05, sample_rate_hz=200_000, duty=0.25)
        expectation = PulseExpectation(input_multiplier=1, shift_hz=100.0, duty_frac=0.25)

        result = analyze_capture(
            test_name="pulse_plus_100",
            times_s=times,
            channels_v={"A": input_v, "B": output_v},
            input_channel="A",
            output_channel="B",
            expectation=expectation,
            cfg=AnalysisConfig(threshold_v=1.5, min_edges=4),
        )

        self.assertEqual(result.status, CheckStatus.PASS)
        self.assertAlmostEqual(result.metrics["input_hz"], 1_000.0, delta=1.0)
        self.assertAlmostEqual(result.metrics["output_hz"], 1_100.0, delta=2.0)
        self.assertAlmostEqual(result.metrics["output_duty"], 0.25, delta=0.02)

    def test_pulse_analysis_fails_wrong_output_frequency(self):
        times, input_v = square_wave(freq_hz=1_000.0, duration_s=0.05, sample_rate_hz=200_000)
        _, output_v = square_wave(freq_hz=1_050.0, duration_s=0.05, sample_rate_hz=200_000)
        expectation = PulseExpectation(input_multiplier=1, shift_hz=100.0, duty_frac=0.5)

        result = analyze_capture(
            test_name="pulse_wrong",
            times_s=times,
            channels_v={"A": input_v, "B": output_v},
            input_channel="A",
            output_channel="B",
            expectation=expectation,
            cfg=AnalysisConfig(threshold_v=1.5, min_edges=4, freq_rel_tol=0.001),
        )

        self.assertEqual(result.status, CheckStatus.FAIL)
        self.assertTrue(any("frequency" in msg for msg in result.messages))

    def test_osc_delay_analysis_reports_center_amplitude_and_rate(self):
        input_freq = 1_000.0
        duration_s = 0.4
        f_osc = 5.0
        p0 = 0.25
        p = 0.05
        input_period = 1 / input_freq
        input_edges = [i * input_period for i in range(int(duration_s * input_freq))]
        output_edges = []
        for t in input_edges:
            tri = 2.0 * abs(2.0 * ((t * f_osc) % 1.0) - 1.0) - 1.0
            phase = p0 + p * tri
            output_edges.append(t + phase * input_period)

        result = analyze_osc_delay(
            input_rising_s=input_edges,
            output_rising_s=output_edges,
            expectation=OscExpectation(f_osc_hz=f_osc, p_frac=p, p0_frac=p0),
        )

        self.assertEqual(result.status, CheckStatus.PASS)
        self.assertAlmostEqual(result.metrics["delay_phase_center"], p0, delta=0.01)
        self.assertAlmostEqual(result.metrics["delay_phase_amplitude"], p, delta=0.01)
        self.assertAlmostEqual(result.metrics["delay_osc_hz"], f_osc, delta=0.6)

    def test_identity_passes_when_undersampled_with_nco_jitter(self):
        # Regression for the false "output frequency differs" FAIL on the
        # shift=0 identity test. An NCO-generated output sampled at only
        # ~11 samples/period gets a quantized, biased median-period estimate
        # even though input and output carry the same number of edges. The
        # span-based estimator must report f_out == f_in here.
        clk_hz = 124_999_999.0
        f_in = 259_000.0
        scope_dt = 350e-9  # 2.857 MHz effective sample rate -> ~11 samples/period
        duration_s = 0.05

        # Clean external input.
        in_times, in_v = square_wave(f_in, duration_s, 1.0 / scope_dt)

        # NCO output: carry-out of a 48-bit accumulator stepped at the rate the
        # FPGA derives for f_in. Reconstruct it on the scope sample grid so the
        # per-edge jitter and grid quantization match a real capture.
        phase_step = round(f_in * 2**48 / clk_hz)
        out_v = []
        for t in in_times:
            # phase advanced by NCO at clk_hz, observed on the scope grid
            phase = (t * clk_hz * phase_step / 2**48) % 1.0
            out_v.append(3.3 if phase < 0.5 else 0.0)

        result = analyze_capture(
            test_name="identity_undersampled",
            times_s=in_times,
            channels_v={"A": in_v, "B": out_v},
            input_channel="A",
            output_channel="B",
            expectation=PulseExpectation(input_multiplier=1, shift_hz=0.0, duty_frac=0.5),
            cfg=AnalysisConfig(threshold_v=1.5, min_edges=4),
        )

        self.assertEqual(result.status, CheckStatus.PASS, result.messages)
        # Span estimate must agree to well within tolerance...
        self.assertAlmostEqual(result.metrics["output_hz"], result.metrics["input_hz"],
                               delta=max(2.0, f_in * 0.002))
        # ...and the under-sampling must be reported for diagnosis.
        self.assertLess(result.metrics["output_samples_per_period"], 20.0)

    def test_frequency_estimator_uses_span_not_median(self):
        from hardware_tests.redpitaya_picosdk_verify import _frequency_from_rising_edges
        # Edge times whose median interval differs from the mean interval:
        # periods are 1, 1, 1, 1, 5 (median 1.0, mean 1.8). The estimator must
        # follow the total span (mean), not the median.
        edges = [0.0, 1.0, 2.0, 3.0, 4.0, 9.0]
        self.assertAlmostEqual(_frequency_from_rising_edges(edges), 5 / 9.0, places=9)


class TestCommandBuilder(unittest.TestCase):
    def test_builds_pulse_command_like_gui(self):
        b = RedPitayaCommandBuilder(DEFAULT_BASE)

        cmd = b.pulse_write(width_cycles=123, shift_hz=100.0)

        self.assertEqual(cmd, ["/root/rp_pulse_ctl", hex(DEFAULT_BASE), "write",
                               "123", str(hz_to_phase(100.0)), "1"])

    def test_builds_osc_sequence_like_gui(self):
        b = RedPitayaCommandBuilder(DEFAULT_BASE)

        commands = b.osc_apply(width_cycles=12, half_period_cycles=1000,
                               preload=osc_phase_preload(0.25, 0.05),
                               shift_hz=20.0)

        self.assertEqual(commands[0][:3], ["/root/rp_pulse_ctl", hex(DEFAULT_BASE), "osc"])
        self.assertEqual(commands[1], ["/root/rp_pulse_ctl", hex(DEFAULT_BASE), "write",
                                       "12", str(hz_to_phase(20.0)), "17"])


class TestDebugBundle(unittest.TestCase):
    def test_write_debug_bundle_contains_machine_readable_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_debug_bundle(
                output_dir=tmp,
                board="rp-test.local",
                results=[],
                captures={},
                metadata={"sample_rate_hz": 1_000_000},
            )

            summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["board"], "rp-test.local")
            self.assertEqual(summary["metadata"]["sample_rate_hz"], 1_000_000)
            self.assertTrue((path / "README.md").exists())


if __name__ == "__main__":
    unittest.main()
