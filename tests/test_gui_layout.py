#!/usr/bin/env python3
"""Smoke tests for the PySide6 GUI layout."""
import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
