#!/usr/bin/env python3
"""
redpitaya_pulse_gui_qt.py — PySide6 desktop GUI for the Red Pitaya pulse generator.

Free-running mode: the FPGA measures the input frequency once at startup, then
generates pulses at f_input + delta_f. The frequency offset is given as a signed
period offset in clock cycles (period_offset register at 0x1C).

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
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
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
        QTextCursor,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QInputDialog,
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
LOGBOOK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rp_logbook.log")

WIDTH_MIN = 1
PERIOD_OFFSET_MIN = -10_000_000
PERIOD_OFFSET_MAX =  10_000_000

CONTROL_PULSE_ENABLE = 0x1
CONTROL_SOFT_RESET   = 0x2

# 60% dominant — dark navy backgrounds
CLR_BG = "#060c17"
CLR_BG_2 = "#09131f"
CLR_SURFACE = "#0c1826"
CLR_PANEL = "#081220"

# 30% secondary — structural elements, borders, text hierarchy
CLR_BORDER = "#1b4a62"
CLR_BORDER_MAGENTA = "#3d1535"
CLR_BORDER_DIM = "#0c4a5e"
CLR_SOFT = "#1e3a52"
CLR_GRID = "#0e2030"
CLR_MUTED = "#6a9ab0"
CLR_TEXT = "#8ab8d0"

# 10% accent
CLR_ACCENT = "#0ecce0"
CLR_SUCCESS = "#0dbb90"
CLR_WARN = "#dd3355"

CLR_ENTRY_BG = "#060f1a"
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


def offset_to_delta_f(offset_cycles: int, period_avg: int) -> float:
    """Approximate frequency shift for a given period offset in clock ticks.

    delta_f ≈ -offset * f_input^2 / CLOCK_HZ = -offset * CLOCK_HZ / period^2
    """
    if period_avg <= 0:
        return 0.0
    return -offset_cycles * CLOCK_HZ / (period_avg ** 2)


@dataclass
class ApplyState:
    width_cycles: int
    period_offset: int
    control_word: int


_SSH_KEY_CANDIDATES = [
    os.path.expanduser("~/.ssh/id_ed25519"),
    os.path.expanduser("~/.ssh/id_rsa"),
    os.path.expanduser("~/.ssh/id_ecdsa"),
    os.path.expanduser("~/.ssh/id_dsa"),
]


class SshKeyHelper:
    """One-time SSH key setup: generate a key pair and install it on the board."""

    @staticmethod
    def default_key_path() -> str | None:
        for path in _SSH_KEY_CANDIDATES:
            if os.path.isfile(path):
                return path
        return None

    @staticmethod
    def key_installed_on_board(host: str, user: str, port: int, key_path: str) -> bool:
        cmd = [
            "ssh", "-p", str(port), "-i", key_path,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8",
            "-o", "PubkeyAuthentication=yes",
            "-o", "PasswordAuthentication=no",
            f"{user}@{host}", "echo ok",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        return result.returncode == 0

    @staticmethod
    def generate_key(key_path: str = _SSH_KEY_CANDIDATES[0]) -> None:
        if os.path.isfile(key_path):
            return
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        result = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-q"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ssh-keygen failed: {result.stderr.strip()}")

    @staticmethod
    def _copy_key_sshpass(host: str, user: str, port: int, key_path: str, password: str) -> None:
        if not shutil.which("sshpass"):
            raise RuntimeError("sshpass_unavailable")
        pub_path = key_path + ".pub"
        cmd = [
            "sshpass", "-e", "ssh-copy-id",
            "-i", pub_path, "-p", str(port),
            "-o", "StrictHostKeyChecking=accept-new",
            f"{user}@{host}",
        ]
        env = os.environ.copy()
        env["SSHPASS"] = password
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "ssh-copy-id failed.")

    @staticmethod
    def _copy_key_askpass(host: str, user: str, port: int, key_path: str, password: str) -> None:
        pub_path = key_path + ".pub"
        with open(pub_path) as fh:
            pubkey = fh.read().strip()
        script = "#!/usr/bin/env python3\nimport sys\n" + f"sys.stdout.write({repr(password)})\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, prefix="rp_askpass_") as tf:
            askpass_path = tf.name
            tf.write(script)
        os.chmod(askpass_path, 0o700)
        remote_cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"echo {shlex.quote(pubkey)} >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys"
        )
        env = os.environ.copy()
        env["SSH_ASKPASS"] = askpass_path
        env["SSH_ASKPASS_REQUIRE"] = "force"
        if "DISPLAY" not in env:
            env["DISPLAY"] = ":0"
        cmd = [
            "ssh", "-p", str(port),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "PubkeyAuthentication=no",
            "-o", "PasswordAuthentication=yes",
            "-o", "ConnectTimeout=15",
            f"{user}@{host}", remote_cmd,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        finally:
            os.unlink(askpass_path)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Key installation failed.")

    @classmethod
    def install_key(cls, host: str, user: str, port: int, password: str,
                    key_path: str = _SSH_KEY_CANDIDATES[0]) -> None:
        cls.generate_key(key_path)
        try:
            cls._copy_key_sshpass(host, user, port, key_path, password)
        except RuntimeError as exc:
            if "sshpass_unavailable" in str(exc):
                cls._copy_key_askpass(host, user, port, key_path, password)
            else:
                raise


class RemoteCtl:
    def __init__(self):
        self.host = ""
        self.user = ""
        self.port = 22

    _CONTROL_PATH = "/".join([
        tempfile.gettempdir().replace("\\", "/"), "rp_ssh_%h_%p_%r.sock"
    ])
    _USE_CONTROL_MASTER = sys.platform != "win32"

    def connect(self, host: str, user: str, port: int):
        if not shutil.which("ssh"):
            raise RuntimeError("OpenSSH client not found on this PC.")
        self.host = host
        self.user = user
        self.port = port

    def _ssh_base_args(self) -> list[str]:
        args = [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8",
        ]
        if self._USE_CONTROL_MASTER:
            args += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._CONTROL_PATH}",
                "-o", "ControlPersist=120",
            ]
        key_path = SshKeyHelper.default_key_path()
        if key_path:
            args += ["-i", key_path]
        return args

    @staticmethod
    def _is_auth_error(message: str) -> bool:
        lowered = message.lower()
        return any(phrase in lowered for phrase in (
            "permission denied", "publickey", "authentication failed",
            "no supported authentication methods",
        ))

    def run(self, cmd: str):
        ssh_cmd = (
            ["ssh", "-p", str(self.port)]
            + self._ssh_base_args()
            + [f"{self.user}@{self.host}", cmd]
        )
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=45)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "SSH command failed.")
        return proc.stdout.strip()

    def helper(self, base_addr: int, command: str, *args):
        remote_cmd = " ".join(
            [shlex.quote(REMOTE_BIN), shlex.quote(hex(base_addr)), shlex.quote(command)]
            + [shlex.quote(str(a)) for a in args]
        )
        return json.loads(self.run(remote_cmd))

    def upload_bitfile(self, local_path: str):
        if not shutil.which("scp"):
            raise RuntimeError("scp not found on this PC.")
        scp_cmd = (
            ["scp", "-P", str(self.port)]
            + self._ssh_base_args()
            + [local_path, f"{self.user}@{self.host}:{REMOTE_BITFILE}"]
        )
        proc = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"scp failed: {proc.stderr.strip() or proc.stdout.strip()}")
        self.run(f"{REMOTE_FPGAUTIL} -b {REMOTE_BITFILE}")

    def upload_and_compile(self, local_src: str, remote_src: str = "/root/rp_pulse_ctl.c"):
        if not shutil.which("scp"):
            raise RuntimeError("scp not found on this PC.")
        scp_cmd = (
            ["scp", "-P", str(self.port)]
            + self._ssh_base_args()
            + [local_src, f"{self.user}@{self.host}:{remote_src}"]
        )
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

        painter.setOpacity(0.15)
        pen = QPen(QColor(CLR_GRID), 1)
        painter.setPen(pen)
        for y in range(16, rect.height(), 28):
            painter.drawLine(0, y, rect.width(), y)

        painter.setOpacity(0.08)
        painter.setPen(QPen(QColor(CLR_BORDER), 1))
        for i in range(8):
            y = 40 + i * 112
            painter.drawLine(30, y, rect.width() - 40, y + (i % 2) * 6)

        painter.setPen(QPen(QColor("#2a4a60"), 2))
        painter.setOpacity(0.20)
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
        painter.setPen(QPen(QColor(CLR_BORDER), 1.5))
        painter.drawPath(path)

        painter.setOpacity(0.55)
        painter.setPen(QPen(QColor("#2a6880"), 1.2))
        painter.drawLine(rect.right() - 100, rect.top() + 10, rect.right() - 20, rect.top() + 10)
        painter.drawLine(rect.left() + 20, rect.bottom() - 12, rect.left() + 100, rect.bottom() - 12)

        painter.setOpacity(0.30)
        painter.setPen(QPen(QColor(CLR_BORDER), 5))
        painter.drawLine(rect.left() + 8, rect.top() + 18, rect.left() + 50, rect.top() + 18)
        painter.drawLine(rect.right() - 38, rect.bottom() - 16, rect.right() - 18, rect.bottom() - 16)

        painter.setOpacity(1.0)


class StatCard(QFrame):
    def __init__(self, title: str, accent: str, parent=None):
        super().__init__(parent)
        self.accent = accent
        self.setObjectName("statCard")
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(5)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("statTitle")
        self.title_label.setWordWrap(False)
        layout.addWidget(self.title_label)

        self.value_label = QLabel("—")
        self.value_label.setObjectName("statValue")
        self.value_label.setStyleSheet(f"color: {accent};")
        self.value_label.setWordWrap(False)
        layout.addWidget(self.value_label)

        self.footer_label = QLabel("")
        self.footer_label.setObjectName("statFooter")
        self.footer_label.setWordWrap(False)
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


class ParameterSlider(QWidget):
    valueChanged = Signal(float)
    valueCommitted = Signal(float)

    def __init__(self, title: str, minimum: float, maximum: float, step: float,
                 decimals: int, suffix_label: str = "", display_factor: float = 1.0,
                 display_suffix: str = "", parent=None):
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
        self.width_frac = 0.1
        self.offset_cycles = 0
        self.period_avg = 500

    def set_state(self, width_frac: float, offset_cycles: int, period_avg: int):
        self.width_frac = max(0.001, min(0.999, width_frac))
        self.offset_cycles = offset_cycles
        self.period_avg = max(1, period_avg)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(12, 8, -12, -8)
        h = rect.height()

        panel_grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        panel_grad.setColorAt(0.0, QColor(8, 14, 24, 215))
        panel_grad.setColorAt(1.0, QColor(6, 10, 18, 230))
        painter.fillRect(rect, panel_grad)

        painter.setPen(QPen(QColor(CLR_GRID), 1))
        for i in range(10):
            x = rect.left() + int(i * rect.width() / 10)
            painter.drawLine(x, rect.top() + 6, x, rect.bottom() - 20)
        for i in range(4):
            y = rect.top() + 6 + int(i * (h - 30) / 3)
            painter.drawLine(rect.left() + 14, y, rect.right() - 8, y)

        label_col_w = 82
        left = rect.left() + label_col_w + 8
        right = rect.right() - 10
        track_w = max(40, right - left)

        y_in_hi  = rect.top() + int(h * 0.08)
        y_in_lo  = rect.top() + int(h * 0.36)
        y_out_hi = rect.top() + int(h * 0.52)
        y_out_lo = rect.top() + int(h * 0.80)
        caption_y = rect.top() + int(h * 0.90)

        in_center  = (y_in_hi + y_in_lo) // 2
        out_center = (y_out_hi + y_out_lo) // 2

        sig_font = QFont(MONO_FONT_FAMILY, 9)
        sig_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        painter.setFont(sig_font)

        painter.setPen(QPen(QColor(CLR_MUTED), 1))
        painter.drawText(QRectF(rect.left() + 2, in_center - 10, label_col_w - 6, 20),
                         Qt.AlignRight | Qt.AlignVCenter, "INPUT")
        painter.setPen(QPen(QColor(CLR_ACCENT), 1))
        painter.drawText(QRectF(rect.left() + 2, out_center - 10, label_col_w - 6, 20),
                         Qt.AlignRight | Qt.AlignVCenter, "OUTPUT")

        for y in (y_in_hi, y_in_lo, y_out_hi, y_out_lo):
            painter.setPen(QPen(QColor(CLR_GRID), 1, Qt.DashLine))
            painter.drawLine(left, y, right, y)

        n_in = 32
        in_pw = track_w / n_in

        painter.setPen(QPen(QColor(CLR_MUTED), 1.4))
        x = float(left)
        for _ in range(n_in):
            mid_x = x + in_pw / 2
            points = [
                QPointF(x, y_in_lo), QPointF(x, y_in_hi),
                QPointF(mid_x, y_in_hi), QPointF(mid_x, y_in_lo),
                QPointF(x + in_pw, y_in_lo),
            ]
            for p1, p2 in zip(points, points[1:]):
                painter.drawLine(p1, p2)
            x += in_pw

        # Output period is slightly different from input period
        out_pw_raw = in_pw * (1.0 + self.offset_cycles / max(1, self.period_avg))
        out_pw = max(4.0, out_pw_raw)
        n_out = max(1, int(track_w / out_pw))
        x = float(left)
        for _ in range(n_out):
            h_px = out_pw * self.width_frac
            points = [
                QPointF(x, y_out_lo),
                QPointF(x, y_out_hi),
                QPointF(x + h_px, y_out_hi),
                QPointF(x + h_px, y_out_lo),
                QPointF(x + out_pw, y_out_lo),
            ]
            painter.setPen(QPen(QColor(CLR_BORDER_DIM), 5))
            for p1, p2 in zip(points, points[1:]):
                painter.drawLine(p1, p2)
            painter.setPen(QPen(QColor(CLR_ACCENT), 2))
            for p1, p2 in zip(points, points[1:]):
                painter.drawLine(p1, p2)
            x += out_pw

        painter.setPen(QPen(QColor(CLR_MUTED), 1))
        caption_font = QFont(MONO_FONT_FAMILY, 9)
        caption_font.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        painter.setFont(caption_font)
        delta_f = offset_to_delta_f(self.offset_cycles, self.period_avg)
        sign = "+" if delta_f >= 0 else ""
        painter.drawText(
            QRectF(left, caption_y - 14, track_w, 16),
            Qt.AlignCenter,
            f"duty {self.width_frac * 100:.1f}%  |  offset {self.offset_cycles:+d} cyc  |  Δf ≈ {sign}{fmt_freq_hz(delta_f)}",
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Red Pitaya Pulse Control")
        self.resize(1100, 700)
        self.setMinimumSize(860, 560)

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
        self._period_valid = False
        self._timeout_flag = False
        self._apply_in_flight = False
        self._pending_apply_state: ApplyState | None = None
        self._poll_in_flight = False
        self.waveform: WaveformPreview | None = None
        self.logbook_view: QTextEdit | None = None
        self._logbook_entries: list[str] = []
        self._logbook_path = LOGBOOK_FILE

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
        self._load_existing_logbook()
        self._record_logbook("INFO", "Application started.")

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
        self.wave_panel = self._build_waveform_panel()
        controls_col.addWidget(self.pulse_controls_panel, 1)
        mid_row.addLayout(controls_col, 11)
        mid_row.addWidget(self.wave_panel, 10)

        self.width_control.setValue(0.1)
        self.offset_entry.setText("0")

        self.logbook_panel = self._build_logbook_panel()
        root.addWidget(self.logbook_panel)
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
        self.connect_btn.clicked.connect(self.connect_to_board)
        row.addWidget(self.connect_btn, 1, 0, 1, 4)

        self.ssh_setup_btn = QPushButton("SETUP SSH KEY")
        self.ssh_setup_btn.setObjectName("ghostButton")
        self.ssh_setup_btn.setToolTip(
            "One-time setup: generates an SSH key pair and installs it on the board."
        )
        self.ssh_setup_btn.clicked.connect(self._on_setup_ssh_key)
        row.addWidget(self.ssh_setup_btn, 2, 0, 1, 4)

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

        adv.addWidget(self.readback_btn, 1, 0, 1, 1)
        adv.addWidget(self.soft_reset_btn, 1, 1, 1, 1)
        adv.addWidget(self.upload_compile_btn, 1, 2, 1, 2)
        adv.addWidget(self.upload_bitfile_btn, 1, 4, 1, 1)

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

        self.stat_input  = StatCard("INPUT FREQ",    CLR_TEXT)
        self.stat_output = StatCard("OUTPUT FREQ",   CLR_ACCENT)
        self.stat_duty   = StatCard("DUTY CYCLE",    CLR_TEXT)
        self.stat_status = StatCard("FREERUN STATUS", CLR_MUTED)

        for col, widget in enumerate([self.stat_input, self.stat_output, self.stat_duty, self.stat_status]):
            grid.addWidget(widget, 0, col)
            grid.setColumnStretch(col, 1)

        return panel

    def _build_pulse_controls_panel(self) -> CyberPanel:
        panel = CyberPanel("PULSE CONTROLS")
        layout = panel.content_layout

        self.width_control = ParameterSlider(
            "Width (duty cycle)",
            0.0, 1.0, 0.05, 1,
            display_factor=100.0, display_suffix="%",
        )
        layout.addWidget(self.width_control)

        # Period offset — signed integer entry
        offset_row = QWidget()
        offset_layout = QGridLayout(offset_row)
        offset_layout.setContentsMargins(0, 0, 0, 0)
        offset_layout.setHorizontalSpacing(12)
        offset_layout.setVerticalSpacing(4)

        offset_title = QLabel("Period offset (cycles)")
        offset_title.setObjectName("paramTitle")
        offset_layout.addWidget(offset_title, 0, 0)

        self.offset_entry = QLineEdit("0")
        self.offset_entry.setAlignment(Qt.AlignCenter)
        self.offset_entry.setObjectName("valueBox")
        self.offset_entry.setFixedWidth(120)
        self.offset_entry.setValidator(
            QIntValidator(PERIOD_OFFSET_MIN, PERIOD_OFFSET_MAX, self)
        )
        offset_layout.addWidget(self.offset_entry, 0, 1)

        self.offset_detail = QLabel("")
        self.offset_detail.setObjectName("paramDetail")
        offset_layout.addWidget(self.offset_detail, 1, 0, 1, 2)

        layout.addWidget(offset_row)

        toggles = QHBoxLayout()
        toggles.setSpacing(12)
        self.enable_toggle = ToggleButton("Enable output")
        self.enable_toggle.setChecked(True)
        toggles.addWidget(self.enable_toggle)
        self.auto_apply_toggle = ToggleButton("Auto apply")
        toggles.addWidget(self.auto_apply_toggle)
        toggles.addStretch(1)
        layout.addLayout(toggles)

        self.apply_btn = QPushButton("APPLY NOW")
        self.apply_btn.setObjectName("accentButton")
        self.apply_btn.clicked.connect(self.apply_now)
        layout.addWidget(self.apply_btn)

        layout.addStretch(1)

        self.width_control.valueChanged.connect(self.on_width_changed)
        self.width_control.valueCommitted.connect(lambda _v: self.maybe_auto_apply())
        self.offset_entry.editingFinished.connect(self.on_offset_changed)
        self.enable_toggle.toggled.connect(lambda _checked: self.maybe_auto_apply())

        return panel

    def _build_waveform_panel(self) -> CyberPanel:
        panel = CyberPanel("WAVEFORM PREVIEW")
        self.waveform = WaveformPreview()
        panel.content_layout.addWidget(self.waveform, 0, Qt.AlignTop)
        panel.content_layout.addStretch(1)
        return panel

    def _build_logbook_panel(self) -> CyberPanel:
        panel = CyberPanel("LOGBOOK")
        self.logbook_view = QTextEdit()
        self.logbook_view.setReadOnly(True)
        self.logbook_view.setMinimumHeight(150)
        self.logbook_view.setObjectName("logbookView")
        panel.content_layout.addWidget(self.logbook_view)

        hint = QLabel(f"Persistent log: {self._logbook_path}")
        hint.setObjectName("infoLabel")
        hint.setWordWrap(True)
        panel.content_layout.addWidget(hint)
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
            QScrollArea, QScrollArea > QWidget > QWidget {{
                background: transparent;
                border: none;
            }}
            QLabel#panelTitle {{
                color: #4a9ab5;
                font-size: 18px;
                font-weight: 700;
                letter-spacing: 3px;
            }}
            QLabel#fieldLabel {{
                color: {CLR_MUTED};
                font-size: 12px;
                letter-spacing: 1px;
            }}
            QLabel#infoLabel {{
                color: {CLR_MUTED};
                font-size: 11px;
            }}
            QLabel#warnStatus {{
                color: {CLR_WARN};
                font-size: 13px;
            }}
            QLabel#okStatus {{
                color: {CLR_SUCCESS};
                font-size: 13px;
            }}
            QLineEdit#neonEntry, QLineEdit#valueBox {{
                background: {CLR_ENTRY_BG};
                border: 1px solid {CLR_SOFT};
                border-radius: 4px;
                padding: 7px 10px;
                color: {CLR_TEXT};
                selection-background-color: {CLR_ACCENT};
                selection-color: {CLR_BG};
            }}
            QLineEdit#neonEntry:focus, QLineEdit#valueBox:focus {{
                border-color: {CLR_BORDER};
            }}
            QPushButton#accentButton, QPushButton#wideAccentButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0a8da0, stop:1 {CLR_ACCENT});
                color: #d8f8ff;
                border: 1px solid {CLR_ACCENT};
                border-radius: 6px;
                padding: 8px 18px;
                min-height: 38px;
                font-size: 15px;
                font-weight: 700;
                letter-spacing: 2px;
            }}
            QPushButton#accentButton:hover, QPushButton#wideAccentButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0ca8c0, stop:1 #30e8f8);
            }}
            QPushButton#ghostButton, QPushButton#stepButton {{
                background: {CLR_PANEL};
                border: 1px solid {CLR_SOFT};
                border-radius: 5px;
                padding: 6px 10px;
                color: {CLR_TEXT};
            }}
            QPushButton#ghostButton:checked {{
                border-color: {CLR_BORDER};
                color: {CLR_ACCENT};
            }}
            QPushButton#smallButton {{
                background: {CLR_PANEL};
                border: 1px solid {CLR_SOFT};
                border-radius: 5px;
                padding: 6px 10px;
                color: {CLR_MUTED};
            }}
            QPushButton#smallButton:hover, QPushButton#ghostButton:hover, QPushButton#stepButton:hover {{
                border-color: {CLR_BORDER};
                color: {CLR_TEXT};
            }}
            QPushButton#toggleButton {{
                text-align: left;
                background: {CLR_PANEL};
                border: 1px solid {CLR_SOFT};
                border-radius: 5px;
                padding: 7px 12px;
                color: {CLR_MUTED};
            }}
            QPushButton#toggleButton:checked {{
                background: rgba(14, 204, 224, 20);
                border-color: {CLR_BORDER};
                color: {CLR_TEXT};
            }}
            QFrame#statCard {{
                background: rgba(8, 16, 28, 200);
                border: 1px solid {CLR_SOFT};
                border-radius: 6px;
            }}
            QLabel#statTitle {{
                color: {CLR_MUTED};
                font-size: 11px;
                letter-spacing: 0.5px;
            }}
            QLabel#statValue {{
                font-size: 22px;
                font-weight: 700;
            }}
            QLabel#statFooter {{
                color: {CLR_MUTED};
                font-size: 11px;
            }}
            QLabel#paramTitle {{
                color: {CLR_TEXT};
                font-size: 13px;
            }}
            QLabel#paramDetail {{
                color: {CLR_MUTED};
                font-size: 11px;
            }}
            QTextEdit#logbookView {{
                background: {CLR_ENTRY_BG};
                border: 1px solid {CLR_SOFT};
                border-radius: 6px;
                color: {CLR_TEXT};
                padding: 8px;
                font-size: 11px;
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

    def _record_logbook(self, level: str, message: str, details: str | None = None):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} [{level}] {message}"
        if details:
            detail_text = " ".join(str(details).splitlines()).strip()
            if detail_text:
                line = f"{line} | {detail_text}"
        self._logbook_entries.append(line)
        self._logbook_entries = self._logbook_entries[-500:]
        try:
            with open(self._logbook_path, "a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")
        except OSError:
            pass
        if self.logbook_view is not None:
            self.logbook_view.setPlainText("\n".join(self._logbook_entries[-200:]))
            self.logbook_view.moveCursor(QTextCursor.End)

    def _load_existing_logbook(self):
        try:
            with open(self._logbook_path, "r", encoding="utf-8") as log_file:
                self._logbook_entries = log_file.read().splitlines()[-200:]
        except OSError:
            self._logbook_entries = []
        if self.logbook_view is not None and self._logbook_entries:
            self.logbook_view.setPlainText("\n".join(self._logbook_entries))
            self.logbook_view.moveCursor(QTextCursor.End)

    def _submit_job(self, fn, on_result=None, on_error=None, on_finished=None,
                    operation: str = "Background operation", log_success: bool = True):
        job_id = self._next_job_id
        self._next_job_id += 1
        self._job_handlers[job_id] = {
            "result": on_result, "error": on_error, "finished": on_finished,
            "operation": operation, "log_success": log_success,
        }
        if log_success:
            self._record_logbook("INFO", f"{operation} started.")

        future = self.executor.submit(fn)
        self._job_futures[job_id] = future

        def _done_callback(done_future: Future):
            try:
                result = done_future.result()
            except Exception as exc:
                self.job_signals.error.emit(job_id, str(exc))
            else:
                self.job_signals.result.emit(job_id, result)
            finally:
                self.job_signals.finished.emit(job_id)

        future.add_done_callback(_done_callback)

    def _handle_job_result(self, job_id: int, payload):
        job = self._job_handlers.get(job_id, {})
        if job.get("log_success", True):
            self._record_logbook("INFO", f"{job.get('operation', 'Background operation')} completed.")
        handler = job.get("result")
        if handler is not None:
            handler(payload)

    def _handle_job_error(self, job_id: int, message: str):
        job = self._job_handlers.get(job_id, {})
        self._record_logbook("ERROR", f"{job.get('operation', 'Background operation')} failed.", message)
        handler = job.get("error")
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

    def _show_error(self, title: str, message: str, *, log: bool = True):
        if log:
            self._record_logbook("ERROR", title, message)
        QMessageBox.critical(self, title, message)

    def _get_offset_cycles(self) -> int:
        try:
            return max(PERIOD_OFFSET_MIN, min(PERIOD_OFFSET_MAX, int(self.offset_entry.text())))
        except ValueError:
            return 0

    def _refresh_preview_and_stats(self):
        width = self.width_control.value()
        offset = self._get_offset_cycles()

        if self.waveform is not None:
            self.waveform.set_state(width, offset, self._period_cycles)

        self.stat_duty.set_value(f"{width * 100:.1f} %")
        self.stat_duty.set_footer("of output period")

        width_cycles = frac_to_cycles(width, self._period_cycles)
        self.width_control.set_detail(
            f"{width * 100:.1f}%   {fmt_time_s(width_cycles / CLOCK_HZ)}"
        )

        delta_f = offset_to_delta_f(offset, self._period_cycles)
        sign = "+" if delta_f >= 0 else ""
        self.offset_detail.setText(
            f"Δf ≈ {sign}{fmt_freq_hz(delta_f)}  "
            f"(output_period ≈ {self._period_cycles + offset} cycles)"
        )
        self._update_info_text()

    def _update_info_text(self):
        if self._period_cycles <= 1:
            self.info_label.setText("Connect to read input frequency from hardware.")
            return
        input_hz = CLOCK_HZ / self._period_cycles
        offset = self._get_offset_cycles()
        out_period = max(200, self._period_cycles + offset)
        out_hz = CLOCK_HZ / out_period
        self.info_label.setText(
            f"Input: {fmt_freq_hz(input_hz)}  ({self._period_cycles} cycles)  |  "
            f"Output: {fmt_freq_hz(out_hz)}  (period {out_period} cycles)"
        )

    def _capture_apply_state(self) -> ApplyState:
        frac = max(0.0, min(1.0, self.width_control.value()))
        width_cycles = frac_to_cycles(frac, self._period_cycles)
        offset = self._get_offset_cycles()
        control_word = CONTROL_PULSE_ENABLE if self.enable_toggle.isChecked() else 0
        return ApplyState(width_cycles=width_cycles, period_offset=offset, control_word=control_word)

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
            if RemoteCtl._is_auth_error(message):
                reply = QMessageBox.question(
                    self, "Authentication Failed",
                    "SSH authentication failed.\n\nRun SSH Key Setup now?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
                )
                if reply == QMessageBox.Yes:
                    self._on_setup_ssh_key()
            else:
                self._show_error("Connection error", message, log=False)

        self._submit_job(
            task, on_result=on_result, on_error=on_error,
            on_finished=lambda: self.connect_btn.setEnabled(True),
            operation=f"Connect to {user}@{host}:{port}",
        )

    def _on_setup_ssh_key(self):
        try:
            host, user, port, _base = self._parse_connect_params()
        except Exception as exc:
            self._show_error("Setup SSH key", f"Fix connection fields first:\n{exc}")
            return

        key_path = SshKeyHelper.default_key_path() or _SSH_KEY_CANDIDATES[0]
        self.ssh_setup_btn.setEnabled(False)
        self.status_label.setText("Checking SSH key…")

        def probe_task():
            return SshKeyHelper.key_installed_on_board(host, user, port, key_path)

        def probe_done(already_works: bool):
            if already_works:
                self.ssh_setup_btn.setEnabled(True)
                self.status_label.setText("SSH key already installed.")
                QMessageBox.information(self, "SSH Key",
                    f"Key-based auth to {user}@{host} is already working.")
                return
            _ask_password()

        def probe_error(_message: str):
            _ask_password()

        def _ask_password():
            password, ok = QInputDialog.getText(
                self, "SSH Key Setup",
                f"Enter the SSH password for {user}@{host}:",
                QLineEdit.Password,
            )
            if not ok or not password:
                self.ssh_setup_btn.setEnabled(True)
                self.status_label.setText("SSH key setup cancelled.")
                return
            _run_install(password)

        def _run_install(password: str):
            self.status_label.setText("Installing SSH key on board…")

            def install_task():
                SshKeyHelper.install_key(host, user, port, password, key_path)

            def install_done(_result):
                self.status_label.setText("SSH key installed — click CONNECT.")
                QMessageBox.information(self, "SSH Key Setup Complete",
                    f"Your public key has been installed on {user}@{host}.")

            def install_error(message: str):
                self.status_label.setText("SSH key installation failed.")
                self._show_error("SSH Key Setup Failed", message, log=False)

            self._submit_job(
                install_task, on_result=install_done, on_error=install_error,
                on_finished=lambda: self.ssh_setup_btn.setEnabled(True),
                operation=f"Install SSH key on {user}@{host}:{port}",
            )

        self._submit_job(
            probe_task, on_result=probe_done, on_error=probe_error,
            operation=f"Check SSH key for {user}@{host}:{port}",
        )

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

        self._submit_job(
            task, on_result=on_result,
            on_finished=lambda: setattr(self, "_poll_in_flight", False),
            operation="Poll register readback", log_success=False,
        )

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
            self._show_error("Upload bitfile failed", message, log=False)

        self._submit_job(task, on_result=on_result, on_error=on_error,
                         operation=f"Upload FPGA bitfile {os.path.basename(local_path)}")

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
            self._show_error("Upload/compile failed", message, log=False)

        self._submit_job(
            task,
            on_result=lambda _none: self.status_label.setText("Upload & compile successful."),
            on_error=on_error,
            operation=f"Upload and compile {os.path.basename(local_src)}",
        )

    def read_back(self):
        if not self.connected:
            self._record_logbook("WARN", "Readback skipped because the board is not connected.")
            return
        self.status_label.setText("Reading registers…")

        def task():
            return self.remote.helper(self.base_addr, "read")

        def on_result(data):
            self._update_readback(data)
            self.status_label.setText("Readback updated.")

        def on_error(message):
            self.status_label.setText("Readback failed.")
            self._show_error("Readback failed", message, log=False)

        self._submit_job(task, on_result=on_result, on_error=on_error,
                         operation="Read hardware registers")

    def soft_reset(self):
        if not self.connected:
            self._record_logbook("WARN", "Soft reset skipped because the board is not connected.")
            return
        self.status_label.setText("Sending soft reset…")

        def task():
            return self.remote.helper(self.base_addr, "soft_reset")

        def on_result(data):
            self._update_readback(data)
            self.status_label.setText("Soft reset pulse sent.")

        def on_error(message):
            self.status_label.setText("Soft reset failed.")
            self._show_error("Soft reset failed", message, log=False)

        self._submit_job(task, on_result=on_result, on_error=on_error,
                         operation="Send soft reset")

    def on_width_changed(self, _value: float):
        self._refresh_preview_and_stats()
        if not self.width_control.value_box.hasFocus():
            self.maybe_auto_apply()

    def on_offset_changed(self):
        self._refresh_preview_and_stats()
        self.maybe_auto_apply()

    def maybe_auto_apply(self):
        if self.auto_apply_toggle.isChecked():
            self.auto_apply_timer.start()

    def _auto_apply_timeout(self):
        self._queue_apply(source="auto")

    def apply_now(self):
        if not self.connected:
            self._record_logbook("WARN", "Apply skipped because the board is not connected.")
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
                self.base_addr, "write",
                state.width_cycles,
                state.period_offset,
                state.control_word,
            )
            return state, data

        def on_result(payload):
            apply_state, data = payload
            self._update_readback(data)
            self.status_label.setText(
                f"Applied — width {apply_state.width_cycles} cyc, "
                f"offset {apply_state.period_offset:+d} cyc."
            )

        def on_error(message):
            self.status_label.setText("Apply failed.")
            self._show_error("Apply failed", message, log=False)

        def on_finished():
            if self._pending_apply_state is not None:
                self._start_next_apply()
            else:
                self._apply_in_flight = False

        self._submit_job(
            task, on_result=on_result, on_error=on_error, on_finished=on_finished,
            operation=(
                f"Apply pulse settings "
                f"(width={state.width_cycles}, offset={state.period_offset:+d}, "
                f"control=0x{state.control_word:X})"
            ),
        )

    def _update_readback(self, data):
        control     = int(data.get("control", 0))
        filt_period = int(data.get("period_avg", data.get("filt_period", 0)))
        out_period  = int(data.get("output_period", 0))
        status      = int(data.get("status", 0))
        period_valid   = (status >> 1) & 0x1
        timeout_flag   = (status >> 2) & 0x1
        period_stable  = (status >> 3) & 0x1
        freerun_active = (status >> 4) & 0x1
        enable         = control & CONTROL_PULSE_ENABLE

        self._period_valid = bool(period_valid)
        self._timeout_flag = bool(timeout_flag)

        if period_valid and filt_period > 0:
            self._period_cycles = filt_period

        raw_period  = int(data.get("raw_period", 0))
        filt_freq   = CLOCK_HZ / filt_period if filt_period > 0 else 0.0
        out_freq    = CLOCK_HZ / out_period if (out_period > 0 and freerun_active) else (
                      CLOCK_HZ / filt_period if filt_period > 0 else 0.0)

        self.stat_input.set_value(fmt_freq_hz(filt_freq) if filt_period > 0 else "—")
        self.stat_input.set_footer("from hardware")
        self.stat_output.set_value(fmt_freq_hz(out_freq) if filt_period > 0 else "—")
        self.stat_output.set_footer("freerun output" if freerun_active else "awaiting lock")
        self.stat_duty.set_value(f"{self.width_control.value() * 100:.1f} %")
        self.stat_duty.set_footer("of output period")

        if freerun_active:
            self.stat_status.set_value("RUNNING")
            self.stat_status.set_footer(f"period {out_period} cyc")
        elif period_stable:
            self.stat_status.set_value("LOCKING")
            self.stat_status.set_footer("period stable, transitioning")
        elif period_valid:
            self.stat_status.set_value("MEASURING")
            self.stat_status.set_footer("waiting for stability")
        else:
            self.stat_status.set_value("WAITING")
            self.stat_status.set_footer("no valid trigger")

        self.enable_toggle.blockSignals(True)
        self.enable_toggle.setChecked(bool(enable))
        self.enable_toggle.blockSignals(False)

        self._refresh_preview_and_stats()

        warnings: list[str] = []
        if not period_valid:
            warnings.append("No valid trigger period.")
        if timeout_flag:
            warnings.append("Trigger timeout detected on STATUS.bit2.")
        self.freq_warning_label.setText("  ".join(f"\u26a0  {text}" for text in warnings))

    def closeEvent(self, event):
        self._record_logbook("INFO", "Application closing.")
        self._stop_poll()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.connected and self.remote.host and RemoteCtl._USE_CONTROL_MASTER:
            try:
                subprocess.run(
                    ["ssh", "-O", "exit",
                     "-o", f"ControlPath={RemoteCtl._CONTROL_PATH}",
                     f"{self.remote.user}@{self.remote.host}"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Red Pitaya Pulse Control")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
