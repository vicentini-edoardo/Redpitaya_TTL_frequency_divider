#!/usr/bin/env python3
"""Smoke tests for the PySide6 GUI layout."""
import os
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtTest import QSignalSpy  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QWidget  # noqa: E402

import redpitaya_combined_gui_qt as gui  # noqa: E402


class _FakeBackend(QObject):
    sig_connected = Signal()
    sig_disconnected = Signal(str)
    sig_status = Signal(dict)
    sig_mode_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.mode = "pulse"
        self.window_calls = []
        self.pulse_calls = []
        self.harmonic_calls = []
        self.control_calls = []

    def set_window(self, window_us: int):
        self.window_calls.append(window_us)

    def apply_pulse(self, *args, **kwargs):
        self.pulse_calls.append((args, kwargs))

    def apply_harmonic(self, *args, **kwargs):
        self.harmonic_calls.append((args, kwargs))

    def set_control_pulse(self, control: int):
        self.control_calls.append(control)

    def set_control_harmonic(self, control: int):
        self.control_calls.append(control)


class TestDarkWorkbenchLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_exposes_dark_workbench_structure(self):
        win = gui.MainWindow()
        self.addCleanup(win.close)

        self.assertEqual(win.objectName(), "rpDarkWorkbench")
        self.assertIsNotNone(win.findChild(QWidget, "rpWorkbenchHeader"))
        self.assertIsNotNone(win.findChild(QWidget, "rpReadoutGrid"))
        self.assertIsNotNone(win.findChild(QWidget, "rpControlDeck"))
        self.assertIsNotNone(win.findChild(QWidget, "rpSharedTools"))

        title = win.findChild(QLabel, "rpWorkbenchTitle")
        self.assertIsNotNone(title)
        self.assertIn("Red Pitaya", title.text())
        self.assertIn("TTL", title.text())

    def test_main_window_applies_repo_icon_to_window_and_app(self):
        win = gui.MainWindow()
        self.addCleanup(win.close)

        self.assertFalse(win.windowIcon().isNull())
        self.assertFalse(self.app.windowIcon().isNull())

    def test_readout_value_font_shrinks_to_fit_long_values(self):
        tile = gui.BigDisplay("Output Frequency", "shift +0.000000 Hz", gui._GREEN)
        self.addCleanup(tile.close)
        tile.setFixedSize(360, 84)
        tile.show()

        tile.set_data("125000000.000000 MHz", "shift +0.000000 Hz")
        self.app.processEvents()

        value_label = tile.findChildren(QLabel)[1]
        value_width = value_label.fontMetrics().horizontalAdvance(value_label.text())
        self.assertLessEqual(value_width, value_label.contentsRect().width())
        self.assertLessEqual(
            value_label.fontMetrics().height(),
            value_label.contentsRect().height(),
        )


class TestConfirmedStateContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_confirmed_state_uses_fpga_register_values(self):
        trig_step = gui.trig_hz_to_phase_step(500.0)
        shift_step = gui.hz_to_phase(-37.0)

        state = gui._confirmed_state(
            {
                "control": gui.CTRL_ENABLE,
                "harmonic_mode": 0,
                "osc_mode": 0,
                "edge_lock": 1,
                "period_stable": 1,
                "trig_phase_step": trig_step,
                "phase_step_offset": shift_step,
                "phase_step_base": gui.hz_to_phase(1_000_000.0),
                "phase_step": gui.hz_to_phase(999_963.0),
                "width": 62,
                "dwell_cycles": 0,
                "osc_phase_preload": 123,
            },
            connected=True,
            sequence=7,
            now=1234.5,
        )

        self.assertEqual(state["schema_version"], 1)
        self.assertTrue(state["connected"])
        self.assertTrue(state["hardware_confirmed"])
        self.assertEqual(state["sequence"], 7)
        self.assertEqual(state["updated_at"], 1234.5)
        self.assertEqual(state["mode"], "pulse")
        self.assertEqual(state["output_mode"], "modulated")
        self.assertTrue(state["period_stable"])
        self.assertAlmostEqual(state["trigger_frequency_hz"], gui.phase_to_hz(trig_step))
        self.assertAlmostEqual(state["frequency_shift_hz"], gui.phase_to_hz(shift_step))
        self.assertAlmostEqual(state["expected_peak_hz"], abs(gui.phase_to_hz(shift_step)))
        self.assertAlmostEqual(state["pulse_freq_shift_hz"], gui.phase_to_hz(shift_step))
        self.assertIsNone(state["harmonic_freq_shift_hz"])
        self.assertTrue(state["edge_lock"])
        self.assertEqual(state["osc_phase_preload"], 123)
        period_cycles = (1 << gui.PHASE_BITS) // gui.hz_to_phase(1_000_000.0)
        self.assertAlmostEqual(state["duty_cycle_pct"], 100.0 * 62 / period_cycles)

    def test_osc_state_reports_strobe_scan_fields(self):
        dwell_cycles = round(0.1 * gui.CLK_HZ)

        state = gui._confirmed_state(
            {
                "control": gui.CTRL_ENABLE | gui.CTRL_OSC_MODE,
                "harmonic_mode": 0,
                "osc_mode": 1,
                "period_stable": 1,
                "trig_phase_step": gui.trig_hz_to_phase_step(500.0),
                "phase_step_offset": gui.strobe_step_word(0.05),
                "phase_step_base": gui.hz_to_phase(1_000_000.0),
                "phase_step": gui.hz_to_phase(1_000_000.0),
                "dwell_cycles": dwell_cycles,
                "n_steps": 10,
                "step_index": 3,
                "strobe_done": 0,
            },
            connected=True,
            sequence=1,
            now=1.0,
        )

        self.assertEqual(state["mode"], "osc")
        # constant phase per point: no beat peak in osc mode
        self.assertEqual(state["expected_peak_hz"], 0.0)
        self.assertEqual(state["dwell_cycles"], dwell_cycles)
        self.assertEqual(state["n_steps"], 10)
        self.assertEqual(state["step_index"], 3)
        self.assertFalse(state["strobe_done"])

    def test_disconnected_state_is_not_hardware_confirmed(self):
        state = gui._confirmed_state(None, connected=False, sequence=2, now=5.0)

        self.assertFalse(state["connected"])
        self.assertFalse(state["hardware_confirmed"])
        self.assertEqual(state["trigger_frequency_hz"], 0.0)
        self.assertEqual(state["expected_peak_hz"], 0.0)

    def test_main_window_publishes_status_acknowledgements(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "rp_state.json"
            old_path = gui.MainWindow._STATE_FILE
            gui.MainWindow._STATE_FILE = state_path
            self.addCleanup(setattr, gui.MainWindow, "_STATE_FILE", old_path)
            win = gui.MainWindow()
            self.addCleanup(win.close)
            win._be._live = True

            win._be.sig_status.emit({
                "control": gui.CTRL_ENABLE,
                "harmonic_mode": 0,
                "period_stable": 1,
                "trig_phase_step": gui.trig_hz_to_phase_step(500.0),
                "phase_step_offset": gui.hz_to_phase(37.0),
            })
            self.app.processEvents()

            state = json.loads(state_path.read_text())
            self.assertTrue(state["hardware_confirmed"])
            self.assertAlmostEqual(state["expected_peak_hz"], 37.0, places=5)


class TestMeasurementWindowField(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.backend = _FakeBackend()
        self.panel = gui.PulsePanel(self.backend, lambda _msg: None)
        self.panel._live = True
        self.panel._output_mode = "modulated"
        self.panel._update_mode_controls()
        self.panel.show()
        self.addCleanup(self.panel.close)

    def test_enter_commits_integer_milliseconds_as_microseconds(self):
        self.panel._window_field.setText("250")
        self.panel._window_field.returnPressed.emit()
        self.assertEqual(self.backend.window_calls[-1], 250_000)

    def test_focus_loss_commits_integer_milliseconds_as_microseconds(self):
        self.panel._window_field.setFocus()
        self.app.processEvents()
        self.panel._window_field.setText("25")
        self.panel._sp_offset.setFocus()
        self.app.processEvents()
        self.assertEqual(self.backend.window_calls[-1], 25_000)

    def test_sub_one_millisecond_input_clamps_to_one_millisecond(self):
        self.panel._window_field.setText("0")
        self.panel._window_field.returnPressed.emit()
        self.assertEqual(self.backend.window_calls[-1], 1_000)
        self.assertEqual(self.panel._window_field.text(), "1")

    def test_empty_input_reverts_to_previous_valid_value(self):
        self.panel._set_window_field_ms(100)
        self.panel._window_field.setFocus()
        self.app.processEvents()
        self.panel._window_field.setText("")
        self.panel._sp_offset.setFocus()
        self.app.processEvents()
        self.assertEqual(self.panel._window_field.text(), "100")
        self.assertEqual(self.backend.window_calls, [])

    def test_input_frequency_readout_refreshes_on_every_poll(self):
        self.panel._on_connected()

        first_hz = 1_000.0
        second_hz = 2_000.0
        self.backend.sig_status.emit({
            "harmonic_mode": 0,
            "control": 1,
            "period_stable": True,
            "phase_step_base": gui.hz_to_phase(first_hz),
        })
        self.app.processEvents()
        self.assertEqual(self.panel._d_in._val.text(), gui.fmt_freq(first_hz))

        self.backend.sig_status.emit({
            "harmonic_mode": 0,
            "control": 1,
            "period_stable": True,
            "phase_step_base": gui.hz_to_phase(second_hz),
        })
        self.app.processEvents()
        self.assertEqual(self.panel._d_in._val.text(), gui.fmt_freq(second_hz))


class TestEdgeLockResponseSelector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.backend = _FakeBackend()
        self.panel = gui.PulsePanel(self.backend, lambda _msg: None)
        self.panel._live = True
        self.panel._period_c = 128
        self.addCleanup(self.panel.close)

    def test_defaults_to_balanced_and_is_only_enabled_while_modulated(self):
        self.assertIsInstance(self.panel._edge_response, QComboBox)
        self.assertEqual(
            self.panel._edge_response.currentData(),
            gui.CTRL_EDGE_RESPONSE_BALANCED,
        )
        self.panel._output_mode = "off"
        self.panel._update_mode_controls()
        self.assertFalse(self.panel._edge_response.isEnabled())
        self.panel._output_mode = "modulated"
        self.panel._update_mode_controls()
        self.assertTrue(self.panel._edge_response.isEnabled())

    def test_apply_forwards_selected_response(self):
        self.panel._edge_response.setCurrentIndex(
            self.panel._edge_response.findData(gui.CTRL_EDGE_RESPONSE_FAST)
        )
        self.panel._do_apply()
        self.assertEqual(
            self.backend.pulse_calls[-1][1]["edge_response"],
            gui.CTRL_EDGE_RESPONSE_FAST,
        )

    def test_off_and_on_control_writes_preserve_selected_response(self):
        self.panel._set_output_mode("off")
        self.assertEqual(
            self.backend.control_calls[-1], gui.CTRL_EDGE_RESPONSE_BALANCED
        )
        self.panel._set_output_mode("on")
        self.assertEqual(
            self.backend.control_calls[-1],
            gui.CTRL_FORCE_HIGH | gui.CTRL_EDGE_RESPONSE_BALANCED,
        )

    def test_status_readback_syncs_selector_from_raw_control(self):
        changed = QSignalSpy(self.panel._edge_response.currentIndexChanged)
        self.panel._on_status({
            "harmonic_mode": 0,
            "control": gui.CTRL_ENABLE | gui.CTRL_EDGE_RESPONSE_SMOOTH,
            "phase_step_base": 0,
        })
        self.assertEqual(
            self.panel._edge_response.currentData(),
            gui.CTRL_EDGE_RESPONSE_SMOOTH,
        )
        self.assertEqual(changed.count(), 0)


class TestGitUpdateHelpers(unittest.TestCase):
    def test_parse_remote_branches_filters_head_pointer(self):
        output = "origin/HEAD -> origin/main\norigin/main\norigin/feature\n\n"
        self.assertEqual(
            gui._parse_remote_branches(output),
            ["origin/feature", "origin/main"],
        )

    def test_fetch_remote_branches_fetches_before_listing(self):
        responses = {
            ("git", "fetch", "--prune", "origin"): [
                SimpleNamespace(stdout="", stderr="", returncode=0),
            ],
            ("git", "branch", "-r"): [
                SimpleNamespace(stdout="origin/HEAD -> origin/main\norigin/main\n", stderr="", returncode=0),
            ],
        }
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(tuple(cmd))
            return responses[tuple(cmd)].pop(0)

        self.assertEqual(gui._fetch_remote_branches(Path("/repo"), run=fake_run), ["origin/main"])
        self.assertEqual(calls, [("git", "fetch", "--prune", "origin"), ("git", "branch", "-r")])

    def test_run_git_update_switches_branch_and_reports_restart_needed(self):
        responses = {
            ("git", "rev-parse", "HEAD"): [
                SimpleNamespace(stdout="old-head\n", stderr="", returncode=0),
                SimpleNamespace(stdout="new-head\n", stderr="", returncode=0),
            ],
            ("git", "fetch", "--prune", "origin"): [
                SimpleNamespace(stdout="", stderr="", returncode=0),
            ],
            ("git", "status", "--porcelain", "--", "rp_state.json"): [
                SimpleNamespace(stdout="", stderr="", returncode=0),
            ],
            ("git", "branch", "--show-current"): [
                SimpleNamespace(stdout="feature\n", stderr="", returncode=0),
            ],
            ("git", "checkout", "main"): [
                SimpleNamespace(stdout="Switched to branch 'main'\n", stderr="", returncode=0),
            ],
            ("git", "branch", "--set-upstream-to", "origin/main", "main"): [
                SimpleNamespace(stdout="branch 'main' set up to track 'origin/main'\n", stderr="", returncode=0),
            ],
            ("git", "pull", "--ff-only"): [
                SimpleNamespace(stdout="Updating old-head..new-head\nFast-forward\n", stderr="", returncode=0),
            ],
            ("git", "log", "old-head..new-head", "--pretty=format:• %s", "--no-merges"): [
                SimpleNamespace(stdout="• Fix updater\n", stderr="", returncode=0),
            ],
        }
        calls = []

        def fake_run(cmd, **_kwargs):
            key = tuple(cmd)
            calls.append(key)
            queue = responses.get(key)
            if not queue:
                raise AssertionError(f"Unexpected command: {cmd}")
            return queue.pop(0)

        msg, restart_needed = gui._run_git_update(gui._APP_DIR, "origin/main", run=fake_run)

        self.assertTrue(restart_needed)
        self.assertEqual(msg, "• Fix updater")
        self.assertEqual(
            calls,
            [
                ("git", "rev-parse", "HEAD"),
                ("git", "status", "--porcelain", "--", "rp_state.json"),
                ("git", "fetch", "--prune", "origin"),
                ("git", "branch", "--show-current"),
                ("git", "checkout", "main"),
                ("git", "branch", "--set-upstream-to", "origin/main", "main"),
                ("git", "pull", "--ff-only"),
                ("git", "rev-parse", "HEAD"),
                ("git", "log", "old-head..new-head", "--pretty=format:• %s", "--no-merges"),
            ],
        )

    def test_cleanup_legacy_repo_state_restores_tracked_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            legacy_state = repo_dir / "rp_state.json"
            legacy_state.write_text('{"dirty": true}\n')
            responses = {
                ("git", "status", "--porcelain", "--", "rp_state.json"): [
                    SimpleNamespace(stdout=" M rp_state.json\n", stderr="", returncode=0),
                ],
                ("git", "restore", "--source=HEAD", "--staged", "--worktree", "--", "rp_state.json"): [
                    SimpleNamespace(stdout="", stderr="", returncode=0),
                ],
            }
            calls = []

            def fake_run(cmd, **_kwargs):
                key = tuple(cmd)
                calls.append(key)
                queue = responses.get(key)
                if not queue:
                    raise AssertionError(f"Unexpected command: {cmd}")
                return queue.pop(0)

            msg = gui._cleanup_legacy_repo_state(repo_dir, run=fake_run)

            self.assertEqual(msg, "Restored legacy repo state file.")
            self.assertEqual(
                calls,
                [
                    ("git", "status", "--porcelain", "--", "rp_state.json"),
                    ("git", "restore", "--source=HEAD", "--staged", "--worktree", "--", "rp_state.json"),
                ],
            )

    def test_state_file_lives_outside_repo_root(self):
        self.assertNotEqual(gui.MainWindow._STATE_FILE.parent, gui._APP_DIR)


if __name__ == "__main__":
    unittest.main()
