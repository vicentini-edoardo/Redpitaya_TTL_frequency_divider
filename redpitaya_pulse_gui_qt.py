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
        QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFrame,
        QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
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

    def soft_reset(self):
        if self._live:
            self._enqueue(self.P_USER, self._do_reset, self.sig_status.emit)

    def upload(self, c_src: str, bit_src: Optional[str]):
        if self._live:
            self._enqueue(self.P_UPLOAD, lambda: self._do_upload(c_src, bit_src))

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

    def _do_upload(self, c_src: str, bit_src: Optional[str]):
        self.sig_log.emit("Uploading rp_pulse_ctl.c …")
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
_PANEL  = "#161b22"
_ACCENT = "#00d4ff"
_GREEN  = "#3fb950"
_AMBER  = "#d29922"
_RED    = "#f85149"
_TEXT   = "#e6edf3"
_DIM    = "#8b949e"
_BORDER = "#30363d"
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
            border-radius: 6px;
            margin-top: 14px;
            padding: 10px 8px 8px 8px;
            font-family: {_MONO};
            font-size: 9px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }}
    """


def _btn_style(color: str = _ACCENT) -> str:
    return f"""
        QPushButton {{
            background: {_PANEL}; color: {color};
            border: 1px solid {color}; border-radius: 4px;
            padding: 4px 10px;
            font-family: {_MONO}; font-size: 10px;
        }}
        QPushButton:hover   {{ background: #1c2333; }}
        QPushButton:pressed {{ background: #0d1824; }}
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
        QDoubleSpinBox {{
            background: {_BG}; color: {_TEXT};
            border: 1px solid {_BORDER}; border-radius: 4px;
            padding: 3px;
            font-family: {_MONO}; font-size: 10px;
        }}
        QDoubleSpinBox:focus {{ border-color: {_ACCENT}; }}
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
            width: 16px;
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
                background: {_PANEL};
                border: 1px solid {_BORDER};
                border-radius: 8px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)

        title_lbl = QLabel(title.upper())
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(_mono_font(8))
        title_lbl.setStyleSheet(f"color: {_DIM}; background: transparent; border: none;")
        lay.addWidget(title_lbl)

        self._val = QLabel("---")
        self._val.setAlignment(Qt.AlignCenter)
        self._val.setFont(_mono_font(26, bold=True))
        self._val.setStyleSheet(f"color: {accent}; background: transparent; border: none;")
        self._val.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self._val)

        self._sub = QLabel(sub_hint)
        self._sub.setAlignment(Qt.AlignCenter)
        self._sub.setFont(_mono_font(9))
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
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

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
        row.addWidget(self._lbl_status)
        row.addStretch()

        for le in (self._w_host, self._w_port, self._w_user, self._w_key):
            le.setStyleSheet(_le_style())
        for b in (self._btn_conn, btn_key):
            b.setStyleSheet(_btn_style())
        return g

    def _build_main_area(self) -> QVBoxLayout:
        outer = QVBoxLayout()
        outer.setSpacing(8)

        # Two-column layout
        cols = QHBoxLayout()
        cols.setSpacing(10)

        # ── Left: Input Freq → Freq-shift spinbox → Output Freq ──────────────
        left = QVBoxLayout()
        left.setSpacing(8)

        self._d_in = BigDisplay(
            "Input Frequency", "measured input period", _ACCENT
        )
        left.addWidget(self._d_in, 1)

        freq_row = QHBoxLayout()
        freq_row.setContentsMargins(0, 0, 0, 0)
        freq_row.setSpacing(8)
        freq_lbl = QLabel("Freq shift:")
        freq_lbl.setFixedWidth(90)
        freq_lbl.setFont(_mono_font(10))
        freq_lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        freq_row.addWidget(freq_lbl)
        self._sp_offset = QDoubleSpinBox()
        self._sp_offset.setRange(-MAX_SHIFT_HZ, MAX_SHIFT_HZ)
        self._sp_offset.setDecimals(6)
        self._sp_offset.setSingleStep(1.0)
        self._sp_offset.setSuffix(" Hz")
        self._sp_offset.setFixedHeight(44)
        self._sp_offset.setMinimumWidth(260)
        self._sp_offset.setFont(_mono_font(15, bold=True))
        self._sp_offset.setStyleSheet(_spin_style())
        self._sp_offset.valueChanged.connect(self._param_changed)
        freq_row.addWidget(self._sp_offset)
        freq_row.addStretch()
        left.addLayout(freq_row)

        self._d_out = BigDisplay("Output Frequency", "NCO output", _GREEN)
        left.addWidget(self._d_out, 1)

        # ── Right: Pulse Duration → Width slider → Duty Cycle ────────────────
        right = QVBoxLayout()
        right.setSpacing(8)

        self._d_dur = BigDisplay("Pulse Duration", "pulse high-time", _AMBER)
        right.addWidget(self._d_dur, 1)

        self._sl_width = ParamSlider("Width", 0.1, 99.9, 2, "%", _ACCENT)
        self._sl_width.set_value(50.0)
        self._sl_width.changed.connect(self._param_changed)
        right.addWidget(self._sl_width)

        self._d_dut = BigDisplay("Duty Cycle", "width / period", _AMBER)
        right.addWidget(self._d_dut, 1)

        cols.addLayout(left, 1)
        cols.addLayout(right, 1)
        outer.addLayout(cols, 1)

        # ── Shift detail label ────────────────────────────────────────────────
        self._lbl_shift = QLabel()
        self._lbl_shift.setFont(_mono_font(9))
        self._lbl_shift.setWordWrap(True)
        self._lbl_shift.setStyleSheet(
            f"color: {_DIM}; background: transparent;"
        )
        outer.addWidget(self._lbl_shift)

        # ── Buttons ───────────────────────────────────────────────────────────
        btns = QHBoxLayout()
        btns.setSpacing(10)
        self._cb_en   = QCheckBox("Enable Output")
        self._cb_auto = QCheckBox("Auto-Apply")
        self._cb_auto.setChecked(True)
        self._btn_apply  = QPushButton("Apply Now   Ctrl+↵")
        self._btn_reset  = QPushButton("Soft Reset")
        self._btn_upload = QPushButton("Upload && Compile")

        for cb in (self._cb_en, self._cb_auto):
            cb.setFont(_mono_font(10))
            cb.setStyleSheet(f"color: {_TEXT}; background: transparent;")
            btns.addWidget(cb)
        for b in (self._btn_apply, self._btn_reset, self._btn_upload):
            b.setStyleSheet(_btn_style())
            btns.addWidget(b)
        btns.addStretch()

        self._cb_en.toggled.connect(self._param_changed)
        self._btn_apply.clicked.connect(self._do_apply)
        self._btn_reset.clicked.connect(self._be.soft_reset)
        self._btn_upload.clicked.connect(self._do_upload)

        outer.addLayout(btns)
        self._update_shift_detail()
        return outer

    def _build_log(self) -> QGroupBox:
        g = self._make_group("Log")
        lay = QVBoxLayout(g)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(120)
        self._log_box.setFont(_mono_font(9))
        self._log_box.setStyleSheet(
            f"background: {_BG}; color: {_DIM}; border: none; border-radius: 4px;"
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
        self._lbl_status.setText("●  Connected")
        self._lbl_status.setStyleSheet(f"color: {_GREEN}; background: transparent;")
        self._poll.start()
        self._log("Connected.")

    @Slot(str)
    def _on_disconnected(self, reason: str):
        self._live = False
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

    def _update_local_displays(self):
        """Immediately refresh duration & duty from local slider state."""
        self._update_shift_detail()
        if self._period_c <= 0:
            return
        frac = self._sl_width.value() / 100.0
        wc   = duty_to_cycles(frac, self._period_c)
        self._d_dur.set_data(fmt_dur(wc / CLK_HZ))
        self._d_dut.set_data(f"{self._sl_width.value():.2f} %")

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

    def _do_apply(self):
        if not self._live:
            return
        frac   = self._sl_width.value() / 100.0
        off_hz = self._sp_offset.value()
        enable = self._cb_en.isChecked()
        period = self._period_c if self._period_c > 0 else 1000
        wc     = duty_to_cycles(frac, period)
        offset_word = hz_to_phase(off_hz)
        actual_hz = phase_to_hz(offset_word)
        self._be.apply(wc, offset_word, enable)
        self._log(
            f"Apply  width={self._sl_width.value():.2f}%  "
            f"shift={actual_hz:+.6f} Hz  "
            f"phase_offset={offset_word:+d}  "
            f"enable={enable}"
        )

    def _do_upload(self):
        if not self._live:
            return
        here    = Path(__file__).parent
        c_src   = str(here / "rp_pulse_ctl.c")
        bit_src = str(here / "red_pitaya_top.bit.bin")
        self._be.upload(c_src, bit_src if Path(bit_src).exists() else None)

    # ── status update from hardware ───────────────────────────────────────────

    @Slot(dict)
    def _on_status(self, d: dict):
        period = int(d.get("period_avg") or d.get("period") or 0)

        if period > 0:
            self._period_c = period
            in_hz  = CLK_HZ / period
            stable = bool(d.get("period_stable"))

            self._d_in.set_data(
                fmt_freq(in_hz),
                "stable" if stable else "acquiring …",
                _ACCENT if stable else _AMBER,
            )

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

            # Faster polling while signal is unstable
            self._poll.setInterval(400 if not stable else 800)
        else:
            self._d_in.set_data("---", "no input signal", _RED)
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
