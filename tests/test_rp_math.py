#!/usr/bin/env python3
"""
Unit tests for rp_math — the Qt-free frequency/duty conversion helpers.

Run with either:
    python3 -m unittest discover -s tests
    pytest tests/
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rp_math import (  # noqa: E402
    CLK_HZ, PHASE_BITS, PHASE_RES_HZ, MAX_SHIFT_HZ, WINDOW_OPTIONS_US, WINDOW_NAMES,
    hz_to_phase, phase_to_hz, duty_to_cycles, fmt_freq, fmt_signed_freq,
    suggest_window, trig_hz_to_phase_step, trig_phase_step_to_hz,
    measured_edges_to_phase_step, fmt_dur,
)

_PHASE_MAX = 2 ** (PHASE_BITS - 1)


class TestPhaseConversion(unittest.TestCase):
    def test_zero_roundtrips_to_zero(self):
        self.assertEqual(hz_to_phase(0.0), 0)
        self.assertEqual(phase_to_hz(0), 0.0)

    def test_roundtrip_within_one_lsb(self):
        # hz -> phase -> hz must land within half an LSB of the request.
        for hz in (1.0, 12.5, 1000.0, -250.0, 1e5, -1e6):
            word = hz_to_phase(hz)
            back = phase_to_hz(word)
            self.assertLessEqual(abs(back - hz), PHASE_RES_HZ / 2 + 1e-9,
                                 msg=f"roundtrip failed for {hz} Hz")

    def test_sign_preserved(self):
        self.assertGreater(hz_to_phase(100.0), 0)
        self.assertLess(hz_to_phase(-100.0), 0)
        self.assertEqual(hz_to_phase(100.0), -hz_to_phase(-100.0))

    def test_clamping_to_phase_range(self):
        # Far beyond the representable range clamps, never overflows.
        self.assertEqual(hz_to_phase(10 * MAX_SHIFT_HZ), _PHASE_MAX - 1)
        self.assertEqual(hz_to_phase(-10 * MAX_SHIFT_HZ), -_PHASE_MAX)

    def test_resolution_constant(self):
        self.assertAlmostEqual(PHASE_RES_HZ, CLK_HZ / 2 ** PHASE_BITS)
        # One LSB step changes the frequency by exactly one resolution unit.
        self.assertAlmostEqual(phase_to_hz(1), PHASE_RES_HZ)


class TestDutyToCycles(unittest.TestCase):
    def test_midpoint(self):
        self.assertEqual(duty_to_cycles(0.5, 1000), 500)

    def test_never_zero_and_never_full(self):
        # Output stays strictly inside (0, period) so the pulse is never
        # degenerate constant-low or constant-high.
        self.assertEqual(duty_to_cycles(0.0, 1000), 1)
        self.assertEqual(duty_to_cycles(1.0, 1000), 999)
        self.assertEqual(duty_to_cycles(-5.0, 1000), 1)
        self.assertEqual(duty_to_cycles(5.0, 1000), 999)

    def test_rounds_to_nearest(self):
        self.assertEqual(duty_to_cycles(0.1234, 1000), 123)


class TestSuggestWindow(unittest.TestCase):
    def test_indices_are_valid(self):
        for f in (0, 0.1, 5, 50, 500, 5000):
            idx = suggest_window(f)
            self.assertIn(idx, range(len(WINDOW_OPTIONS_US)))

    def test_monotonic_faster_shift_shorter_window(self):
        # Higher shift frequency => shorter window (lower index).
        self.assertGreaterEqual(suggest_window(0.5), suggest_window(5))
        self.assertGreaterEqual(suggest_window(5), suggest_window(50))
        self.assertGreaterEqual(suggest_window(50), suggest_window(500))
        self.assertGreaterEqual(suggest_window(500), suggest_window(5000))

    def test_boundaries(self):
        self.assertEqual(suggest_window(0), 2)
        self.assertEqual(suggest_window(2000), 0)


class TestTrigPhaseStep(unittest.TestCase):
    def test_off(self):
        self.assertEqual(trig_hz_to_phase_step(0), 0)
        self.assertEqual(trig_hz_to_phase_step(-1), 0)

    def test_uses_same_quantisation_as_delta(self):
        self.assertEqual(trig_hz_to_phase_step(4.0), hz_to_phase(4.0))

    def test_reconstructed_frequency_is_close(self):
        for f in (1.0, 50.0, 1000.0):
            step = trig_hz_to_phase_step(f)
            recon = trig_phase_step_to_hz(step)
            self.assertLessEqual(abs(recon - f), PHASE_RES_HZ / 2 + 1e-9)


class TestInputMeasurementMath(unittest.TestCase):
    def test_excludes_window_start_edge_and_preserves_half_edge_resolution(self):
        window_cycles = 125_000_000
        step = measured_edges_to_phase_step(526_260, window_cycles)
        hz = phase_to_hz(step)

        self.assertAlmostEqual(hz, 263_129.497894923, places=6)

    def test_too_few_edges_reports_zero(self):
        self.assertEqual(measured_edges_to_phase_step(3, 125_000), 0)


class TestFormatters(unittest.TestCase):
    def test_fmt_freq_units(self):
        self.assertEqual(fmt_freq(0), "---")
        self.assertEqual(fmt_freq(-5), "---")
        self.assertIn("Hz", fmt_freq(12.5))
        self.assertIn("kHz", fmt_freq(12_500))
        self.assertIn("MHz", fmt_freq(12_500_000))

    def test_fmt_signed_freq_sign_and_zero(self):
        self.assertEqual(fmt_signed_freq(0.0), "+0.000000 Hz")
        self.assertTrue(fmt_signed_freq(100).startswith("+"))
        self.assertTrue(fmt_signed_freq(-100).startswith("-"))

    def test_fmt_dur_units(self):
        self.assertEqual(fmt_dur(0), "---")
        self.assertIn("ns", fmt_dur(5e-9))
        self.assertIn("µs", fmt_dur(5e-6))
        self.assertIn("ms", fmt_dur(5e-3))
        self.assertIn("s", fmt_dur(5.0))

    def test_window_names_match_options(self):
        self.assertEqual(len(WINDOW_NAMES), len(WINDOW_OPTIONS_US))


if __name__ == "__main__":
    unittest.main()
