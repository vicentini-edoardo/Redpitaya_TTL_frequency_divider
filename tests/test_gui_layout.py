#!/usr/bin/env python3
"""Smoke tests for the PySide6 GUI layout."""
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QWidget  # noqa: E402

import redpitaya_combined_gui_qt as gui  # noqa: E402


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

    def test_update_branch_selector_lists_remote_branches(self):
        with patch.object(gui, "_git_remote_branches", return_value=["main", "feature"]), \
             patch.object(gui, "_git_current_branch", return_value="feature"):
            win = gui.MainWindow()
        self.addCleanup(win.close)

        selector = win.findChild(QComboBox, "rpUpdateBranch")
        self.assertIsNotNone(selector)
        self.assertEqual(
            [selector.itemText(i) for i in range(selector.count())],
            ["main", "feature"],
        )
        self.assertEqual(selector.currentText(), "feature")

    def test_git_update_commands_switch_and_fast_forward_selected_branch(self):
        self.assertEqual(
            gui._git_update_commands("feature"),
            [
                ["git", "fetch", "origin", "--prune"],
                ["git", "checkout", "feature"],
                ["git", "pull", "--ff-only"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
