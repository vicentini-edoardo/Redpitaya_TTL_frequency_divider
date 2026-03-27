#!/usr/bin/env python3
"""
redpitaya_pulse_gui_qt.py — PySide6 desktop GUI for the Red Pitaya pulse generator.

This is the Qt-based parallel rewrite of the original Tkinter GUI. It preserves
the same hardware semantics and remote helper contract while replacing the
visual layer with a custom-painted cyberpunk dashboard.

Run with:  python3 redpitaya_pulse_gui_qt.py
Requires: PySide6
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal

try:
    from PySide6.QtCore import QObject, QPointF, QRectF, QTimer, Qt, Signal
    from PySide6.QtGui import (
        QAction,
        QColor,
        QDoubleValidator,
        QFont,
        QIntValidator,
        QKeySequence,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "PySide6 is required. Create a local environment and install it with:\n"
        "python3 -m venv .venv && .venv/bin/python -m pip install PySide6-Essentials"
    ) from exc


CLOCK_HZ = 125_000_000
BASE_ADDR = 0x40600000
REMOTE_BIN = "/root/rp_pulse_ctl"
REMOTE_FPGAUTIL = "/opt/redpitaya/bin/fpgautil"
REMOTE_BITFILE = "/root/red_pitaya_top.bit.bin"

DIV_MIN = 1
DIV_MAX = 32
WIDTH_MIN = 1
DELAY_MIN = 1
MOD_FREQ_MIN_HZ = 0.0
MOD_FREQ_MAX_HZ = 5_000.0
MOD_AMP_MIN = 0.0
MOD_AMP_MAX = 1.0
MOD_AMP_Q15_MAX = 32767

CONTROL_PULSE_ENABLE = 0x1
CONTROL_SOFT_RESET = 0x2
CONTROL_PHASE_MOD_ENABLE = 0x4

CLR_BG = "#050a0f"
CLR_BG_2 = "#09121d"
CLR_SURFACE = "#0d1117"
CLR_PANEL = "#09111d"
CLR_BORDER = "#11c6de"
CLR_BORDER_MAGENTA = "#ff2dcb"
CLR_BORDER_DIM = "#0b7d92"
CLR_ACCENT = "#24d5e8"
CLR_ACCENT2 = "#ff37d8"
CLR_SUCCESS = "#2cffc7"
CLR_WARN = "#ff4d85"
CLR_TEXT = "#dff3ff"
CLR_MUTED = "#97adc6"
CLR_SOFT = "#243248"
CLR_ENTRY_BG = "#09131e"
CLR_GRID = "#183445"
MONO_FONT_FAMILY = "Menlo"


def fmt_freq_hz(freq_hz: float) -> str:
    if freq_hz >= 1e6:
        return f"{freq_hz / 1e6:.6g} MHz"
    if freq_hz >= 1e3:
        return f"{freq_hz / 1e3:.6g} kHz"
    return f"{freq_hz:.6g} Hz"


def fmt_time_s(value_s: float) -> str:
    if value_s >= 1:
        return f"{value_s:.6g} s"
    if value_s >= 1e-3:
        return f"{value_s * 1e3:.6g} ms"
    if value_s >= 1e-6:
        return f"{value_s * 1e6:.6g} us"
    return f"{value_s * 1e9:.6g} ns"


def frac_to_cycles(frac: float, period_cycles: int) -> int:
    return max(WIDTH_MIN, min(period_cycles, round(frac * period_cycles)))


def cycles_to_frac(cycles: int, period_cycles: int) -> float:
    return cycles / period_cycles if period_cycles > 0 else 0.0


def deg_to_cycles(deg: float, period_cycles: int) -> int:
    max_delay = max(DELAY_MIN, period_cycles // 2)
    return max(DELAY_MIN, min(max_delay, round((deg / 360.0) * period_cycles)))


def cycles_to_deg(cycles: int, period_cycles: int) -> float:
    return (cycles / period_cycles) * 360.0 if period_cycles > 0 else 0.0


def clamp_mod_freq_hz(freq_hz: float) -> float:
    return max(MOD_FREQ_MIN_HZ, min(MOD_FREQ_MAX_HZ, freq_hz))


def mod_freq_to_word(freq_hz: float) -> int:
    return int(clamp_mod_freq_hz(freq_hz) * (2**32) / CLOCK_HZ)


def clamp_mod_amp(value: float) -> float:
    return max(MOD_AMP_MIN, min(MOD_AMP_MAX, value))


def mod_amp_to_q15(value: float) -> int:
    return int(round(clamp_mod_amp(value) * MOD_AMP_Q15_MAX))


def q15_to_mod_amp(value: int) -> float:
    return clamp_mod_amp(value / MOD_AMP_Q15_MAX) if MOD_AMP_Q15_MAX > 0 else 0.0


@dataclass
class ApplyState:
    divider: int
    width_cycles: int
    delay_cycles: int
    phase_freq_word: int
    phase_amp_q15: int
    control_word: int


class RemoteCtl:
    def __init__(self):
        self.host = ""
        self.user = ""
        self.port = 22

    def connect(self, host: str, user: str, port: int):
        if not shutil.which("ssh"):
            raise RuntimeError("OpenSSH client not found on this PC.")
        self.host = host
        self.user = user
        self.port = port

    def run(self, cmd: str):
        ssh_cmd = ["ssh", "-p", str(self.port), f"{self.user}@{self.host}", cmd]
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "SSH command failed.")
        return proc.stdout.strip()

    def helper(self, base_addr: int, command: str, *args: int):
        remote_cmd = " ".join(
            [shlex.quote(REMOTE_BIN), shlex.quote(hex(base_addr)), shlex.quote(command)]
            + [shlex.quote(str(a)) for a in args]
        )
        return json.loads(self.run(remote_cmd))

    def upload_bitfile(self, local_path: str):
        if not shutil.which("scp"):
            raise RuntimeError("scp not found on this PC.")
        scp_cmd = ["scp", "-P", str(self.port), local_path, f"{self.user}@{self.host}:{REMOTE_BITFILE}"]
        proc = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"scp failed: {proc.stderr.strip() or proc.stdout.strip()}")
        self.run(f"{REMOTE_FPGAUTIL} -b {REMOTE_BITFILE}")

    def upload_and_compile(self, local_src: str, remote_src: str = "/root/rp_pulse_ctl.c"):
        if not shutil.which("scp"):
            raise RuntimeError("scp not found on this PC.")
        scp_cmd = ["scp", "-P", str(self.port), local_src, f"{self.user}@{self.host}:{remote_src}"]
        proc = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"scp failed: {proc.stderr.strip() or proc.stdout.strip()}")
        compile_cmd = f"gcc -O2 -o {shlex.quote(REMOTE_BIN)} {shlex.quote(remote_src)}"
        return self.run(compile_cmd)


class JobSignals(QObject):
    result = Signal(int, object)
    error = Signal(int, str)
    finished = Signal(int)


class BackgroundWidget(QWidget):
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = self.rect()
        grad = QLinearGradient(0, 0, rect.width(), rect.height())
        grad.setColorAt(0.0, QColor(CLR_BG))
        grad.setColorAt(0.45, QColor(CLR_BG_2))
        grad.setColorAt(1.0, QColor("#04070d"))
        painter.fillRect(rect, grad)

        painter.setOpacity(0.22)
        pen = QPen(QColor(CLR_GRID), 1)
        painter.setPen(pen)
        for y in range(16, rect.height(), 28):
            painter.drawLine(0, y, rect.width(), y)

        painter.setOpacity(0.14)
        painter.setPen(QPen(QColor(CLR_BORDER), 1))
        for i in range(8):
            y = 40 + i * 112
            painter.drawLine(30, y, rect.width() - 40, y + (i % 2) * 6)

        painter.setPen(QPen(QColor(CLR_BORDER_MAGENTA), 2))
        painter.setOpacity(0.35)
        for x, y, w in [(80, 54, 120), (rect.width() - 240, 84, 140), (140, rect.height() - 110, 180)]:
            painter.drawLine(x, y, x + w, y)


class CyberPanel(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 28, 24, 20)
        outer.setSpacing(12)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("panelTitle")
        outer.addWidget(self.title_label)

        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(12)
        outer.addWidget(self.content_widget)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(2, 2, -2, -2)

        panel_grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        panel_grad.setColorAt(0.0, QColor(9, 17, 29, 220))
        panel_grad.setColorAt(1.0, QColor(7, 10, 18, 230))
        painter.fillRect(rect, panel_grad)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 10))
        painter.drawRect(rect.adjusted(8, 8, -8, -8))

        path = QPainterPath()
        chamfer = 18
        x1, y1, x2, y2 = rect.left(), rect.top(), rect.right(), rect.bottom()
        path.moveTo(x1 + chamfer, y1)
        path.lineTo(x2 - chamfer, y1)
        path.lineTo(x2, y1 + chamfer)
        path.lineTo(x2, y2 - chamfer)
        path.lineTo(x2 - chamfer, y2)
        path.lineTo(x1 + chamfer, y2)
        path.lineTo(x1, y2 - chamfer)
        path.lineTo(x1, y1 + chamfer)
        path.closeSubpath()

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(CLR_BORDER), 2.0))
        painter.drawPath(path)

        painter.setPen(QPen(QColor(CLR_BORDER_MAGENTA), 1.4))
        painter.drawLine(rect.right() - 120, rect.top() + 10, rect.right() - 18, rect.top() + 10)
        painter.drawLine(rect.left() + 18, rect.bottom() - 14, rect.left() + 120, rect.bottom() - 14)

        painter.setOpacity(0.38)
        painter.setPen(QPen(QColor(CLR_BORDER), 6))
        painter.drawLine(rect.left() + 8, rect.top() + 18, rect.left() + 60, rect.top() + 18)
        painter.drawLine(rect.right() - 42, rect.bottom() - 18, rect.right() - 18, rect.bottom() - 18)

        painter.setOpacity(1.0)


class StatCard(QFrame):
    def __init__(self, title: str, accent: str, parent=None):
        super().__init__(parent)
        self.accent = accent
        self.setObjectName("statCard")
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(10)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("statTitle")
        layout.addWidget(self.title_label)

        self.value_label = QLabel("—")
        self.value_label.setObjectName("statValue")
        self.value_label.setStyleSheet(f"color: {accent};")
        layout.addWidget(self.value_label)

        self.footer_label = QLabel("")
        self.footer_label.setObjectName("statFooter")
        layout.addWidget(self.footer_label)
        layout.addStretch(1)

    def set_value(self, text: str):
        self.value_label.setText(text)

    def set_footer(self, text: str):
        self.footer_label.setText(text)


class ToggleButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(34)
        self.setObjectName("toggleButton")


class DividerControl(QWidget):
    valueChanged = Signal(int)

    def __init__(self, minimum: int, maximum: int, parent=None):
        super().__init__(parent)
        self.minimum = minimum
        self.maximum = maximum
        self._value = minimum

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.dec_btn = QPushButton("‹")
        self.inc_btn = QPushButton("›")
        for btn in (self.dec_btn, self.inc_btn):
            btn.setFixedSize(44, 40)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setObjectName("stepButton")
            layout.addWidget(btn)

        self.entry = QLineEdit(str(minimum))
        self.entry.setAlignment(Qt.AlignCenter)
        self.entry.setValidator(QIntValidator(minimum, maximum, self))
        self.entry.setFixedWidth(86)
        self.entry.setObjectName("valueBox")
        layout.insertWidget(1, self.entry)

        self.dec_btn.clicked.connect(lambda: self.setValue(self._value - 1))
        self.inc_btn.clicked.connect(lambda: self.setValue(self._value + 1))
        self.entry.editingFinished.connect(self._emit_entry)

    def _emit_entry(self):
        try:
            value = int(self.entry.text())
        except ValueError:
            value = self._value
        self.setValue(value)

    def value(self) -> int:
        return self._value

    def setValue(self, value: int):
        value = max(self.minimum, min(self.maximum, int(value)))
        if value == self._value and self.entry.text() == str(value):
            return
        self._value = value
        self.entry.setText(str(value))
        self.valueChanged.emit(value)


class ParameterSlider(QWidget):
    valueChanged = Signal(float)
    valueCommitted = Signal(float)

    def __init__(
        self,
        title: str,
        minimum: float,
        maximum: float,
        step: float,
        decimals: int,
        suffix_label: str = "",
        display_factor: float = 1.0,
        display_suffix: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.decimals = decimals
        self._internal_decimals = max(decimals, self._decimal_places(step))
        self.display_factor = display_factor
        self.display_suffix = display_suffix
        self._value = minimum

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("paramTitle")
        layout.addWidget(self.title_label, 0, 0)

        self.value_box = QLineEdit()
        self.value_box.setAlignment(Qt.AlignCenter)
        self.value_box.setObjectName("valueBox")
        self.value_box.setFixedWidth(88)
        display_min = minimum * self.display_factor
        display_max = maximum * self.display_factor
        if decimals == 0:
            self.value_box.setValidator(QIntValidator(int(display_min), int(display_max), self))
        else:
            validator = QDoubleValidator(display_min, display_max, decimals, self)
            validator.setNotation(QDoubleValidator.StandardNotation)
            self.value_box.setValidator(validator)
        layout.addWidget(self.value_box, 0, 2, 2, 1)

        self.slider = QFrame()
        self.slider.setObjectName("sliderTrack")
        self.slider.setMinimumHeight(18)
        self.slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.slider.mousePressEvent = self._slider_mouse_press
        self.slider.mouseMoveEvent = self._slider_mouse_move
        self.slider.paintEvent = self._paint_slider
        layout.addWidget(self.slider, 0, 1)

        self.detail_label = QLabel(suffix_label)
        self.detail_label.setObjectName("paramDetail")
        layout.addWidget(self.detail_label, 1, 1)

        self.value_box.textEdited.connect(self._sync_from_text)
        self.value_box.editingFinished.connect(self._entry_changed)

    @staticmethod
    def _decimal_places(value: float) -> int:
        decimal_value = Decimal(str(value)).normalize()
        return max(0, -decimal_value.as_tuple().exponent)

    def _normalize_value(self, value: float, snap: bool = False) -> float:
        value = max(self.minimum, min(self.maximum, value))
        if self.decimals == 0:
            return int(round(value))
        if snap:
            value = round(value / self.step) * self.step
        return round(value, self._internal_decimals)

    def _set_internal_value(self, value: float):
        if self._value == value:
            self.slider.update()
            return
        self._value = value
        self.slider.update()
        self.valueChanged.emit(float(value))

    def _format_display_value(self, value: float) -> str:
        display_value = value * self.display_factor
        return f"{display_value:.{self.decimals}f}{self.display_suffix}"

    def _parse_display_value(self, raw_text: str) -> float:
        raw = raw_text.strip()
        if self.display_suffix and raw.endswith(self.display_suffix):
            raw = raw[: -len(self.display_suffix)].strip()
        return float(raw) / self.display_factor

    def _sync_from_text(self, raw_text: str):
        if not raw_text.strip():
            return
        try:
            value = self._parse_display_value(raw_text)
        except ValueError:
            return
        if self.minimum <= value <= self.maximum:
            self._set_internal_value(self._normalize_value(value, snap=False))

    def _entry_changed(self):
        try:
            value = self._parse_display_value(self.value_box.text())
        except ValueError:
            value = self._value
        self.setValue(value, snap=False)
        self.valueCommitted.emit(float(self._value))

    def _slider_mouse_press(self, event):
        self._set_from_pos(event.position().x())

    def _slider_mouse_move(self, event):
        if event.buttons() & Qt.LeftButton:
            self._set_from_pos(event.position().x())

    def _set_from_pos(self, x_pos: float):
        usable = max(1.0, self.slider.width() - 20.0)
        t = min(1.0, max(0.0, (x_pos - 10.0) / usable))
        value = self.minimum + t * (self.maximum - self.minimum)
        snapped = round(value / self.step) * self.step
        self.setValue(snapped, snap=True)

    def _paint_slider(self, _event):
        painter = QPainter(self.slider)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.slider.rect().adjusted(2, 2, -2, -2)

        track_rect = QRectF(rect.left() + 8, rect.center().y() - 3, rect.width() - 16, 6)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(CLR_SOFT))
        painter.drawRoundedRect(track_rect, 3, 3)

        t = 0.0 if self.maximum <= self.minimum else (self._value - self.minimum) / (self.maximum - self.minimum)
        filled = QRectF(track_rect.left(), track_rect.top(), max(10.0, track_rect.width() * t), track_rect.height())
        grad = QLinearGradient(filled.topLeft(), filled.topRight())
        grad.setColorAt(0.0, QColor(CLR_ACCENT))
        grad.setColorAt(1.0, QColor("#7fffff"))
        painter.setBrush(grad)
        painter.drawRoundedRect(filled, 3, 3)

        knob_x = track_rect.left() + track_rect.width() * t
        glow_pen = QPen(QColor(CLR_ACCENT), 8)
        glow_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(glow_pen)
        painter.drawPoint(QPointF(knob_x, track_rect.center().y()))
        painter.setPen(QPen(QColor("#b8ffff"), 3))
        painter.drawPoint(QPointF(knob_x, track_rect.center().y()))

    def set_detail(self, text: str):
        self.detail_label.setText(text)

    def value(self) -> float:
        return self._value

    def setValue(self, value: float, snap: bool = False):
        value = self._normalize_value(value, snap=snap)
        self.value_box.setText(self._format_display_value(value))
        self._set_internal_value(value)


class WaveformPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMaximumHeight(220)
        self.divider = 1
        self.width_frac = 0.5
        self.delay_deg = 0.0
        self.phase_mod_enabled = False
        self.mod_freq_hz = 0.0

    def set_state(
        self,
        divider: int,
        width_frac: float,
        delay_deg: float,
        phase_mod_enabled: bool = False,
        mod_freq_hz: float = 0.0,
    ):
        self.divider = max(1, divider)
        self.width_frac = max(0.001, min(0.999, width_frac))
        self.delay_deg = max(0.0, min(180.0, delay_deg))
        self.phase_mod_enabled = phase_mod_enabled
        self.mod_freq_hz = max(0.0, mod_freq_hz)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(14, 10, -14, -22)

        panel_grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        panel_grad.setColorAt(0.0, QColor(6, 11, 19, 210))
        panel_grad.setColorAt(1.0, QColor(7, 9, 15, 230))
        painter.fillRect(rect, panel_grad)

        painter.setPen(QPen(QColor(CLR_SOFT), 1))
        for i in range(10):
            x = rect.left() + int(i * rect.width() / 10)
            painter.drawLine(x, rect.top() + 12, x, rect.bottom() - 30)
        for i in range(5):
            y = rect.top() + 20 + int(i * (rect.height() - 60) / 4)
            painter.drawLine(rect.left() + 20, y, rect.right() - 10, y)

        left = rect.left() + 98
        right = rect.right() - 18
        top = rect.top() + 26
        mid = rect.top() + 108
        bot = rect.top() + 194
        track_w = max(40, right - left)
        n_in = 32
        in_pw = track_w / n_in

        painter.setPen(QPen(QColor(CLR_MUTED), 1))
        input_font = QFont(MONO_FONT_FAMILY, 10)
        input_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        painter.setFont(input_font)
        painter.drawText(QRectF(rect.left() + 8, top + 12, 80, 24), "INPUT")
        painter.setPen(QPen(QColor(CLR_ACCENT), 1))
        painter.drawText(QRectF(rect.left() + 2, mid + 16, 86, 24), "OUTPUT")

        y_in_hi, y_in_lo = top + 4, top + 52
        y_out_hi, y_out_lo = mid + 2, mid + 58
        for y in (y_in_hi, y_in_lo, y_out_hi, y_out_lo):
            painter.setPen(QPen(QColor(CLR_GRID), 1, Qt.DashLine))
            painter.drawLine(left, y, right, y)

        painter.setPen(QPen(QColor(CLR_MUTED), 1.4))
        x = float(left)
        for _ in range(n_in):
            mid_x = x + in_pw / 2
            points = [
                QPointF(x, y_in_lo),
                QPointF(x, y_in_hi),
                QPointF(mid_x, y_in_hi),
                QPointF(mid_x, y_in_lo),
                QPointF(x + in_pw, y_in_lo),
            ]
            for p1, p2 in zip(points, points[1:]):
                painter.drawLine(p1, p2)
            x += in_pw

        out_pw = in_pw * self.divider
        n_out = max(1, int(n_in / self.divider))
        x = float(left)
        delay_frac = self.delay_deg / 360.0
        for _ in range(n_out):
            d_px = out_pw * delay_frac
            h_px = out_pw * self.width_frac
            points = [
                QPointF(x, y_out_lo),
                QPointF(x + d_px, y_out_lo),
                QPointF(x + d_px, y_out_hi),
                QPointF(x + d_px + h_px, y_out_hi),
                QPointF(x + d_px + h_px, y_out_lo),
                QPointF(x + out_pw, y_out_lo),
            ]
            painter.setPen(QPen(QColor(CLR_BORDER_DIM), 5))
            for p1, p2 in zip(points, points[1:]):
                painter.drawLine(p1, p2)
            painter.setPen(QPen(QColor(CLR_ACCENT), 2))
            for p1, p2 in zip(points, points[1:]):
                painter.drawLine(p1, p2)
            x += out_pw

        caption_y = rect.bottom() - 6
        painter.setPen(QPen(QColor(CLR_TEXT), 1))
        caption_font = QFont(MONO_FONT_FAMILY, 10)
        caption_font.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
        painter.setFont(caption_font)
        painter.drawText(
            QRectF(left, caption_y - 20, track_w, 20),
            Qt.AlignCenter,
            (
                f"÷{self.divider}    |    duty {self.width_frac * 100:.1f}% of input period"
                f"    |    phase mod {fmt_freq_hz(self.mod_freq_hz)} sweep 0..T"
                if self.phase_mod_enabled
                else f"÷{self.divider}    |    duty {self.width_frac * 100:.1f}% of input period    |    delay {self.delay_deg:.1f}°"
            ),
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Red Pitaya Pulse Control")
        self.resize(1240, 760)
        self.setMinimumSize(980, 620)

        self.remote = RemoteCtl()
        self.connected = False
        self.base_addr = BASE_ADDR
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rpctl")
        self.job_signals = JobSignals()
        self.job_signals.result.connect(self._handle_job_result)
        self.job_signals.error.connect(self._handle_job_error)
        self.job_signals.finished.connect(self._handle_job_finished)
        self._next_job_id = 0
        self._job_handlers: dict[int, dict[str, object]] = {}
        self._job_futures: dict[int, Future] = {}
        self._period_cycles = 1
        self._force_period_update = False
        self._apply_in_flight = False
        self._pending_apply_state: ApplyState | None = None
        self._poll_in_flight = False
        self._period_valid = False
        self._timeout_flag = False
        self._phase_freq_clamped = False
        self._mod_freq_dirty = False
        self._mod_amp_dirty = False
        self.waveform: WaveformPreview | None = None

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self._poll_tick)

        self.auto_apply_timer = QTimer(self)
        self.auto_apply_timer.setSingleShot(True)
        self.auto_apply_timer.setInterval(300)
        self.auto_apply_timer.timeout.connect(self._auto_apply_timeout)

        self._build_ui()
        self._wire_shortcuts()
        self._apply_styles()
        self._refresh_preview_and_stats()

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setCentralWidget(scroll)

        bg = BackgroundWidget()
        scroll.setWidget(bg)

        root = QVBoxLayout(bg)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(14)
        root.setAlignment(Qt.AlignTop)

        top_row = QHBoxLayout()
        top_row.setSpacing(16)
        top_row.setAlignment(Qt.AlignTop)
        root.addLayout(top_row)

        self.connection_panel = self._build_connection_panel()
        self.stats_panel = self._build_stats_panel()
        top_row.addWidget(self.connection_panel, 1)
        top_row.addWidget(self.stats_panel, 1)

        mid_row = QHBoxLayout()
        mid_row.setSpacing(16)
        mid_row.setAlignment(Qt.AlignTop)
        root.addLayout(mid_row)

        controls_col = QVBoxLayout()
        controls_col.setSpacing(16)
        self.pulse_controls_panel = self._build_pulse_controls_panel()
        self.modulation_panel = self._build_modulation_panel()
        self.wave_panel = self._build_waveform_panel()
        controls_col.addWidget(self.pulse_controls_panel, 1)
        controls_col.addWidget(self.modulation_panel, 1)
        mid_row.addLayout(controls_col, 11)
        mid_row.addWidget(self.wave_panel, 10)

        # Initialize control defaults only after every dependent widget exists.
        self.divider_control.setValue(1)
        self.width_control.setValue(0.5)
        self.delay_control.setValue(0.0)
        self.mod_freq_control.setValue(10.0)
        self.mod_amp_control.setValue(0.7)

        root.addStretch(1)

    def _build_connection_panel(self) -> CyberPanel:
        panel = CyberPanel("CONNECTION")
        layout = panel.content_layout

        row = QGridLayout()
        row.setHorizontalSpacing(12)
        row.setVerticalSpacing(10)
        layout.addLayout(row)

        host_label = QLabel("HOST")
        host_label.setObjectName("fieldLabel")
        row.addWidget(host_label, 0, 0)

        self.host_edit = QLineEdit("rp-f06a51.local")
        self.host_edit.setObjectName("neonEntry")
        row.addWidget(self.host_edit, 0, 1, 1, 2)

        self.connect_btn = QPushButton("CONNECT")
        self.connect_btn.setObjectName("accentButton")
        self.connect_btn.setFixedWidth(230)
        self.connect_btn.clicked.connect(self.connect_to_board)
        row.addWidget(self.connect_btn, 1, 0, 1, 2)

        self.advanced_toggle = QPushButton("▾ ADVANCED")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setObjectName("ghostButton")
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        row.addWidget(self.advanced_toggle, 0, 3, 1, 1, Qt.AlignRight)

        self.status_label = QLabel("Disconnected")
        self.status_label.setObjectName("warnStatus")
        layout.addWidget(self.status_label)

        self.advanced_widget = QWidget()
        self.advanced_widget.setVisible(False)
        adv = QGridLayout(self.advanced_widget)
        adv.setHorizontalSpacing(10)
        adv.setVerticalSpacing(10)

        adv.addWidget(self._make_field_label("PORT"), 0, 0)
        self.port_edit = QLineEdit("22")
        self.port_edit.setValidator(QIntValidator(1, 65535, self))
        self.port_edit.setObjectName("neonEntry")
        adv.addWidget(self.port_edit, 0, 1)

        adv.addWidget(self._make_field_label("USER"), 0, 2)
        self.user_edit = QLineEdit("root")
        self.user_edit.setObjectName("neonEntry")
        adv.addWidget(self.user_edit, 0, 3)

        adv.addWidget(self._make_field_label("BASE ADDRESS"), 0, 4)
        self.base_edit = QLineEdit("0x40600000")
        self.base_edit.setObjectName("neonEntry")
        adv.addWidget(self.base_edit, 0, 5)

        self.readback_btn = self._make_small_button("READ BACK", self.read_back)
        self.soft_reset_btn = self._make_small_button("SOFT RESET", self.soft_reset)
        self.upload_compile_btn = self._make_small_button("UPLOAD & COMPILE", self.upload_and_compile)
        self.upload_bitfile_btn = self._make_small_button("UPLOAD BITFILE", self.upload_bitfile)
        self.force_freq_btn = self._make_small_button("FORCE FREQ UPDATE", self.force_freq_update)

        adv.addWidget(self.readback_btn, 1, 0, 1, 1)
        adv.addWidget(self.soft_reset_btn, 1, 1, 1, 1)
        adv.addWidget(self.upload_compile_btn, 1, 2, 1, 2)
        adv.addWidget(self.upload_bitfile_btn, 1, 4, 1, 1)
        adv.addWidget(self.force_freq_btn, 1, 5, 1, 1)

        self.info_label = QLabel("Connect to read input frequency from hardware.")
        self.info_label.setWordWrap(True)
        self.info_label.setObjectName("infoLabel")
        adv.addWidget(self.info_label, 2, 0, 1, 6)

        self.freq_warning_label = QLabel("")
        self.freq_warning_label.setObjectName("warnStatus")
        adv.addWidget(self.freq_warning_label, 3, 0, 1, 6)

        layout.addWidget(self.advanced_widget)
        return panel

    def _build_stats_panel(self) -> CyberPanel:
        panel = CyberPanel("LIVE STATS")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        panel.content_layout.addLayout(grid)

        self.stat_input = StatCard("INPUT FREQ", CLR_ACCENT)
        self.stat_output = StatCard("OUTPUT FREQ", CLR_SUCCESS)
        self.stat_duty = StatCard("DUTY CYCLE", CLR_ACCENT2)
        self.stat_phase = StatCard("PHASE SHIFT", CLR_TEXT)

        for col, widget in enumerate([self.stat_input, self.stat_output, self.stat_duty, self.stat_phase]):
            grid.addWidget(widget, 0, col)
            grid.setColumnStretch(col, 1)

        return panel

    def _build_pulse_controls_panel(self) -> CyberPanel:
        panel = CyberPanel("PULSE CONTROLS")
        layout = panel.content_layout

        divider_row = QWidget()
        divider_layout = QGridLayout(divider_row)
        divider_layout.setContentsMargins(0, 0, 0, 0)
        divider_layout.setHorizontalSpacing(12)
        divider_layout.setVerticalSpacing(4)
        divider_layout.addWidget(self._make_field_label("Divider"), 0, 0)
        self.divider_control = DividerControl(DIV_MIN, DIV_MAX)
        divider_layout.addWidget(self.divider_control, 0, 1)
        layout.addWidget(divider_row)

        self.width_control = ParameterSlider(
            "Width (duty cycle)",
            0.0,
            1.0,
            0.05,
            1,
            display_factor=100.0,
            display_suffix="%",
        )
        self.delay_control = ParameterSlider("Delay (phase 0–180°)", 0.0, 180.0, 5.0, 1)
        layout.addWidget(self.width_control)
        layout.addWidget(self.delay_control)

        toggles = QHBoxLayout()
        toggles.setSpacing(12)
        self.enable_toggle = ToggleButton("Enable output")
        self.enable_toggle.setChecked(True)
        toggles.addWidget(self.enable_toggle)
        self.auto_apply_toggle = ToggleButton("Auto apply")
        toggles.addWidget(self.auto_apply_toggle)
        toggles.addStretch(1)
        layout.addLayout(toggles)

        layout.addStretch(1)

        self.divider_control.valueChanged.connect(self.on_divider_changed)
        self.width_control.valueChanged.connect(self.on_width_changed)
        self.delay_control.valueChanged.connect(self.on_delay_changed)
        self.width_control.valueCommitted.connect(lambda _value: self.maybe_auto_apply())
        self.delay_control.valueCommitted.connect(lambda _value: self.maybe_auto_apply())
        self.enable_toggle.toggled.connect(lambda _checked: self.maybe_auto_apply())

        return panel

    def _build_modulation_panel(self) -> CyberPanel:
        panel = CyberPanel("MODULATION")
        layout = panel.content_layout

        self.phase_mod_toggle = ToggleButton("Enable phase modulation")
        layout.addWidget(self.phase_mod_toggle)

        self.mod_freq_control = ParameterSlider(
            "Modulation frequency",
            MOD_FREQ_MIN_HZ,
            MOD_FREQ_MAX_HZ,
            10.0,
            0,
            display_suffix=" Hz",
        )
        self.mod_amp_control = ParameterSlider(
            "Modulation amplitude (sweep / T)",
            MOD_AMP_MIN,
            MOD_AMP_MAX,
            0.05,
            2,
            display_factor=100.0,
            display_suffix="%",
        )
        layout.addWidget(self.mod_freq_control)
        layout.addWidget(self.mod_amp_control)

        presets = QHBoxLayout()
        presets.setSpacing(10)
        for label, amp in (("0.7T", 0.7), ("0.8T", 0.8), ("1.0T", 1.0)):
            btn = self._make_small_button(label, lambda _checked=False, value=amp: self.mod_amp_control.setValue(value))
            presets.addWidget(btn)
        presets.addStretch(1)
        layout.addLayout(presets)

        self.apply_btn = QPushButton("APPLY NOW")
        self.apply_btn.setObjectName("accentButton")
        self.apply_btn.setFixedWidth(230)
        self.apply_btn.clicked.connect(self.apply_now)
        layout.addWidget(self.apply_btn)

        layout.addStretch(1)

        self.mod_freq_control.valueChanged.connect(self.on_mod_freq_changed)
        self.mod_amp_control.valueChanged.connect(self.on_mod_amp_changed)
        self.mod_freq_control.valueCommitted.connect(lambda _value: self.maybe_auto_apply())
        self.mod_amp_control.valueCommitted.connect(lambda _value: self.maybe_auto_apply())
        self.phase_mod_toggle.toggled.connect(self.on_phase_mod_toggled)

        return panel

    def _build_waveform_panel(self) -> CyberPanel:
        panel = CyberPanel("WAVEFORM PREVIEW")
        self.waveform = WaveformPreview()
        panel.content_layout.addWidget(self.waveform, 0, Qt.AlignTop)
        panel.content_layout.addStretch(1)
        return panel

    def _wire_shortcuts(self):
        action = QAction(self)
        action.setShortcut(QKeySequence("Ctrl+Return"))
        action.triggered.connect(self.apply_now)
        self.addAction(action)

    def _apply_styles(self):
        self.setStyleSheet(
            f"""
            QWidget {{
                color: {CLR_TEXT};
                font-family: Menlo, Monaco, 'Courier New', monospace;
                font-size: 13px;
            }}
            QLabel#panelTitle {{
                color: {CLR_ACCENT};
                font-size: 22px;
                font-weight: 700;
                letter-spacing: 3px;
            }}
            QLabel#fieldLabel {{
                color: {CLR_MUTED};
                font-size: 13px;
                letter-spacing: 1.2px;
            }}
            QLabel#infoLabel {{
                color: {CLR_MUTED};
                font-size: 12px;
            }}
            QLabel#warnStatus {{
                color: {CLR_WARN};
                font-size: 14px;
            }}
            QLabel#okStatus {{
                color: {CLR_SUCCESS};
                font-size: 14px;
            }}
            QLineEdit#neonEntry, QLineEdit#valueBox {{
                background: rgba(9, 19, 30, 220);
                border: 1px solid {CLR_BORDER};
                border-radius: 4px;
                padding: 8px 10px;
                color: {CLR_TEXT};
                selection-background-color: {CLR_ACCENT};
                selection-color: {CLR_BG};
            }}
            QPushButton#accentButton, QPushButton#wideAccentButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #129db2, stop:1 {CLR_ACCENT});
                color: #e8fbff;
                border: 1px solid {CLR_ACCENT};
                border-radius: 7px;
                padding: 9px 20px;
                min-height: 42px;
                font-size: 17px;
                font-weight: 700;
                letter-spacing: 2px;
            }}
            QPushButton#wideAccentButton {{
                min-height: 48px;
            }}
            QPushButton#ghostButton, QPushButton#stepButton {{
                background: rgba(10, 17, 29, 180);
                border: 1px solid {CLR_SOFT};
                border-radius: 6px;
                padding: 8px 12px;
                color: {CLR_ACCENT};
            }}
            QPushButton#ghostButton:checked {{
                border-color: {CLR_BORDER};
                color: {CLR_TEXT};
            }}
            QPushButton#smallButton {{
                background: rgba(8, 15, 24, 200);
                border: 1px solid {CLR_SOFT};
                border-radius: 6px;
                padding: 8px 12px;
                color: {CLR_TEXT};
            }}
            QPushButton#smallButton:hover, QPushButton#ghostButton:hover, QPushButton#stepButton:hover {{
                border-color: {CLR_BORDER};
            }}
            QPushButton#toggleButton {{
                text-align: left;
                background: rgba(7, 12, 18, 180);
                border: 1px solid {CLR_SOFT};
                border-radius: 6px;
                padding: 8px 14px;
                color: {CLR_MUTED};
            }}
            QPushButton#toggleButton:checked {{
                background: rgba(0, 229, 255, 40);
                border-color: {CLR_BORDER};
                color: {CLR_TEXT};
            }}
            QFrame#statCard {{
                background: rgba(7, 13, 21, 210);
                border: 1px solid {CLR_SOFT};
                border-radius: 8px;
            }}
            QLabel#statTitle {{
                color: {CLR_MUTED};
                font-size: 13px;
                letter-spacing: 1.4px;
            }}
            QLabel#statValue {{
                font-size: 26px;
                font-weight: 700;
            }}
            QLabel#statFooter {{
                color: {CLR_MUTED};
                font-size: 12px;
            }}
            QLabel#paramTitle {{
                color: {CLR_TEXT};
                font-size: 16px;
            }}
            QLabel#paramDetail {{
                color: {CLR_MUTED};
                font-size: 12px;
            }}
            """
        )

    def _make_field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def _make_small_button(self, text: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("smallButton")
        button.clicked.connect(slot)
        return button

    def _toggle_advanced(self, checked: bool):
        self.advanced_widget.setVisible(checked)
        self.advanced_toggle.setText("▴ ADVANCED" if checked else "▾ ADVANCED")

    def _submit_job(self, fn, on_result=None, on_error=None, on_finished=None):
        job_id = self._next_job_id
        self._next_job_id += 1
        self._job_handlers[job_id] = {
            "result": on_result,
            "error": on_error,
            "finished": on_finished,
        }

        future = self.executor.submit(fn)
        self._job_futures[job_id] = future

        def _done_callback(done_future: Future):
            try:
                result = done_future.result()
            except Exception as exc:  # pragma: no cover - UI worker error path
                self.job_signals.error.emit(job_id, str(exc))
            else:
                self.job_signals.result.emit(job_id, result)
            finally:
                self.job_signals.finished.emit(job_id)

        future.add_done_callback(_done_callback)

    def _handle_job_result(self, job_id: int, payload):
        handler = self._job_handlers.get(job_id, {}).get("result")
        if handler is not None:
            handler(payload)

    def _handle_job_error(self, job_id: int, message: str):
        handler = self._job_handlers.get(job_id, {}).get("error")
        if handler is not None:
            handler(message)

    def _handle_job_finished(self, job_id: int):
        handler = self._job_handlers.get(job_id, {}).get("finished")
        if handler is not None:
            handler()
        self._job_handlers.pop(job_id, None)
        self._job_futures.pop(job_id, None)

    def _set_connected(self, connected: bool):
        self.connected = connected
        host = self.host_edit.text().strip()
        self.setWindowTitle(f"Red Pitaya Pulse Control — {host}" if connected else "Red Pitaya Pulse Control")
        self.status_label.setObjectName("okStatus" if connected else "warnStatus")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _show_error(self, title: str, message: str):
        QMessageBox.critical(self, title, message)

    def _effective_phase_mod_enabled(self) -> bool:
        return self.phase_mod_toggle.isChecked() and self._period_valid and not self._timeout_flag

    def _phase_mod_requested_state(self) -> bool:
        return self.phase_mod_toggle.isChecked()

    def _update_modulation_controls(self):
        effective_phase_mod = self._effective_phase_mod_enabled()
        self.delay_control.setEnabled(not effective_phase_mod)
        self.mod_freq_control.setEnabled(self._phase_mod_requested_state())
        self.mod_amp_control.setEnabled(self._phase_mod_requested_state())

    def _refresh_preview_and_stats(self):
        divider = self.divider_control.value()
        width = self.width_control.value()
        delay = self.delay_control.value()
        mod_freq_hz = clamp_mod_freq_hz(self.mod_freq_control.value())
        mod_amp = clamp_mod_amp(self.mod_amp_control.value())
        effective_phase_mod = self._effective_phase_mod_enabled()
        if self.waveform is not None:
            self.waveform.set_state(divider, width, delay, effective_phase_mod, mod_freq_hz)
        self.stat_duty.set_value(f"{width * 100:.1f} %")
        if effective_phase_mod:
            self.stat_phase.set_value(fmt_freq_hz(mod_freq_hz))
            self.stat_phase.set_footer("phase modulation")
        else:
            self.stat_phase.set_value(f"{delay:.1f} °")
            self.stat_phase.set_footer("input referenced")

        width_cycles = frac_to_cycles(width, self._period_cycles)
        delay_cycles = deg_to_cycles(delay, self._period_cycles)
        phase_word = mod_freq_to_word(mod_freq_hz)
        phase_amp_q15 = mod_amp_to_q15(mod_amp)
        self.width_control.set_detail(f"{width * 100:.1f}%   {fmt_time_s(width_cycles / CLOCK_HZ)}")
        if effective_phase_mod:
            self.delay_control.set_detail("ignored while phase modulation is active")
        else:
            self.delay_control.set_detail(fmt_time_s(delay_cycles / CLOCK_HZ))
        self.mod_freq_control.set_detail(f"DDS word 0x{phase_word:08X}   TTL out {fmt_freq_hz(mod_freq_hz)}")
        self.mod_amp_control.set_detail(f"Q15 {phase_amp_q15}   sweep {mod_amp:.2f}T")
        self._update_modulation_controls()
        self._update_info_text()

    def _update_info_text(self):
        if self._period_cycles <= 1:
            self.info_label.setText("Connect to read input frequency from hardware.")
            return
        divider = max(DIV_MIN, min(DIV_MAX, self.divider_control.value()))
        input_hz = CLOCK_HZ / self._period_cycles
        divided_hz = input_hz / divider
        input_period_s = self._period_cycles / CLOCK_HZ
        mode_text = (
            f"Phase modulation: {fmt_freq_hz(clamp_mod_freq_hz(self.mod_freq_control.value()))}, delay sweep 0..T"
            if self._effective_phase_mod_enabled()
            else "Static delay mode"
        )
        self.info_label.setText(
            f"Input: {fmt_freq_hz(input_hz)}  |  Divider: ÷{divider}  |  Divided: {fmt_freq_hz(divided_hz)}\n"
            f"Width/Delay ref: input period {fmt_time_s(input_period_s)}  ({self._period_cycles} cycles)  |  {mode_text}"
        )

    def _capture_apply_state(self) -> ApplyState:
        divider = max(DIV_MIN, min(DIV_MAX, self.divider_control.value()))
        frac = max(0.0, min(1.0, self.width_control.value()))
        deg = max(0.0, min(180.0, self.delay_control.value()))
        mod_freq_hz = clamp_mod_freq_hz(self.mod_freq_control.value())
        mod_amp = clamp_mod_amp(self.mod_amp_control.value())
        control_word = 0
        if self.enable_toggle.isChecked():
            control_word |= CONTROL_PULSE_ENABLE
        if self._phase_mod_requested_state():
            control_word |= CONTROL_PHASE_MOD_ENABLE
        return ApplyState(
            divider=divider,
            width_cycles=frac_to_cycles(frac, self._period_cycles),
            delay_cycles=deg_to_cycles(deg, self._period_cycles),
            phase_freq_word=mod_freq_to_word(mod_freq_hz),
            phase_amp_q15=mod_amp_to_q15(mod_amp),
            control_word=control_word,
        )

    def _parse_connect_params(self):
        host = self.host_edit.text().strip()
        user = self.user_edit.text().strip()
        port = int(self.port_edit.text().strip())
        base_addr = int(self.base_edit.text().replace("_", ""), 0)
        return host, user, port, base_addr

    def connect_to_board(self):
        try:
            host, user, port, base_addr = self._parse_connect_params()
        except Exception as exc:
            self._show_error("Connection error", str(exc))
            return

        self.connect_btn.setEnabled(False)
        self.status_label.setText("Loading FPGA bitstream…")

        def task():
            remote = RemoteCtl()
            remote.connect(host, user, port)
            remote.run(f"{REMOTE_FPGAUTIL} -b {REMOTE_BITFILE}")
            data = remote.helper(base_addr, "read")
            return remote, data, host, user, port, base_addr

        def on_result(payload):
            remote, data, host, user, port, base_addr = payload
            self.remote = remote
            self.base_addr = base_addr
            self._set_connected(True)
            self.status_label.setText(f"Connected to {user}@{host}:{port}.")
            self._update_readback(data)
            self._start_poll()

        def on_error(message):
            self._stop_poll()
            self._set_connected(False)
            self.status_label.setText("Connection failed.")
            self._show_error("Connection error", message)

        self._submit_job(task, on_result=on_result, on_error=on_error, on_finished=lambda: self.connect_btn.setEnabled(True))

    def _start_poll(self):
        self.poll_timer.start()

    def _stop_poll(self):
        self.poll_timer.stop()

    def _poll_tick(self):
        if not self.connected or self._poll_in_flight:
            return
        self._poll_in_flight = True

        def task():
            return self.remote.helper(self.base_addr, "read")

        def on_result(data):
            self._update_readback(data)

        self._submit_job(task, on_result=on_result, on_finished=lambda: setattr(self, "_poll_in_flight", False))

    def upload_bitfile(self):
        if not self.connected:
            self._show_error("Not connected", "Connect to the Red Pitaya first.")
            return

        local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "red_pitaya_top.bit.bin")
        if not os.path.isfile(local_path):
            self._show_error("File not found", f"Cannot find:\n{local_path}")
            return

        self.status_label.setText("Uploading bitfile…")

        def task():
            self.remote.upload_bitfile(local_path)
            return self.remote.helper(self.base_addr, "read")

        def on_result(data):
            self.status_label.setText("Bitfile uploaded and FPGA reloaded.")
            self._update_readback(data)

        def on_error(message):
            self.status_label.setText("Bitfile upload failed.")
            self._show_error("Upload bitfile failed", message)

        self._submit_job(task, on_result=on_result, on_error=on_error)

    def upload_and_compile(self):
        if not self.connected:
            self._show_error("Not connected", "Connect to the Red Pitaya first.")
            return

        local_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rp_pulse_ctl.c")
        if not os.path.isfile(local_src):
            self._show_error("File not found", f"Cannot find:\n{local_src}")
            return

        self.status_label.setText("Uploading rp_pulse_ctl.c…")

        def task():
            self.remote.upload_and_compile(local_src)
            return None

        def on_error(message):
            self.status_label.setText("Upload/compile failed.")
            self._show_error("Upload/compile failed", message)

        self._submit_job(task, on_result=lambda _none: self.status_label.setText("Upload & compile successful."), on_error=on_error)

    def force_freq_update(self):
        self._force_period_update = True
        self.read_back()

    def read_back(self):
        if not self.connected:
            return
        self.status_label.setText("Reading registers…")

        def task():
            return self.remote.helper(self.base_addr, "read")

        def on_result(data):
            self._update_readback(data)
            self.status_label.setText("Readback updated.")

        def on_error(message):
            self.status_label.setText("Readback failed.")
            self._show_error("Readback failed", message)

        self._submit_job(task, on_result=on_result, on_error=on_error)

    def soft_reset(self):
        if not self.connected:
            return
        self.status_label.setText("Sending soft reset…")

        def task():
            return self.remote.helper(self.base_addr, "soft_reset")

        def on_result(data):
            self._update_readback(data)
            self.status_label.setText("Soft reset pulse sent.")

        def on_error(message):
            self.status_label.setText("Soft reset failed.")
            self._show_error("Soft reset failed", message)

        self._submit_job(task, on_result=on_result, on_error=on_error)

    def on_divider_changed(self, value: int):
        if self.divider_control.entry.hasFocus():
            self.divider_control.entry.setText(str(value))
        self._refresh_preview_and_stats()
        self.maybe_auto_apply()

    def on_width_changed(self, value: float):
        self._refresh_preview_and_stats()
        if not self.width_control.value_box.hasFocus():
            self.maybe_auto_apply()

    def on_delay_changed(self, value: float):
        self._refresh_preview_and_stats()
        if not self.delay_control.value_box.hasFocus():
            self.maybe_auto_apply()

    def on_mod_freq_changed(self, value: float):
        clamped = clamp_mod_freq_hz(value)
        was_clamped = abs(clamped - value) > 1e-9
        self._phase_freq_clamped = was_clamped
        if was_clamped:
            self.mod_freq_control.setValue(clamped)
            return
        self._mod_freq_dirty = True
        self._refresh_preview_and_stats()
        if not self.mod_freq_control.value_box.hasFocus():
            self.maybe_auto_apply()

    def on_mod_amp_changed(self, value: float):
        clamped = clamp_mod_amp(value)
        if abs(clamped - value) > 1e-9:
            self.mod_amp_control.setValue(clamped)
            return
        self._mod_amp_dirty = True
        self._refresh_preview_and_stats()
        if not self.mod_amp_control.value_box.hasFocus():
            self.maybe_auto_apply()

    def on_phase_mod_toggled(self, checked: bool):
        self._refresh_preview_and_stats()
        self.maybe_auto_apply()

    def maybe_auto_apply(self):
        if self.auto_apply_toggle.isChecked():
            self.auto_apply_timer.start()

    def _auto_apply_timeout(self):
        self._queue_apply(source="auto")

    def apply_now(self):
        if not self.connected:
            return
        if self.auto_apply_timer.isActive():
            self.auto_apply_timer.stop()
        self._queue_apply(source="manual")

    def _queue_apply(self, source: str):
        if not self.connected:
            return
        self._pending_apply_state = self._capture_apply_state()
        if not self._apply_in_flight:
            self.status_label.setText("Auto-applying…" if source == "auto" else "Applying…")
            self._start_next_apply()
        else:
            self.status_label.setText("Apply queued…")

    def _start_next_apply(self):
        if self._pending_apply_state is None:
            self._apply_in_flight = False
            return

        state = self._pending_apply_state
        self._pending_apply_state = None
        self._apply_in_flight = True
        self.status_label.setText("Applying…")

        def task():
            data = self.remote.helper(
                self.base_addr,
                "write",
                state.divider,
                state.width_cycles,
                state.delay_cycles,
                state.phase_freq_word,
                state.phase_amp_q15,
                state.control_word,
            )
            return state, data

        def on_result(payload):
            apply_state, data = payload
            self._update_readback(data)
            if int(data.get("phase_freq", -1)) == apply_state.phase_freq_word:
                self._mod_freq_dirty = False
            if int(data.get("phase_amp_q15", -1)) == apply_state.phase_amp_q15:
                self._mod_amp_dirty = False
            mode_text = "phase mod ON" if (apply_state.control_word & CONTROL_PHASE_MOD_ENABLE) else "phase mod OFF"
            self.status_label.setText(
                f"Applied — width {apply_state.width_cycles} cyc, delay {apply_state.delay_cycles} cyc, {mode_text}."
            )

        def on_error(message):
            self.status_label.setText("Apply failed.")
            self._show_error("Apply failed", message)

        def on_finished():
            if self._pending_apply_state is not None:
                self._start_next_apply()
            else:
                self._apply_in_flight = False

        self._submit_job(task, on_result=on_result, on_error=on_error, on_finished=on_finished)

    def _update_readback(self, data):
        control = int(data.get("control", 0))
        divider = int(data.get("divider", 0))
        width = int(data.get("width", 0))
        delay = int(data.get("delay", 0))
        status = int(data.get("status", 0))
        raw_period = int(data.get("period", data.get("raw_period", 0)))
        filt_period = int(data.get("period_avg", data.get("filt_period", 0)))
        phase_freq = int(data.get("phase_freq", 0))
        phase_amp_q15 = int(data.get("phase_amp_q15", 0))

        busy = (status >> 0) & 0x1
        period_valid = (status >> 1) & 0x1
        timeout_flag = (status >> 2) & 0x1
        enable = control & CONTROL_PULSE_ENABLE
        phase_mod_enable = (control & CONTROL_PHASE_MOD_ENABLE) != 0
        self._period_valid = bool(period_valid)
        self._timeout_flag = bool(timeout_flag)

        if period_valid and filt_period > 0:
            change = abs(filt_period - self._period_cycles) / max(1, self._period_cycles)
            if self._force_period_update or self._period_cycles <= 1 or change > 0.05:
                self._force_period_update = False
                self._period_cycles = filt_period
                self._refresh_preview_and_stats()

        raw_freq = CLOCK_HZ / raw_period if raw_period > 0 else 0.0
        filt_freq = CLOCK_HZ / filt_period if filt_period > 0 else 0.0
        divider_hw = max(1, divider)
        out_freq = filt_freq / divider_hw if filt_freq > 0 else 0.0
        current_width = self.width_control.value()
        current_delay = self.delay_control.value()
        mod_freq_hz = phase_freq * CLOCK_HZ / (2**32)
        mod_amp = q15_to_mod_amp(phase_amp_q15)

        self.stat_input.set_value(fmt_freq_hz(filt_freq) if filt_period > 0 else "—")
        self.stat_output.set_value(fmt_freq_hz(out_freq) if filt_period > 0 else "—")
        self.stat_duty.set_value(f"{current_width * 100:.1f} %")
        self.stat_input.set_footer("from hardware")
        self.stat_output.set_footer("divided output")
        self.stat_duty.set_footer("input referenced")
        if phase_mod_enable and period_valid and not timeout_flag:
            self.stat_phase.set_value(fmt_freq_hz(mod_freq_hz))
            self.stat_phase.set_footer("phase modulation")
        else:
            self.stat_phase.set_value(f"{current_delay:.1f} °")
            self.stat_phase.set_footer("input referenced")

        blocked_phase_mod = self.phase_mod_toggle.isChecked() and (not period_valid or timeout_flag)

        self.enable_toggle.blockSignals(True)
        self.enable_toggle.setChecked(bool(enable))
        self.enable_toggle.blockSignals(False)

        should_sync_mod_freq = (phase_mod_enable or phase_freq != 0) and not self._mod_freq_dirty
        if should_sync_mod_freq and abs(self.mod_freq_control.value() - clamp_mod_freq_hz(mod_freq_hz)) > 0.5:
            self.mod_freq_control.blockSignals(True)
            self.mod_freq_control.setValue(clamp_mod_freq_hz(mod_freq_hz))
            self.mod_freq_control.blockSignals(False)

        should_sync_mod_amp = (phase_mod_enable or phase_amp_q15 != 0) and not self._mod_amp_dirty
        if should_sync_mod_amp and abs(self.mod_amp_control.value() - clamp_mod_amp(mod_amp)) > 0.01:
            self.mod_amp_control.blockSignals(True)
            self.mod_amp_control.setValue(clamp_mod_amp(mod_amp))
            self.mod_amp_control.blockSignals(False)

        self._refresh_preview_and_stats()

        warnings: list[str] = []
        if blocked_phase_mod:
            warnings.append("Phase modulation requested, but blocked by trigger status. Static delay is active.")
        if not period_valid:
            warnings.append("No valid trigger period.")
        if timeout_flag:
            warnings.append("Trigger timeout detected on STATUS.bit2.")
        if self._phase_freq_clamped:
            warnings.append("Modulation frequency was clamped to 5 kHz.")
        self.freq_warning_label.setText("  ".join(f"\u26a0  {text}" for text in warnings))

    def closeEvent(self, event):
        self._stop_poll()
        self.executor.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Red Pitaya Pulse Control")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
