#!/usr/bin/env python3
"""Behavior checks for the bounded edge-lock response simulator."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osc_delay_sim import simulate_edge_lock_response  # noqa: E402


class TestEdgeLockResponseSimulation(unittest.TestCase):
    def test_hard_snaps_once_to_the_persistent_delayed_reference(self):
        result = simulate_edge_lock_response("hard")

        phase_jump_words = 32 * result["step_base"]
        self.assertEqual(result["anchor_adjustments"][8], -phase_jump_words)
        self.assertTrue(all(adjustment == 0
                            for adjustment in result["anchor_adjustments"][9:]))
        self.assertEqual(result["reference_displacements"][8:],
                         [result["reference_displacements"][8]] * 72)
        self.assertEqual(result["pulse_ticks"][0], 255)
        self.assertEqual(result["pulse_ticks"][7], 1151)

    def test_gradual_responses_stay_bounded_and_monotonic(self):
        for response in ("fast", "balanced", "smooth"):
            with self.subTest(response=response):
                result = simulate_edge_lock_response(response)
                self.assertTrue(all(abs(correction) <= result["correction_limit"]
                                    for correction in result["corrections"]))
                running = result["run_start_tick"]
                self.assertTrue(all(increment > 0
                                    for increment in result["increments"][running:]))
                self.assertTrue(all(right > left for left, right in zip(
                    result["unwrapped_phase"][running:],
                    result["unwrapped_phase"][running + 1:])))

    def test_gradual_pulses_match_continuous_wraps_without_duplicate_ticks(self):
        for response in ("fast", "balanced", "smooth"):
            with self.subTest(response=response):
                result = simulate_edge_lock_response(response)
                self.assertEqual(len(result["pulse_ticks"]),
                                 result["continuous_wraps"])
                self.assertEqual(len(result["pulse_ticks"]),
                                 len(set(result["pulse_ticks"])))

    def test_gradual_responses_converge_fastest_to_slowest(self):
        converged = {
            response: simulate_edge_lock_response(response)["converged_anchor"]
            for response in ("fast", "balanced", "smooth")
        }

        self.assertTrue(all(anchor is not None for anchor in converged.values()))
        self.assertLess(converged["fast"], converged["balanced"])
        self.assertLess(converged["balanced"], converged["smooth"])

    def test_low_nominal_step_caps_negative_correction(self):
        step_base = 2**48 // 128
        result = simulate_edge_lock_response(
            "fast", phase_step_offset=2 - step_base, anchor_count=12)

        self.assertEqual(result["phase_step"], 2)
        self.assertEqual(result["correction_limit"], 1)
        self.assertIn(-1, result["corrections"])
        self.assertTrue(all(increment > 0
                            for increment in result["increments"][
                                result["run_start_tick"]:]))

    def test_harmonic_and_offset_use_the_nominal_increment(self):
        result = simulate_edge_lock_response(
            "balanced", harmonic_n=3, phase_step_offset=-17)

        self.assertEqual(result["phase_step"], 3 * result["step_base"] - 17)
        running = result["run_start_tick"]
        for previous, current in zip(result["target_trace"][running - 1:],
                                     result["target_trace"][running:]):
            self.assertEqual(current, (previous - 17) % 2**48)
        for increment, correction in zip(result["increments"][running:],
                                         result["corrections"][running:]):
            self.assertEqual(increment, result["phase_step"] + correction)
            self.assertGreater(increment, 0)
            self.assertLessEqual(abs(correction), result["correction_limit"])
        self.assertTrue(all(right > left for left, right in zip(
            result["unwrapped_phase"][running:],
            result["unwrapped_phase"][running + 1:])))

    def test_acquisition_holds_preload_and_gates_harmonic_output(self):
        preload = 2**47
        result = simulate_edge_lock_response(
            "hard", harmonic_n=2, preload=preload, anchor_count=4)

        accepted = result["accepted_anchor_tick"]
        running = result["run_start_tick"]
        self.assertEqual((accepted, running), (127, 128))
        self.assertEqual(result["phase_trace"][:running], [preload] * running)
        self.assertFalse(any(result["running"][:running]))
        self.assertFalse(any(result["harmonic_output"][:running]))
        self.assertFalse(any(tick < running for tick in result["pulse_ticks"]))
        self.assertTrue(result["running"][running])
        self.assertTrue(result["harmonic_output"][running])
        self.assertEqual(result["increments"][running], result["phase_step"])

    def test_gradual_responses_converge_with_a_quantized_period(self):
        converged = {
            response: simulate_edge_lock_response(response, period_clocks=127)[
                "converged_anchor"]
            for response in ("fast", "balanced", "smooth")
        }

        self.assertTrue(all(anchor is not None for anchor in converged.values()))
        self.assertLess(converged["fast"], converged["balanced"])
        self.assertLess(converged["balanced"], converged["smooth"])


if __name__ == "__main__":
    unittest.main()
