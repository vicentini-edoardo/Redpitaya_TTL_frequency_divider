#!/usr/bin/env python3
"""
redpitaya_pulse_gui_qt.py — PySide6 desktop GUI for the Red Pitaya TTL frequency divider.

Architecture
------------
- SshBackend  : persistent paramiko TCP connection, single worker thread, priority queue.
                User writes (priority 0) always execute before polls (priority 9).
                Works on Windows, macOS, and Linux without any OS-level SSH client.
- MainWindow  : Qt UI on the main thread; communicates with the backend via signals only.

Run with:  python redpitaya_pulse_gui_qt.py
Requires:  pip install PySide6 paramiko
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
    from PySide6.QtGui import QAction, QFont, QKeySequence
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
        QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
        QSizePolicy, QSlider, QTextEdit, QVBoxLayout, QWidget,
    )
except ImportError as exc:
    raise SystemExit(
        "PySide6 is required.\n"
        "  python -m pip install PySide6-Essentials"
    ) from exc

try:
    import paramiko
    _HAS_PARAMIKO = True
except ImportError:
    _HAS_PARAMIKO = False

# ─────────────────────────────────────────────────────────────────────────────
# Hardware constants
# ─────────────────────────────────────────────────────────────────────────────
CLK_HZ       = 125_000_000
PHASE_BITS   = 48
DEFAULT_BASE = 0x40600000
CTRL_ENABLE  = 0x01

_PHASE_MAX = 2 ** (PHASE_BITS - 1)
PHASE_RES_HZ = CLK_HZ / 2**PHASE_BITS

# Measurement window options: combo index → duration in microseconds
WINDOW_OPTIONS_US = [1_000, 10_000, 100_000, 500_000, 1_000_000]

# ─────────────────────────────────────────────────────────────────────────────
# Math helpers
# ─────────────────────────────────────────────────────────────────────────────

def hz_to_phase(delta_hz: float) -> int:
    v = int(round(delta_hz * 2**PHASE_BITS / CLK_HZ))
    return max(-_PHASE_MAX, min(_PHASE_MAX - 1, v))


def phase_to_hz(word: int) -> float:
    return word * CLK_HZ / 2**PHASE_BITS


MAX_SHIFT_HZ = phase_to_hz(_PHASE_MAX - 1)


def duty_to_cycles(frac: float, period: int) -> int:
    return max(1, min(period - 1, int(round(frac * period))))


def fmt_freq(hz: float) -> str:
    if hz <= 0:
        return "---"
    if hz < 1e3:
        return f"{hz:.6f} Hz"
    if hz < 1e6:
        return f"{hz / 1e3:.6f} kHz"
    return f"{hz / 1e6:.6f} MHz"


def fmt_signed_freq(hz: float) -> str:
    if abs(hz) < PHASE_RES_HZ / 2:
        return "+0.000000 Hz"
    sign = "+" if hz >= 0 else "-"
    return f"{sign}{fmt_freq(abs(hz))}"


def suggest_window(f_shift_hz: float) -> int:
    """Suggest measurement window based on frequency shift.
    Returns index 0=1ms, 1=10ms, 2=100ms, 3=500ms, 4=1000ms
    """
    if f_shift_hz <= 0:
        return 2  # Default to 100 ms
    if f_shift_hz < 1:
        return 4  # 1000 ms for sub-Hz shifts
    if f_shift_hz < 10:
        return 3  # 500 ms for 1-10 Hz
    if f_shift_hz < 100:
        return 2  # 100 ms for 10-100 Hz
    if f_shift_hz < 1000:
        return 1  # 10 ms for 100 Hz - 1 kHz
    return 0  # 1 ms for >= 1 kHz


def fmt_dur(s: float) -> str:
    if s <= 0:
        return "---"
    if s < 1e-6:
        return f"{s * 1e9:.3f} ns"
    if s < 1e-3:
        return f"{s * 1e6:.3f} µs"
    if s < 1.0:
        return f"{s * 1e3:.3f} ms"
    return f"{s:.6f} s"


# ─────────────────────────────────────────────────────────────────────────────
# SSH Backend
# ─────────────────────────────────────────────────────────────────────────────

class _Job:
    """Priority queue item. Lower pri number = higher urgency."""
    __slots__ = ("pri", "seq", "fn", "cb")
    _counter = 0

    def __init__(self, pri: int, fn: Callable, cb: Optional[Callable]):
        _Job._counter += 1
        self.pri = pri
        self.seq = _Job._counter
        self.fn  = fn
        self.cb  = cb

    def __lt__(self, other: "_Job") -> bool:
        return (self.pri, self.seq) < (other.pri, other.seq)


class SshBackend(QObject):
    """
    Maintains a single persistent paramiko SSH session.

    All SSH I/O runs in one background thread fed by a priority queue:
      P_USER (0)   – register writes triggered by the user
      P_UPLOAD (1) – file upload / compile
      P_INIT (2)   – connect / disconnect
      P_POLL (9)   – periodic register reads

    Results are returned to the Qt main thread through signals.
    """

    P_USER   = 0
    P_UPLOAD = 1
    P_INIT   = 2
    P_POLL   = 9

    sig_connected    = Signal()
    sig_disconnected = Signal(str)   # reason string
    sig_status       = Signal(dict)  # parsed JSON from the board
    sig_log          = Signal(str)
    sig_error        = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._ssh:  Optional[paramiko.SSHClient]  = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._live  = False
        self._base  = DEFAULT_BASE
        self._q: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._upload_pending: Optional[tuple] = None
        self._upload_callback: Optional[Callable] = None
        self._thread = threading.Thread(target=self._loop, name="rp-ssh", daemon=True)
        self._thread.start()

    # ── public API (called from Qt main thread) ───────────────────────────────

    def start_connect(self, host: str, port: int, user: str,
                      key: Optional[str], base: int):
        self._base = base
        self._enqueue(self.P_INIT, lambda: self._do_connect(host, port, user, key))

    def start_disconnect(self):
        self._enqueue(self.P_INIT, self._do_disconnect)

    def poll(self):
        if self._live:
            self._enqueue(self.P_POLL, self._do_read, self.sig_status.emit)

    def apply(self, width_cycles: int, offset_word: int, enable: bool):
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_write(width_cycles, offset_word, enable),
                          self.sig_status.emit)

    def set_window(self, window: int):
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_window(window),
                          self.sig_status.emit)

    def soft_reset(self):
        if self._live:
            self._enqueue(self.P_USER, self._do_reset, self.sig_status.emit)

    def upload(self, c_src: str, bit_src: Optional[str], on_connected: Optional[Callable] = None):
        if self._live:
            self._enqueue(self.P_UPLOAD, lambda: self._do_upload(c_src, bit_src))
        else:
            self._upload_pending = (c_src, bit_src)
            self._upload_callback = on_connected

    # ── internal ──────────────────────────────────────────────────────────────

    def _enqueue(self, pri: int, fn: Callable, cb: Optional[Callable] = None):
        self._q.put(_Job(pri, fn, cb))

    def _loop(self):
        while True:
            job = self._q.get()
            try:
                result = job.fn()
                if job.cb is not None and result is not None:
                    job.cb(result)
            except Exception as exc:
                self._live = False
                self.sig_error.emit(str(exc))
                self.sig_disconnected.emit(str(exc))

    def _exec(self, cmd: str, timeout: float = 10.0) -> str:
        _, stdout, stderr = self._ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode()
        err = stderr.read().decode().strip()
        if err:
            self.sig_log.emit(f"[stderr] {err}")
        return out

    def _rp_cmd(self) -> str:
        return f"/root/rp_pulse_ctl 0x{self._base:08X}"

    # ── SSH operations (worker thread) ────────────────────────────────────────

    def _do_connect(self, host: str, port: int, user: str, key: Optional[str]):
        self.sig_log.emit(f"Connecting to {user}@{host}:{port} …")
        for obj in (self._sftp, self._ssh):
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict = dict(
            hostname=host, port=port, username=user,
            timeout=12, banner_timeout=20, auth_timeout=12,
        )
        if key:
            kwargs["key_filename"] = key
        client.connect(**kwargs)
        self._ssh  = client
        self._sftp = client.open_sftp()
        self._live = True
        self.sig_log.emit("SSH connected.")
        if self._upload_pending:
            c_src, bit_src = self._upload_pending
            self._upload_pending = None
            self.sig_log.emit("Starting pending upload…")
            self._do_upload(c_src, bit_src)
        self.sig_connected.emit()

    def _do_disconnect(self):
        self._live = False
        for obj in (self._sftp, self._ssh):
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        self._sftp = self._ssh = None
        self.sig_disconnected.emit("user request")

    def _do_read(self) -> dict:
        return json.loads(self._exec(f"{self._rp_cmd()} read"))

    def _do_write(self, width: int, offset: int, enable: bool) -> dict:
        ctrl = CTRL_ENABLE if enable else 0
        return json.loads(self._exec(
            f"{self._rp_cmd()} write {width} {offset} {ctrl}"
        ))

    def _do_reset(self) -> dict:
        return json.loads(self._exec(f"{self._rp_cmd()} soft_reset"))

    def _do_window(self, meas_us: int) -> dict:
        return json.loads(self._exec(f"{self._rp_cmd()} window {meas_us}"))

    def _do_upload(self, c_src: str, bit_src: Optional[str]):
        self.sig_log.emit(f"Uploading {Path(c_src).as_posix()} …")
        self._sftp.put(c_src, "/root/rp_pulse_ctl.c")
        self.sig_log.emit("Compiling on board …")
        self._exec(
            "gcc -O2 -o /root/rp_pulse_ctl /root/rp_pulse_ctl.c",
            timeout=60,
        )
        self.sig_log.emit("Compiled OK.")
        if bit_src and Path(bit_src).exists():
            self.sig_log.emit("Uploading FPGA bitfile …")
            self._sftp.put(bit_src, "/root/red_pitaya_top.bit.bin")
            self.sig_log.emit("Loading bitfile …")
            self._exec(
                "/opt/redpitaya/bin/fpgautil -b /root/red_pitaya_top.bit.bin",
                timeout=30,
            )
            self.sig_log.emit("FPGA loaded.")


# ─────────────────────────────────────────────────────────────────────────────
# Colour palette & shared style helpers
# ─────────────────────────────────────────────────────────────────────────────
_BG     = "#0d1117"
_PANEL  = "#131a24"
_ACCENT = "#00d4ff"
_GREEN  = "#3fb950"
_AMBER  = "#d29922"
_RED    = "#f85149"
_TEXT   = "#e6edf3"
_DIM    = "#8b949e"
_BORDER = "#263241"
_MONO   = "Menlo, Consolas, 'Courier New', monospace"


def _mono_font(size: int = 10, bold: bool = False) -> QFont:
    for fam in ("Menlo", "Consolas", "Courier New"):
        f = QFont(fam, size)
        if f.exactMatch():
            f.setBold(bold)
            return f
    f = QFont("monospace", size)
    f.setBold(bold)
    return f


def _group_style() -> str:
    return f"""
        QGroupBox {{
            color: {_ACCENT};
            border: 1px solid {_BORDER};
            border-radius: 10px;
            margin-top: 16px;
            padding: 14px 12px 12px 12px;
            font-family: {_MONO};
            font-size: 10px;
            font-weight: bold;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 6px;
        }}
    """


def _btn_style(color: str = _ACCENT) -> str:
    return f"""
        QPushButton {{
            background: #111923; color: {color};
            border: 1px solid {color}; border-radius: 7px;
            padding: 6px 12px;
            font-family: {_MONO}; font-size: 10px;
        }}
        QPushButton:hover   {{ background: #182536; }}
        QPushButton:pressed {{ background: #0b1622; }}
        QPushButton:disabled {{ color: {_DIM}; border-color: {_BORDER}; }}
    """


def _le_style() -> str:
    return f"""
        QLineEdit {{
            background: {_BG}; color: {_TEXT};
            border: 1px solid {_BORDER}; border-radius: 4px;
            padding: 3px 6px;
            font-family: {_MONO}; font-size: 10px;
        }}
        QLineEdit:focus {{ border-color: {_ACCENT}; }}
    """


def _spin_style() -> str:
    return f"""
        QDoubleSpinBox, QComboBox {{
            background: #0b111a; color: {_TEXT};
            border: 1px solid {_BORDER}; border-radius: 7px;
            padding: 5px 8px;
            font-family: {_MONO}; font-size: 10px;
        }}
        QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {_ACCENT}; }}
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
            width: 16px;
        }}
        QComboBox::drop-down {{
            width: 22px;
            border: none;
        }}
        QComboBox QAbstractItemView {{
            background: #0b111a;
            color: {_TEXT};
            selection-background-color: #182536;
            border: 1px solid {_BORDER};
        }}
    """


def _slider_style(accent: str = _ACCENT) -> str:
    return f"""
        QSlider::groove:horizontal {{
            height: 4px; background: {_BORDER}; border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            width: 14px; height: 14px; margin: -5px 0;
            background: {accent}; border-radius: 7px;
        }}
        QSlider::sub-page:horizontal {{
            background: {accent}; border-radius: 2px;
        }}
    """


# ─────────────────────────────────────────────────────────────────────────────
# BigDisplay — large labelled readout
# ─────────────────────────────────────────────────────────────────────────────

class BigDisplay(QFrame):
    """
    Displays a single measurement (frequency, duration, etc.) in a large font.
    Four of these form the top row of the UI.
    """

    def __init__(self, title: str, sub_hint: str = "",
                 accent: str = _ACCENT, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._accent = accent
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"""
            QFrame {{
                background: #101722;
                border: 1px solid {_BORDER};
                border-radius: 14px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(8)

        title_lbl = QLabel(title.upper())
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(_mono_font(9, bold=True))
        title_lbl.setStyleSheet(f"color: {_DIM}; background: transparent; border: none;")
        lay.addWidget(title_lbl)

        self._val = QLabel("---")
        self._val.setAlignment(Qt.AlignCenter)
        self._val.setFont(_mono_font(32, bold=True))
        self._val.setStyleSheet(f"color: {accent}; background: transparent; border: none;")
        self._val.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self._val, 1)

        self._sub = QLabel(sub_hint)
        self._sub.setAlignment(Qt.AlignCenter)
        self._sub.setFont(_mono_font(10))
        self._sub.setStyleSheet(f"color: {_DIM}; background: transparent; border: none;")
        lay.addWidget(self._sub)

    def set_data(self, value: str, sub: str = "",
                 color: Optional[str] = None):
        self._val.setText(value)
        c = color or self._accent
        self._val.setStyleSheet(f"color: {c}; background: transparent; border: none;")
        if sub is not None:
            self._sub.setText(sub)


# ─────────────────────────────────────────────────────────────────────────────
# ParamSlider — horizontal slider paired with a spinbox
# ─────────────────────────────────────────────────────────────────────────────

class ParamSlider(QWidget):
    """Combines a QSlider and a QDoubleSpinBox that stay synchronised."""

    changed = Signal(float)

    def __init__(self, label: str, lo: float, hi: float,
                 decimals: int = 2, suffix: str = "",
                 accent: str = _ACCENT, parent: Optional[QWidget] = None,
                 spin_width: int = 115, single_step: Optional[float] = None):
        super().__init__(parent)
        self._lo  = lo
        self._hi  = hi

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        lbl = QLabel(f"{label}:")
        lbl.setFixedWidth(90)
        lbl.setFont(_mono_font(10))
        lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        row.addWidget(lbl)

        self._sl = QSlider(Qt.Horizontal)
        self._sl.setRange(0, 10000)
        self._sl.setStyleSheet(_slider_style(accent))
        row.addWidget(self._sl, 1)

        self._sp = QDoubleSpinBox()
        self._sp.setRange(lo, hi)
        self._sp.setDecimals(decimals)
        if single_step is not None:
            self._sp.setSingleStep(single_step)
        if suffix:
            self._sp.setSuffix(f" {suffix}")
        self._sp.setFixedWidth(spin_width)
        self._sp.setStyleSheet(_spin_style())
        row.addWidget(self._sp)

        self._sl.valueChanged.connect(self._from_slider)
        self._sp.valueChanged.connect(self._from_spin)

    def _frac(self, v: float) -> float:
        span = self._hi - self._lo
        return (v - self._lo) / span if span else 0.0

    def _from_slider(self, tick: int):
        v = self._lo + tick / 10000.0 * (self._hi - self._lo)
        self._sp.blockSignals(True)
        self._sp.setValue(v)
        self._sp.blockSignals(False)
        self.changed.emit(v)

    def _from_spin(self, v: float):
        self._sl.blockSignals(True)
        self._sl.setValue(int(self._frac(v) * 10000))
        self._sl.blockSignals(False)
        self.changed.emit(v)

    def value(self) -> float:
        return self._sp.value()

    def set_value(self, v: float):
        self._sp.blockSignals(True)
        self._sl.blockSignals(True)
        self._sp.setValue(v)
        self._sl.setValue(int(self._frac(v) * 10000))
        self._sp.blockSignals(False)
        self._sl.blockSignals(False)


# ─────────────────────────────────────────────────────────────────────────────
# MainWindow
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Red Pitaya — TTL Frequency Divider")
        self.setMinimumSize(960, 680)

        self._period_c = 0   # last known period in FPGA clock cycles
        self._live     = False
        self._window_select = 2  # default: 100 ms
        self._refresh_input_pending = False

        # Backend
        self._be = SshBackend(self)
        self._be.sig_connected.connect(self._on_connected)
        self._be.sig_disconnected.connect(self._on_disconnected)
        self._be.sig_status.connect(self._on_status)
        self._be.sig_log.connect(self._log)
        self._be.sig_error.connect(self._on_error)

        # Debounce timer — delays auto-apply 300 ms after last slider move
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._do_apply)

        # Poll timer — adaptive interval updated by _on_status
        self._poll = QTimer(self)
        self._poll.setInterval(800)
        self._poll.timeout.connect(self._be.poll)

        self._build_ui()
        self._set_global_style()

        # Ctrl+Return → apply
        act = QAction(self)
        act.setShortcut(QKeySequence("Ctrl+Return"))
        act.triggered.connect(self._do_apply)
        self.addAction(act)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        root.addWidget(self._build_connection())
        root.addLayout(self._build_main_area(), 1)
        root.addWidget(self._build_log())

    def _make_group(self, title: str) -> QGroupBox:
        g = QGroupBox(title)
        g.setStyleSheet(_group_style())
        return g

    def _build_connection(self) -> QGroupBox:
        g = self._make_group("Connection")
        row = QHBoxLayout(g)
        row.setSpacing(6)

        self._w_host = QLineEdit("rp-f06a51.local")
        self._w_port = QLineEdit("22");   self._w_port.setFixedWidth(55)
        self._w_user = QLineEdit("root"); self._w_user.setFixedWidth(70)
        self._w_key  = QLineEdit();       self._w_key.setPlaceholderText("SSH key (optional)")

        btn_key = QPushButton("…"); btn_key.setFixedWidth(28)
        btn_key.clicked.connect(self._pick_key)

        self._btn_conn = QPushButton("Connect"); self._btn_conn.setFixedWidth(95)
        self._btn_conn.clicked.connect(self._toggle_connect)

        self._btn_upload  = QPushButton("Upload && Compile")
        self._btn_upload.setFixedWidth(150)
        self._btn_upload.clicked.connect(self._do_upload)
        self._btn_upload.setStyleSheet(_btn_style())

        self._lbl_status = QLabel("●  Disconnected")
        self._lbl_status.setFont(_mono_font(10))
        self._lbl_status.setStyleSheet(f"color: {_RED}; background: transparent;")

        for w, lbl in ((self._w_host, "Host:"), (self._w_port, "Port:"),
                       (self._w_user, "User:"), (self._w_key, "Key:")):
            cap = QLabel(lbl)
            cap.setFont(_mono_font(9))
            cap.setStyleSheet(f"color: {_DIM}; background: transparent;")
            row.addWidget(cap)
            row.addWidget(w)

        row.addWidget(btn_key)
        row.addWidget(self._btn_conn)
        row.addWidget(self._btn_upload)
        row.addWidget(self._lbl_status)
        row.addStretch()

        for le in (self._w_host, self._w_port, self._w_user, self._w_key):
            le.setStyleSheet(_le_style())
        for b in (self._btn_conn, btn_key):
            b.setStyleSheet(_btn_style())
        return g

    def _build_main_area(self) -> QVBoxLayout:
        outer = QVBoxLayout()
        outer.setSpacing(12)

        # ── Four primary monitors in a 2×2 grid ───────────────────────────────
        self._d_in  = BigDisplay("Input Frequency",  "measured input period", _ACCENT)
        self._d_dur = BigDisplay("Pulse Duration",   "pulse high-time",       _AMBER)
        self._d_out = BigDisplay("Output Frequency", "NCO output",            _GREEN)
        self._d_dut = BigDisplay("Duty Cycle",       "width / period",        _AMBER)

        for d in (self._d_in, self._d_dur, self._d_out, self._d_dut):
            d.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            d.setMinimumHeight(150)

        grid = QGridLayout()
        grid.setSpacing(12)
        grid.addWidget(self._d_in,  0, 0)
        grid.addWidget(self._d_dur, 0, 1)
        grid.addWidget(self._d_out, 1, 0)
        grid.addWidget(self._d_dut, 1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        outer.addLayout(grid, 1)

        controls = self._make_group("Controls")
        controls_lay = QHBoxLayout(controls)
        controls_lay.setContentsMargins(12, 12, 12, 12)
        controls_lay.setSpacing(18)

        fields = QGridLayout()
        fields.setHorizontalSpacing(10)
        fields.setVerticalSpacing(8)

        # ── Editable frequency shift ──────────────────────────────────────────
        freq_lbl = QLabel("Freq shift:")
        freq_lbl.setFixedWidth(90)
        freq_lbl.setFont(_mono_font(10))
        freq_lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        fields.addWidget(freq_lbl, 0, 0)
        self._sp_offset = QDoubleSpinBox()
        self._sp_offset.setRange(-MAX_SHIFT_HZ, MAX_SHIFT_HZ)
        self._sp_offset.setDecimals(6)
        self._sp_offset.setSingleStep(1.0)
        self._sp_offset.setSuffix(" Hz")
        self._sp_offset.setFixedHeight(46)
        self._sp_offset.setMinimumWidth(300)
        self._sp_offset.setFont(_mono_font(15, bold=True))
        self._sp_offset.setStyleSheet(_spin_style())
        self._sp_offset.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_offset, 0, 1)

        width_lbl = QLabel("Width:")
        width_lbl.setFixedWidth(90)
        width_lbl.setFont(_mono_font(10))
        width_lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        fields.addWidget(width_lbl, 0, 2)
        self._sp_width = QDoubleSpinBox()
        self._sp_width.setRange(0.1, 99.9)
        self._sp_width.setDecimals(2)
        self._sp_width.setSuffix(" %")
        self._sp_width.setValue(50.0)
        self._sp_width.setFixedHeight(46)
        self._sp_width.setMinimumWidth(150)
        self._sp_width.setFont(_mono_font(15, bold=True))
        self._sp_width.setStyleSheet(_spin_style())
        self._sp_width.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_width, 0, 3)

        # ── Measurement window and suggestion ────────────────────────────────
        window_lbl = QLabel("Meas. window:")
        window_lbl.setFixedWidth(90)
        window_lbl.setFont(_mono_font(10))
        window_lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        fields.addWidget(window_lbl, 1, 0)
        self._cb_window = QComboBox()
        self._cb_window.addItems(["1 ms", "10 ms", "100 ms", "500 ms", "1000 ms"])
        self._cb_window.setCurrentIndex(2)  # Default: 100 ms
        self._cb_window.setFixedHeight(38)
        self._cb_window.setFixedWidth(118)
        self._cb_window.setFont(_mono_font(10))
        self._cb_window.setStyleSheet(_spin_style())
        self._cb_window.currentIndexChanged.connect(self._on_window_changed)
        fields.addWidget(self._cb_window, 1, 1)
        self._lbl_window_suggest = QLabel()
        self._lbl_window_suggest.setFont(_mono_font(9))
        self._lbl_window_suggest.setStyleSheet(f"color: {_AMBER}; background: transparent;")
        fields.addWidget(self._lbl_window_suggest, 1, 2, 1, 2)

        toggles = QHBoxLayout()
        toggles.setSpacing(18)
        self._cb_en = QCheckBox("Enable Output")
        self._cb_auto = QCheckBox("Auto-Apply")
        self._cb_auto.setChecked(True)
        for cb in (self._cb_en, self._cb_auto):
            cb.setFont(_mono_font(10))
            cb.setStyleSheet(f"color: {_TEXT}; background: transparent;")
            toggles.addWidget(cb)
        toggles.addStretch()
        self._cb_en.toggled.connect(self._param_changed)
        fields.addLayout(toggles, 2, 0, 1, 4)

        # ── Shift detail label ─────────────────────────────────────────────────
        self._lbl_shift = QLabel()
        self._lbl_shift.setFont(_mono_font(9))
        self._lbl_shift.setWordWrap(True)
        self._lbl_shift.setStyleSheet(f"color: {_DIM}; background: transparent;")
        fields.addWidget(self._lbl_shift, 3, 0, 1, 4)
        fields.setColumnStretch(1, 1)
        controls_lay.addLayout(fields, 1)

        actions = QVBoxLayout()
        actions.setSpacing(8)
        self._btn_apply = QPushButton("Apply Now\nCtrl+↵")
        self._btn_apply.setFixedWidth(210)
        self._btn_apply.setFixedHeight(92)
        self._btn_apply.setFont(_mono_font(13, bold=True))
        self._btn_apply.setStyleSheet(_btn_style(_GREEN))
        self._btn_apply.clicked.connect(self._do_apply)
        actions.addWidget(self._btn_apply)

        self._btn_reset = QPushButton("Soft Reset")
        self._btn_reset.setFixedWidth(210)
        self._btn_reset.setFixedHeight(34)
        self._btn_reset.setStyleSheet(_btn_style(_AMBER))
        self._btn_reset.clicked.connect(self._do_soft_reset)
        actions.addWidget(self._btn_reset)
        actions.addStretch()
        controls_lay.addLayout(actions)

        outer.addWidget(controls)
        self._update_shift_detail()
        return outer

    def _build_log(self) -> QGroupBox:
        g = self._make_group("Log")
        lay = QVBoxLayout(g)
        lay.setContentsMargins(10, 10, 10, 8)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(78)
        self._log_box.setFont(_mono_font(9))
        self._log_box.setStyleSheet(
            f"background: #090e15; color: {_DIM}; border: none; border-radius: 6px;"
        )
        lay.addWidget(self._log_box)
        return g

    def _set_global_style(self):
        self.setStyleSheet(
            f"QMainWindow, QWidget {{ background: {_BG}; color: {_TEXT}; }}"
        )

    # ── connection handling ───────────────────────────────────────────────────

    def _pick_key(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SSH private key", str(Path.home() / ".ssh")
        )
        if path:
            self._w_key.setText(path)

    def _toggle_connect(self):
        if self._live:
            self._be.start_disconnect()
        else:
            if not _HAS_PARAMIKO:
                self._log("ERROR: paramiko not installed — run: pip install paramiko")
                return
            host = self._w_host.text().strip()
            port = int(self._w_port.text().strip() or "22")
            user = self._w_user.text().strip() or "root"
            key  = self._w_key.text().strip() or None
            self._btn_conn.setEnabled(False)
            self._lbl_status.setText("●  Connecting …")
            self._lbl_status.setStyleSheet(f"color: {_ACCENT}; background: transparent;")
            self._be.start_connect(host, port, user, key, DEFAULT_BASE)

    @Slot()
    def _on_connected(self):
        self._live = True
        self._btn_conn.setText("Disconnect")
        self._btn_conn.setEnabled(True)
        self._btn_upload.setEnabled(True)
        self._lbl_status.setText("●  Connected")
        self._lbl_status.setStyleSheet(f"color: {_GREEN}; background: transparent;")
        # Set initial window selection
        self._be.set_window(WINDOW_OPTIONS_US[self._cb_window.currentIndex()])
        self._poll.start()
        self._be.poll()   # kick off immediate first read rather than waiting 800 ms
        self._log("Connected.")

    def _on_upload_connected(self):
        pass

    @Slot(str)
    def _on_disconnected(self, reason: str):
        self._live = False
        self._refresh_input_pending = False
        self._poll.stop()
        self._btn_conn.setText("Connect")
        self._btn_conn.setEnabled(True)
        self._lbl_status.setText("●  Disconnected")
        self._lbl_status.setStyleSheet(f"color: {_RED}; background: transparent;")
        self._log(f"Disconnected: {reason}")

    # ── parameter controls ────────────────────────────────────────────────────

    def _param_changed(self, *_):
        """Called on any slider/spinbox/checkbox change."""
        if self._cb_auto.isChecked():
            self._debounce.start()   # restart 300 ms window
        self._update_local_displays()
        self._update_window_suggestion()

    def _on_window_changed(self, idx: int):
        """Called when user changes window selection."""
        self._window_select = idx
        if self._live:
            self._be.set_window(WINDOW_OPTIONS_US[idx])
        self._update_window_suggestion()

    def _update_local_displays(self):
        """Immediately refresh duration & duty from local slider state."""
        self._update_shift_detail()
        if self._period_c <= 0:
            return
        frac = self._sp_width.value() / 100.0
        wc   = duty_to_cycles(frac, self._period_c)
        self._d_dur.set_data(fmt_dur(wc / CLK_HZ))
        self._d_dut.set_data(f"{self._sp_width.value():.2f} %")

    def _update_shift_detail(self):
        requested_hz = self._sp_offset.value()
        offset_word = hz_to_phase(requested_hz)
        actual_hz = phase_to_hz(offset_word)
        if self._period_c > 0:
            input_hz = CLK_HZ / self._period_c
            output_hz = input_hz + actual_hz
            out_text = f", target output {fmt_freq(output_hz)}" if output_hz > 0 else ""
        else:
            out_text = ""
        self._lbl_shift.setText(
            f"Frequency shift: requested {requested_hz:+.6f} Hz, "
            f"actual {actual_hz:+.6f} Hz, register {offset_word:+d}, "
            f"resolution {PHASE_RES_HZ:.9f} Hz/LSB{out_text}"
        )

    def _update_window_suggestion(self):
        """Update window suggestion based on frequency shift."""
        f_shift = abs(self._sp_offset.value())
        suggested = suggest_window(f_shift)
        window_names = ["1 ms", "10 ms", "100 ms", "500 ms", "1000 ms"]
        current = self._cb_window.currentIndex()
        if current == suggested:
            self._lbl_window_suggest.setText(f"✓ optimal for {fmt_freq(f_shift) if f_shift > 0 else '---'}")
            self._lbl_window_suggest.setStyleSheet(f"color: {_GREEN}; background: transparent;")
        else:
            self._lbl_window_suggest.setText(f"suggested: {window_names[suggested]} for {fmt_freq(f_shift) if f_shift > 0 else '---'}")
            self._lbl_window_suggest.setStyleSheet(f"color: {_AMBER}; background: transparent;")

    def _do_apply(self):
        if not self._live:
            return
        frac   = self._sp_width.value() / 100.0
        off_hz = self._sp_offset.value()
        enable = self._cb_en.isChecked()
        period = self._period_c if self._period_c > 0 else 1000
        wc     = duty_to_cycles(frac, period)
        offset_word = hz_to_phase(off_hz)
        actual_hz = phase_to_hz(offset_word)
        self._be.apply(wc, offset_word, enable)
        self._log(
            f"Apply  width={self._sp_width.value():.2f}%  "
            f"shift={actual_hz:+.6f} Hz  "
            f"phase_offset={offset_word:+d}  "
            f"enable={enable}"
        )

    def _do_measure_input(self):
        if not self._live:
            return
        self._refresh_input_pending = True
        self._d_in.set_data("…", "measuring", _AMBER)
        self._be.poll()
        self._log("Measure Input requested.")

    def _do_soft_reset(self):
        if not self._live:
            return
        self._refresh_input_pending = True
        self._be.soft_reset()

    def _do_upload(self):
        here    = Path(__file__).resolve().parent
        c_src   = os.fspath(here / "rp_pulse_ctl.c")
        bit_src = os.fspath(here / "red_pitaya_top.bit.bin")
        if not Path(c_src).exists():
            self._log(f"ERROR: source file not found: {Path(c_src).as_posix()}")
            return
        if not self._live:
            self._log("Not connected. Connecting first…")
            self._btn_upload.setEnabled(False)
            host = self._w_host.text().strip()
            port = int(self._w_port.text().strip() or "22")
            user = self._w_user.text().strip() or "root"
            key  = self._w_key.text().strip() or None
            self._be.upload(c_src, bit_src if Path(bit_src).exists() else None, self._on_upload_connected)
            self._be.start_connect(host, port, user, key, DEFAULT_BASE)
        else:
            self._be.upload(c_src, bit_src if Path(bit_src).exists() else None)

    # ── status update from hardware ───────────────────────────────────────────

    @Slot(dict)
    def _on_status(self, d: dict):
        # Update window combo from FPGA meas_time_us if it changed
        raw_us = d.get("meas_time_us")
        if raw_us is not None:
            us_val = int(raw_us)
            try:
                fpga_idx = WINDOW_OPTIONS_US.index(us_val)
            except ValueError:
                fpga_idx = min(range(len(WINDOW_OPTIONS_US)),
                               key=lambda i: abs(WINDOW_OPTIONS_US[i] - us_val))
            if fpga_idx != self._cb_window.currentIndex():
                self._cb_window.blockSignals(True)
                self._cb_window.setCurrentIndex(fpga_idx)
                self._cb_window.blockSignals(False)
                self._window_select = fpga_idx

        period = int(d.get("period_avg") or d.get("period") or 0)
        stable = bool(d.get("period_stable"))

        # Only update the displayed input frequency when explicitly requested.
        if self._refresh_input_pending:
            if period > 0:
                self._period_c = period
                in_hz = CLK_HZ / period
                self._d_in.set_data(
                    fmt_freq(in_hz),
                    "stable" if stable else "acquiring …",
                    _ACCENT if stable else _AMBER,
                )
                self._refresh_input_pending = False
                self._log(
                    f"Input measured: {fmt_freq(in_hz)} "
                    f"({'stable' if stable else 'acquiring'})"
                )
            else:
                # No signal yet; keep the flag set so the next poll tries again.
                self._d_in.set_data("---", "no input signal", _RED)

        # Output, width/duty, and adaptive poll interval update on every tick.
        if period > 0:
            step_base = int(d.get("phase_step_base") or 0)
            step_live = int(d.get("phase_step") or step_base)
            step_off  = int(d.get("phase_step_offset") or (step_live - step_base))
            out_hz = phase_to_hz(step_live)
            delta  = phase_to_hz(step_off)
            self._d_out.set_data(fmt_freq(out_hz), f"shift {fmt_signed_freq(delta)}")
            self._update_shift_detail()

            wc = int(d.get("width") or 0)
            if wc > 0:
                self._d_dur.set_data(fmt_dur(wc / CLK_HZ))
                self._d_dut.set_data(f"{wc / period * 100:.2f} %")

            self._poll.setInterval(400 if not stable else 800)
        else:
            self._poll.setInterval(400)

    # ── error / log ───────────────────────────────────────────────────────────

    @Slot(str)
    def _on_error(self, msg: str):
        self._lbl_status.setText("●  Error")
        self._lbl_status.setStyleSheet(f"color: {_RED}; background: transparent;")
        self._log(f"ERROR  {msg}")

    @Slot(str)
    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._log_box.append(f"[{ts}]  {msg}")
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())


# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("RP Pulse GUI")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
