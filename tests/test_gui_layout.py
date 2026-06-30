#!/usr/bin/env python3
"""Smoke tests for the PySide6 GUI layout."""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
