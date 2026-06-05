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


if __name__ == "__main__":
    unittest.main()
