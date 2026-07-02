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

    def test_osc_delay_sinusoidal_fit_recovers_parameters(self):
        # Ideal sinusoidal delay oscillation: fit should recover P0, P, f_osc.
        import math as _math
        f_in = 10_000.0
        duration_s = 1.0
        f_osc = 5.0
        p0 = 0.25
        p = 0.05
        T_in = 1.0 / f_in
        input_edges = [k * T_in for k in range(int(duration_s * f_in))]
        output_edges = [
            t + (p0 + p * _math.cos(2 * _math.pi * f_osc * t)) * T_in
            for t in input_edges
        ]

        result = analyze_osc_delay(
            input_rising_s=input_edges,
            output_rising_s=output_edges,
            expectation=OscExpectation(f_osc_hz=f_osc, p_frac=p, p0_frac=p0),
        )

        self.assertEqual(result.status, CheckStatus.PASS, result.messages)
        self.assertAlmostEqual(result.metrics["delay_phase_center"], p0, delta=0.01)
        self.assertAlmostEqual(result.metrics["delay_phase_amplitude"], p, delta=0.005)
        self.assertAlmostEqual(result.metrics["delay_osc_hz"], f_osc, delta=0.5)
        self.assertEqual(result.metrics["delay_fit_nonlinear"], 1)

    def test_osc_delay_triangle_wave_passes_within_amplitude_tolerance(self):
        # Hardware generates a triangle-wave delay (phase_step_offset alternates
        # sign). The sinusoidal fit underestimates the amplitude by 8/π² ≈ 0.81,
        # but the error (< 0.01 for P=0.05) stays within osc_phase_abs_tol=0.025.
        import math as _math
        f_in = 10_000.0
        duration_s = 1.0
        f_osc = 5.0
        p0 = 0.25
        p = 0.05
        T_in = 1.0 / f_in
        input_edges = [k * T_in for k in range(int(duration_s * f_in))]
        output_edges = [
            t + (p0 + p * (2.0 * abs(2.0 * ((t * f_osc) % 1.0) - 1.0) - 1.0)) * T_in
            for t in input_edges
        ]

        result = analyze_osc_delay(
            input_rising_s=input_edges,
            output_rising_s=output_edges,
            expectation=OscExpectation(f_osc_hz=f_osc, p_frac=p, p0_frac=p0),
        )

        self.assertEqual(result.status, CheckStatus.PASS, result.messages)
        self.assertAlmostEqual(result.metrics["delay_phase_center"], p0, delta=0.01)
        self.assertAlmostEqual(result.metrics["delay_osc_hz"], f_osc, delta=0.5)
        # Amplitude biased to ~0.81×P; must pass the hardware check (tol 0.025)
        self.assertLess(abs(result.metrics["delay_phase_amplitude"] - p), 0.025)

    def test_osc_delay_fails_when_oscillation_rate_wrong(self):
        # f_osc is 10× higher than commanded: the fit must detect the mismatch.
        import math as _math
        f_in = 10_000.0
        duration_s = 1.0
        f_osc_actual = 50.0
        f_osc_expected = 5.0
        p0 = 0.25
        p = 0.05
        T_in = 1.0 / f_in
        input_edges = [k * T_in for k in range(int(duration_s * f_in))]
        output_edges = [
            t + (p0 + p * _math.cos(2 * _math.pi * f_osc_actual * t)) * T_in
            for t in input_edges
        ]

        result = analyze_osc_delay(
            input_rising_s=input_edges,
            output_rising_s=output_edges,
            expectation=OscExpectation(f_osc_hz=f_osc_expected, p_frac=p, p0_frac=p0),
        )

        self.assertEqual(result.status, CheckStatus.FAIL)
        self.assertTrue(any("oscillation rate" in m for m in result.messages))

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

    def test_coherent_frequency_resolves_far_below_a_millihertz(self):
        from hardware_tests.redpitaya_picosdk_verify import _coherent_frequency
        import random

        f = 254_000.0
        period = 1.0 / f
        n = 100_000  # ~0.4 s of edges
        rng = random.Random(1234)
        jitter_s = 5e-9  # 5 ns RMS edge timing noise
        edges = [k * period + rng.gauss(0.0, jitter_s) for k in range(n)]

        freq, stderr = _coherent_frequency(edges)
        self.assertTrue(math.isfinite(freq) and math.isfinite(stderr))
        # The coherent fit must recover the true frequency to well under 1 mHz
        # and report a standard error well under 1 mHz.
        self.assertLess(abs(freq - f), 1e-3)
        self.assertLess(stderr, 1e-3)

    def test_coherent_frequency_rejects_trains_with_missing_edges(self):
        from hardware_tests.redpitaya_picosdk_verify import _coherent_frequency
        # A dropped edge leaves a ~2x gap; the integer-index fit is invalid, so
        # the estimator must refuse rather than report a biased frequency.
        edges = [0.0, 1.0, 2.0, 4.0, 5.0, 6.0]  # missing edge at 3.0
        freq, stderr = _coherent_frequency(edges)
        self.assertTrue(math.isnan(freq))
        self.assertTrue(math.isnan(stderr))

    def test_frequency_match_check_passes_when_output_equals_command(self):
        from hardware_tests.redpitaya_picosdk_verify import (
            _coherent_frequency, _frequency_match_check,
        )

        commanded = 254_000.0
        n = 120_000
        out_edges = [k / commanded for k in range(n)]
        out_hz, out_se = _coherent_frequency(out_edges)

        metrics: dict = {}
        msgs = _frequency_match_check(out_hz, out_se, commanded, AnalysisConfig(), metrics)
        self.assertEqual(msgs, [])
        self.assertEqual(metrics["freq_match_resolved"], 1)
        self.assertLess(abs(metrics["output_freq_error_hz"]), 1e-3)

    def test_frequency_match_check_fails_when_output_off_command(self):
        from hardware_tests.redpitaya_picosdk_verify import (
            _coherent_frequency, _frequency_match_check,
        )

        commanded = 254_000.0
        # NCO actually emits 20 mHz high: with the clock allowance zeroed, the
        # 1 mHz statistical floor governs and the error must be caught.
        out_edges = [k / (commanded + 0.02) for k in range(120_000)]
        out_hz, out_se = _coherent_frequency(out_edges)

        metrics: dict = {}
        msgs = _frequency_match_check(
            out_hz, out_se, commanded,
            AnalysisConfig(freq_match_timebase_rel_tol=0.0), metrics,
        )
        self.assertEqual(metrics["freq_match_resolved"], 1)
        self.assertTrue(any("FPGA-commanded" in m for m in msgs))

    def test_frequency_match_check_tolerates_scope_clock_offset(self):
        from hardware_tests.redpitaya_picosdk_verify import (
            _coherent_frequency, _frequency_match_check,
        )

        commanded = 254_000.0
        # A 50 ppm scope-vs-RedPitaya clock offset (~12.7 Hz at 254 kHz) must NOT
        # fail: absolute frequency agreement is bounded by the clock mismatch,
        # which the timebase allowance covers.
        out_edges = [k / (commanded * (1 + 50e-6)) for k in range(120_000)]
        out_hz, out_se = _coherent_frequency(out_edges)

        metrics: dict = {}
        msgs = _frequency_match_check(out_hz, out_se, commanded, AnalysisConfig(), metrics)
        self.assertEqual(msgs, [])
        self.assertEqual(metrics["freq_match_resolved"], 1)
        self.assertGreater(abs(metrics["output_freq_error_hz"]), 1.0)

    def test_frequency_match_check_skips_when_capture_cannot_resolve(self):
        from hardware_tests.redpitaya_picosdk_verify import (
            _coherent_frequency, _frequency_match_check,
        )
        import random

        # A short, coarsely-sampled train cannot resolve the 1 mHz floor (clock
        # allowance zeroed): the check must record the error but not fail.
        f = 254_000.0
        n = 200  # ~0.8 ms of edges -> large standard error
        rng = random.Random(7)
        out_edges = [k / f + rng.gauss(0.0, 50e-9) for k in range(n)]
        out_hz, out_se = _coherent_frequency(out_edges)

        metrics: dict = {}
        msgs = _frequency_match_check(
            out_hz, out_se, f,
            AnalysisConfig(freq_match_timebase_rel_tol=0.0), metrics,
        )
        self.assertEqual(msgs, [])
        self.assertEqual(metrics["freq_match_resolved"], 0)

    def test_ratio_check_passes_and_cancels_common_scope_clock(self):
        from hardware_tests.redpitaya_picosdk_verify import (
            _coherent_frequency, _frequency_ratio_check,
        )

        f_out = 254_000.0
        f_dio2 = 200_000.0
        ratio = f_out / f_dio2  # exact register ratio (Red Pitaya clock cancels)
        skew = 1.0 + 50e-6      # common PicoScope clock offset on BOTH channels
        out_edges = [k / f_out * skew for k in range(200_000)]
        dio2_edges = [k / f_dio2 * skew for k in range(160_000)]
        out_hz, out_se = _coherent_frequency(out_edges)
        d_hz, d_se = _coherent_frequency(dio2_edges)

        metrics: dict = {}
        msgs = _frequency_ratio_check(out_hz, out_se, d_hz, d_se, ratio, AnalysisConfig(), metrics)
        self.assertEqual(msgs, [])
        self.assertEqual(metrics["ratio_match_resolved"], 1)
        # The common 50 ppm scope skew cancels in the ratio: error stays sub-mHz.
        self.assertLess(abs(metrics["ratio_output_freq_error_hz"]), 1e-3)

    def test_ratio_check_fails_when_output_off_ratio(self):
        from hardware_tests.redpitaya_picosdk_verify import (
            _coherent_frequency, _frequency_ratio_check,
        )

        f_out = 254_000.0
        f_dio2 = 200_000.0
        ratio = f_out / f_dio2
        # NCO emits 5 mHz off the ratio the registers promise -> must be caught.
        out_edges = [k / (f_out + 0.005) for k in range(200_000)]
        dio2_edges = [k / f_dio2 for k in range(160_000)]
        out_hz, out_se = _coherent_frequency(out_edges)
        d_hz, d_se = _coherent_frequency(dio2_edges)

        metrics: dict = {}
        msgs = _frequency_ratio_check(out_hz, out_se, d_hz, d_se, ratio, AnalysisConfig(), metrics)
        self.assertEqual(metrics["ratio_match_resolved"], 1)
        self.assertTrue(any("ratio implies" in m for m in msgs))

    def test_ratio_check_skips_when_dio2_too_low_to_resolve(self):
        from hardware_tests.redpitaya_picosdk_verify import (
            _coherent_frequency, _frequency_ratio_check,
        )
        import random

        # A low, short DIO2 reference has its error amplified by the large ratio,
        # so the floor is not resolved: report but do not fail.
        f_out = 254_000.0
        f_dio2 = 100.0
        ratio = f_out / f_dio2
        rng = random.Random(3)
        out_edges = [k / f_out for k in range(200_000)]
        dio2_edges = [k / f_dio2 + rng.gauss(0.0, 50e-9) for k in range(100)]
        out_hz, out_se = _coherent_frequency(out_edges)
        d_hz, d_se = _coherent_frequency(dio2_edges)

        metrics: dict = {}
        msgs = _frequency_ratio_check(out_hz, out_se, d_hz, d_se, ratio, AnalysisConfig(), metrics)
        self.assertEqual(msgs, [])
        self.assertEqual(metrics["ratio_match_resolved"], 0)


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
        # osc bit cleared before the write so its rising edge re-arms the sweep
        self.assertEqual(commands[1], ["/root/rp_pulse_ctl", hex(DEFAULT_BASE), "control", "1"])
        self.assertEqual(commands[2], ["/root/rp_pulse_ctl", hex(DEFAULT_BASE), "write",
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
