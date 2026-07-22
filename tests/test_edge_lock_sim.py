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
        self.assertEqual(result["pulse_ticks"][0], 127)
        self.assertEqual(result["pulse_ticks"][8], 1151)

    def test_gradual_responses_stay_bounded_and_monotonic(self):
        for response in ("fast", "balanced", "smooth"):
            with self.subTest(response=response):
                result = simulate_edge_lock_response(response)
                self.assertTrue(all(abs(correction) <= result["correction_limit"]
                                    for correction in result["corrections"]))
                self.assertTrue(all(increment > 0 for increment in result["increments"]))
                self.assertTrue(all(right > left for left, right in zip(
                    result["unwrapped_phase"], result["unwrapped_phase"][1:])))

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


if __name__ == "__main__":
    unittest.main()
