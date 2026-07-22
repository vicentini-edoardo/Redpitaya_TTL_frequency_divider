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
    strobe_step_word, dwell_s_to_cycles,
    phase_offset_to_preload, preload_to_phase_offset,
    harmonic_phase_offset_to_preload, harmonic_preload_to_phase_offset,
    CTRL_EDGE_RESPONSE_MASK, CTRL_EDGE_RESPONSE_HARD, CTRL_EDGE_RESPONSE_FAST,
    CTRL_EDGE_RESPONSE_BALANCED, CTRL_EDGE_RESPONSE_SMOOTH,
    DEFAULT_EDGE_LOCK_RESPONSE, EDGE_LOCK_RESPONSES,
)

_PHASE_MAX = 2 ** (PHASE_BITS - 1)


class TestEdgeLockResponseBits(unittest.TestCase):
    def test_response_values_are_control_bits_7_through_6(self):
        self.assertEqual(CTRL_EDGE_RESPONSE_MASK, 0xC0)
        self.assertEqual(
            (CTRL_EDGE_RESPONSE_HARD, CTRL_EDGE_RESPONSE_FAST,
             CTRL_EDGE_RESPONSE_BALANCED, CTRL_EDGE_RESPONSE_SMOOTH),
            (0x00, 0x40, 0x80, 0xC0),
        )
        self.assertEqual(DEFAULT_EDGE_LOCK_RESPONSE, CTRL_EDGE_RESPONSE_BALANCED)
        self.assertEqual(EDGE_LOCK_RESPONSES, (
            ("Hard", 0x00),
            ("Fast", 0x40),
            ("Balanced", 0x80),
            ("Smooth", 0xC0),
        ))


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
    def test_whole_periods_over_span(self):
        # 100_001 rising edges → 100_000 whole periods over 12_500_000 cycles
        step = measured_edges_to_phase_step(100_001, 12_500_000)
        hz = phase_to_hz(step)
        self.assertAlmostEqual(hz, CLK_HZ * 100_000 / 12_500_000,
                               delta=PHASE_RES_HZ)

    def test_excludes_window_opening_edge(self):
        # 3 rising edges = exactly 2 whole periods inside the span
        step = measured_edges_to_phase_step(3, 250_000)
        self.assertAlmostEqual(phase_to_hz(step), CLK_HZ * 2 / 250_000,
                               delta=PHASE_RES_HZ)

    def test_too_few_edges_reports_zero(self):
        self.assertEqual(measured_edges_to_phase_step(2, 125_000), 0)
        self.assertEqual(measured_edges_to_phase_step(3, 0), 0)


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


class TestStrobeMath(unittest.TestCase):
    PHASE_WRAP = 2 ** PHASE_BITS

    def test_step_word_sign_and_magnitude(self):
        # Advancing the delay subtracts phase from the target: word is negative.
        word = strobe_step_word(0.05)
        self.assertEqual(word, -round(0.05 * self.PHASE_WRAP))

    def test_step_word_roundtrip_mod_wrap(self):
        # n steps of step_frac then back n steps of the complement returns to
        # the start word mod 2^48.
        start = phase_offset_to_preload(0.20)
        word = strobe_step_word(0.05)
        target = start
        for _ in range(4):
            target = (target + word) % self.PHASE_WRAP
        # 0.20 + 4*0.05 = 0.40 delay
        self.assertAlmostEqual(target, phase_offset_to_preload(0.40), delta=4)

    def test_step_word_reduces_mod_one(self):
        self.assertEqual(strobe_step_word(1.05), strobe_step_word(0.05))

    def test_step_word_zero(self):
        self.assertEqual(strobe_step_word(0.0), 0)

    def test_dwell_rounding(self):
        self.assertEqual(dwell_s_to_cycles(1.0), CLK_HZ)
        self.assertEqual(dwell_s_to_cycles(0.1), round(0.1 * CLK_HZ))

    def test_dwell_clamps(self):
        self.assertEqual(dwell_s_to_cycles(0.0), 1)
        self.assertEqual(dwell_s_to_cycles(1e6), 2 ** 32 - 1)


class TestPhaseOffsetPreload(unittest.TestCase):
    PHASE_WRAP = 2 ** PHASE_BITS

    def test_zero_offset_is_zero_preload(self):
        # 0 turns → pulse aligned to the input edge → preload 0.
        self.assertEqual(phase_offset_to_preload(0.0), 0)

    def test_quarter_turn(self):
        # 0.25 turn delay → preload = (1 - 0.25) * 2^48 = 0.75 * 2^48.
        expected = int(round(0.75 * self.PHASE_WRAP))
        self.assertEqual(phase_offset_to_preload(0.25), expected)

    def test_result_within_48_bits(self):
        for turns in (0.0, 0.1, 0.5, 0.999, 1.0, 2.5, -0.25):
            word = phase_offset_to_preload(turns)
            self.assertGreaterEqual(word, 0)
            self.assertLess(word, self.PHASE_WRAP)

    def test_reduces_mod_one_turn(self):
        # A whole extra turn is the same physical phase.
        self.assertEqual(phase_offset_to_preload(0.3),
                         phase_offset_to_preload(1.3))
        # Negative offset wraps to its positive complement.
        self.assertEqual(phase_offset_to_preload(-0.25),
                         phase_offset_to_preload(0.75))

    def test_roundtrip_within_one_lsb(self):
        for turns in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9):
            word = phase_offset_to_preload(turns)
            back = preload_to_phase_offset(word)
            self.assertLessEqual(abs(back - turns), 1.0 / self.PHASE_WRAP + 1e-12,
                                 msg=f"roundtrip failed for {turns} turns")


class TestHarmonicPhaseOffsetPreload(unittest.TestCase):
    PHASE_WRAP = 2 ** PHASE_BITS

    def test_zero_offset_aligns_msb(self):
        # 0 turns → output rising edge at the input edge → preload = 2^47.
        self.assertEqual(harmonic_phase_offset_to_preload(0.0), self.PHASE_WRAP // 2)

    def test_quarter_turn(self):
        # 0.25 turn delay → preload = (0.5 - 0.25) * 2^48 = 0.25 * 2^48.
        expected = int(round(0.25 * self.PHASE_WRAP))
        self.assertEqual(harmonic_phase_offset_to_preload(0.25), expected)

    def test_result_within_48_bits(self):
        for turns in (0.0, 0.1, 0.5, 0.999, 1.0, 2.5, -0.25):
            word = harmonic_phase_offset_to_preload(turns)
            self.assertGreaterEqual(word, 0)
            self.assertLess(word, self.PHASE_WRAP)

    def test_reduces_mod_one_turn(self):
        self.assertEqual(harmonic_phase_offset_to_preload(0.3),
                         harmonic_phase_offset_to_preload(1.3))
        self.assertEqual(harmonic_phase_offset_to_preload(-0.25),
                         harmonic_phase_offset_to_preload(0.75))

    def test_half_turn_from_pulse_reference(self):
        # Harmonic preload leads the pulse preload by exactly half a turn.
        for turns in (0.0, 0.1, 0.37, 0.8):
            self.assertEqual(harmonic_phase_offset_to_preload(turns),
                             phase_offset_to_preload(turns - 0.5))

    def test_roundtrip_within_one_lsb(self):
        for turns in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9):
            word = harmonic_phase_offset_to_preload(turns)
            back = harmonic_preload_to_phase_offset(word)
            self.assertLessEqual(abs(back - turns), 1.0 / self.PHASE_WRAP + 1e-12,
                                 msg=f"roundtrip failed for {turns} turns")


if __name__ == "__main__":
    unittest.main()
