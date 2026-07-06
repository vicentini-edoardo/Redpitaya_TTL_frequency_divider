#!/usr/bin/env python3
"""Smoke tests for the PySide6 GUI layout."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

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

    def set_window(self, window_us: int):
        self.window_calls.append(window_us)


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


class TestGitUpdateHelpers(unittest.TestCase):
    def test_parse_remote_branches_filters_head_pointer(self):
        output = "origin/HEAD -> origin/main\norigin/main\norigin/feature\n\n"
        self.assertEqual(
            gui._parse_remote_branches(output),
            ["origin/feature", "origin/main"],
        )

    def test_run_git_update_switches_branch_and_reports_restart_needed(self):
        responses = {
            ("git", "rev-parse", "HEAD"): [
                SimpleNamespace(stdout="old-head\n", stderr="", returncode=0),
                SimpleNamespace(stdout="new-head\n", stderr="", returncode=0),
            ],
            ("git", "fetch", "origin"): [
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
        self.assertIn("Fast-forward", msg)
        self.assertEqual(
            calls,
            [
                ("git", "rev-parse", "HEAD"),
                ("git", "status", "--porcelain", "--", "rp_state.json"),
                ("git", "fetch", "origin"),
                ("git", "branch", "--show-current"),
                ("git", "checkout", "main"),
                ("git", "branch", "--set-upstream-to", "origin/main", "main"),
                ("git", "pull", "--ff-only"),
                ("git", "rev-parse", "HEAD"),
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
