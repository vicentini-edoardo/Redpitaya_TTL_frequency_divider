#!/usr/bin/env python3
"""
redpitaya_harmonic_gui_qt.py — PySide6 desktop GUI for the Red Pitaya harmonic generator.

Generates a 50% duty-cycle TTL square wave at  f_out = N * f_input + f_shift
where N is selectable from 1 to 5 and f_shift is a signed frequency offset.

Architecture
------------
- SshBackend  : persistent paramiko TCP connection, single worker thread, priority queue.
                User writes (priority 0) always execute before polls (priority 9).
- MainWindow  : Qt UI on the main thread; communicates with the backend via signals only.

Run with:  python redpitaya_harmonic_gui_qt.py
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
        QSizePolicy, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
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
CLK_HZ       = 124_999_999
PHASE_BITS   = 48
DEFAULT_BASE = 0x40600000

# control register bits
CTRL_ENABLE     = 0x01   # bit 0 — enable output + NCO
CTRL_FORCE_HIGH = 0x04   # bit 2 — force output HIGH (constant 1)
# bit 3 (CTRL_HARMONIC = 0x08) is enforced by the C helper; not set here

_PHASE_MAX = 2 ** (PHASE_BITS - 1)
PHASE_RES_HZ = CLK_HZ / 2**PHASE_BITS

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
    if f_shift_hz <= 0:
        return 2
    if f_shift_hz < 1:
        return 4
    if f_shift_hz < 10:
        return 3
    if f_shift_hz < 100:
        return 2
    if f_shift_hz < 1000:
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# SSH Backend
# ─────────────────────────────────────────────────────────────────────────────

class _Job:
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

    Priority queue:
      P_USER (0)   – register writes triggered by the user
      P_UPLOAD (1) – file upload / compile
      P_INIT (2)   – connect / disconnect
      P_POLL (9)   – periodic register reads
    """

    P_USER   = 0
    P_UPLOAD = 1
    P_INIT   = 2
    P_POLL   = 9

    sig_connected    = Signal()
    sig_disconnected = Signal(str)
    sig_status       = Signal(dict)
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
        self._thread = threading.Thread(target=self._loop, name="rp-ssh", daemon=True)
        self._thread.start()

    # ── public API ────────────────────────────────────────────────────────────

    def start_connect(self, host: str, port: int, user: str,
                      key: Optional[str], base: int):
        self._base = base
        self._enqueue(self.P_INIT, lambda: self._do_connect(host, port, user, key))

    def start_disconnect(self):
        self._enqueue(self.P_INIT, self._do_disconnect)

    def poll(self):
        if self._live:
            self._enqueue(self.P_POLL, self._do_read, self.sig_status.emit)

    def apply(self, mult_n: int, offset_word: int):
        """Send modulated-mode write (enable=1, force_high=0, harmonic_mode=1 via helper)."""
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_write(mult_n, offset_word),
                          self.sig_status.emit)

    def set_control(self, ctrl: int):
        """Set control register directly (for Laser Off / Laser On)."""
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_set_control(ctrl),
                          self.sig_status.emit)

    def set_window(self, window: int):
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_window(window),
                          self.sig_status.emit)

    def soft_reset(self):
        if self._live:
            self._enqueue(self.P_USER, self._do_reset, self.sig_status.emit)

    def upload(self, c_src: str, bit_src: Optional[str]):
        if self._live:
            self._enqueue(self.P_UPLOAD, lambda: self._do_upload(c_src, bit_src))
        else:
            self._upload_pending = (c_src, bit_src)

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
        return f"/root/rp_harmonic_ctl 0x{self._base:08X}"

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

    def _do_write(self, mult_n: int, offset: int) -> dict:
        return json.loads(self._exec(
            f"{self._rp_cmd()} write {mult_n} {offset} {CTRL_ENABLE}"
        ))

    def _do_set_control(self, ctrl: int) -> dict:
        return json.loads(self._exec(f"{self._rp_cmd()} control {ctrl}"))

    def _do_reset(self) -> dict:
        return json.loads(self._exec(f"{self._rp_cmd()} soft_reset"))

    def _do_window(self, meas_us: int) -> dict:
        return json.loads(self._exec(f"{self._rp_cmd()} window {meas_us}"))

    def _do_upload(self, c_src: str, bit_src: Optional[str]):
        self.sig_log.emit(f"Uploading {Path(c_src).as_posix()} …")
        self._sftp.put(c_src, "/root/rp_harmonic_ctl.c")
        self.sig_log.emit("Compiling on board …")
        self._exec(
            "gcc -O2 -o /root/rp_harmonic_ctl /root/rp_harmonic_ctl.c",
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
_WHITE  = "#e6edf3"
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


def _mode_btn_style(color: str, active: bool) -> str:
    """Style for the three output-mode buttons (OFF / MODULATED / ON)."""
    if active:
        return f"""
            QPushButton {{
                background: {color}28; color: {color};
                border: 2px solid {color}; border-radius: 7px;
                padding: 5px 16px;
                font-family: {_MONO}; font-size: 10px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {color}38; }}
        """
    return f"""
        QPushButton {{
            background: #111923; color: {_DIM};
            border: 1px solid {_BORDER}; border-radius: 7px;
            padding: 5px 16px;
            font-family: {_MONO}; font-size: 10px;
        }}
        QPushButton:hover {{ background: #182536; color: {color}; border-color: {color}; }}
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
        QDoubleSpinBox, QSpinBox, QComboBox {{
            background: #0b111a; color: {_TEXT};
            border: 1px solid {_BORDER}; border-radius: 7px;
            padding: 5px 8px;
            font-family: {_MONO}; font-size: 10px;
        }}
        QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {_ACCENT}; }}
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
        QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; }}
        QComboBox::drop-down {{ width: 22px; border: none; }}
        QComboBox QAbstractItemView {{
            background: #0b111a; color: {_TEXT};
            selection-background-color: #182536;
            border: 1px solid {_BORDER};
        }}
    """


# ─────────────────────────────────────────────────────────────────────────────
# BigDisplay — large labelled readout
# ─────────────────────────────────────────────────────────────────────────────

class BigDisplay(QFrame):
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

    def set_data(self, value: str, sub: str = "", color: Optional[str] = None):
        self._val.setText(value)
        c = color or self._accent
        self._val.setStyleSheet(f"color: {c}; background: transparent; border: none;")
        if sub is not None:
            self._sub.setText(sub)


# ─────────────────────────────────────────────────────────────────────────────
# MainWindow
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Red Pitaya — Harmonic Generator")
        self.setMinimumSize(960, 700)

        self._period_c  = 0
        self._live      = False
        self._window_select = 2
        self._refresh_input_pending = False
        self._output_mode: str = "modulated"   # "off" | "modulated" | "on"

        self._be = SshBackend(self)
        self._be.sig_connected.connect(self._on_connected)
        self._be.sig_disconnected.connect(self._on_disconnected)
        self._be.sig_status.connect(self._on_status)
        self._be.sig_log.connect(self._log)
        self._be.sig_error.connect(self._on_error)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._do_apply)

        self._poll = QTimer(self)
        self._poll.setInterval(800)
        self._poll.timeout.connect(self._be.poll)

        self._build_ui()
        self._set_global_style()

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

        self._btn_upload = QPushButton("Upload && Compile")
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

        self._d_in     = BigDisplay("Input Frequency",  "measured input period", _ACCENT)
        self._d_n      = BigDisplay("Harmonic N",       "applied multiplier",    _AMBER)
        self._d_out    = BigDisplay("Output Frequency", "N·f_in + f_shift",      _GREEN)
        self._d_status = BigDisplay("NCO Status",       "lock / acquiring",      _AMBER)

        for d in (self._d_in, self._d_n, self._d_out, self._d_status):
            d.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            d.setMinimumHeight(150)

        grid = QGridLayout()
        grid.setSpacing(12)
        grid.addWidget(self._d_in,     0, 0)
        grid.addWidget(self._d_n,      0, 1)
        grid.addWidget(self._d_out,    1, 0)
        grid.addWidget(self._d_status, 1, 1)
        grid.setRowStretch(0, 1); grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        outer.addLayout(grid, 1)

        controls = self._make_group("Controls")
        controls_lay = QHBoxLayout(controls)
        controls_lay.setContentsMargins(12, 12, 12, 12)
        controls_lay.setSpacing(18)

        left_col = QVBoxLayout()
        left_col.setSpacing(10)

        # ── Output mode bar ───────────────────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)

        mode_lbl = QLabel("Output mode:")
        mode_lbl.setFont(_mono_font(10, bold=True))
        mode_lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        mode_row.addWidget(mode_lbl)

        self._btn_off = QPushButton("■  LASER OFF")
        self._btn_mod = QPushButton("~  MODULATED")
        self._btn_on  = QPushButton("●  LASER ON")
        for btn in (self._btn_off, self._btn_mod, self._btn_on):
            btn.setFixedHeight(34)
            btn.setFont(_mono_font(10))
            mode_row.addWidget(btn)
        mode_row.addStretch()

        self._btn_off.clicked.connect(lambda: self._set_output_mode("off"))
        self._btn_mod.clicked.connect(lambda: self._set_output_mode("modulated"))
        self._btn_on.clicked.connect(lambda: self._set_output_mode("on"))
        left_col.addLayout(mode_row)

        # Divider
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        left_col.addWidget(sep)

        # ── Parameter fields ──────────────────────────────────────────────────
        fields = QGridLayout()
        fields.setHorizontalSpacing(10)
        fields.setVerticalSpacing(8)

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

        n_lbl = QLabel("Harmonic N:")
        n_lbl.setFixedWidth(90)
        n_lbl.setFont(_mono_font(10))
        n_lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        fields.addWidget(n_lbl, 0, 2)

        self._sp_n = QSpinBox()
        self._sp_n.setRange(1, 5)
        self._sp_n.setValue(1)
        self._sp_n.setFixedHeight(46)
        self._sp_n.setMinimumWidth(90)
        self._sp_n.setFont(_mono_font(15, bold=True))
        self._sp_n.setStyleSheet(_spin_style())
        self._sp_n.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_n, 0, 3)

        window_lbl = QLabel("Meas. window:")
        window_lbl.setFixedWidth(90)
        window_lbl.setFont(_mono_font(10))
        window_lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        fields.addWidget(window_lbl, 1, 0)

        self._cb_window = QComboBox()
        self._cb_window.addItems(["1 ms", "10 ms", "100 ms", "500 ms", "1000 ms"])
        self._cb_window.setCurrentIndex(2)
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

        auto_row = QHBoxLayout()
        self._cb_auto = QCheckBox("Auto-Apply")
        self._cb_auto.setChecked(True)
        self._cb_auto.setFont(_mono_font(10))
        self._cb_auto.setStyleSheet(f"color: {_TEXT}; background: transparent;")
        auto_row.addWidget(self._cb_auto)
        auto_row.addStretch()
        fields.addLayout(auto_row, 2, 0, 1, 4)

        self._lbl_shift = QLabel()
        self._lbl_shift.setFont(_mono_font(9))
        self._lbl_shift.setWordWrap(True)
        self._lbl_shift.setStyleSheet(f"color: {_DIM}; background: transparent;")
        fields.addWidget(self._lbl_shift, 3, 0, 1, 4)
        fields.setColumnStretch(1, 1)

        left_col.addLayout(fields)
        controls_lay.addLayout(left_col, 1)

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
        self._update_mode_styles()
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

    # ── Output mode ───────────────────────────────────────────────────────────

    def _set_output_mode(self, mode: str):
        self._output_mode = mode
        self._update_mode_styles()
        self._update_mode_controls()
        if self._live:
            if mode == "off":
                self._be.set_control(0x00)
                self._log("Laser OFF  (output = constant 0)")
            elif mode == "on":
                self._be.set_control(CTRL_FORCE_HIGH)
                self._log("Laser ON   (output = constant 1)")
            else:
                self._do_apply()

    def _update_mode_styles(self):
        m = self._output_mode
        self._btn_off.setStyleSheet(_mode_btn_style(_RED,   m == "off"))
        self._btn_mod.setStyleSheet(_mode_btn_style(_GREEN, m == "modulated"))
        self._btn_on.setStyleSheet(_mode_btn_style(_WHITE,  m == "on"))

    def _update_mode_controls(self):
        enabled = (self._output_mode == "modulated")
        for w in (self._sp_offset, self._sp_n, self._cb_window,
                  self._cb_auto, self._btn_apply):
            w.setEnabled(enabled)

    # ── Connection handling ───────────────────────────────────────────────────

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
        self._be.set_window(WINDOW_OPTIONS_US[self._cb_window.currentIndex()])
        self._poll.start()
        self._be.poll()
        self._log("Connected.")

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

    # ── Parameter controls ────────────────────────────────────────────────────

    def _param_changed(self, *_):
        if self._output_mode == "modulated" and self._cb_auto.isChecked():
            self._debounce.start()
        self._update_shift_detail()
        self._update_window_suggestion()

    def _on_window_changed(self, idx: int):
        self._window_select = idx
        if self._live:
            self._be.set_window(WINDOW_OPTIONS_US[idx])
        self._update_window_suggestion()

    def _update_shift_detail(self):
        n            = self._sp_n.value()
        requested_hz = self._sp_offset.value()
        offset_word  = hz_to_phase(requested_hz)
        actual_hz    = phase_to_hz(offset_word)
        if self._period_c > 0:
            input_hz  = CLK_HZ / self._period_c
            output_hz = n * input_hz + actual_hz
            out_text  = f", target output {fmt_freq(output_hz)}" if output_hz > 0 else ""
        else:
            out_text = ""
        self._lbl_shift.setText(
            f"N={n}  shift: requested {requested_hz:+.6f} Hz, "
            f"actual {actual_hz:+.6f} Hz, register {offset_word:+d}, "
            f"resolution {PHASE_RES_HZ:.9f} Hz/LSB{out_text}"
        )

    def _update_window_suggestion(self):
        f_shift      = abs(self._sp_offset.value())
        suggested    = suggest_window(f_shift)
        window_names = ["1 ms", "10 ms", "100 ms", "500 ms", "1000 ms"]
        current      = self._cb_window.currentIndex()
        if current == suggested:
            self._lbl_window_suggest.setText(f"✓ optimal for {fmt_freq(f_shift) if f_shift > 0 else '---'}")
            self._lbl_window_suggest.setStyleSheet(f"color: {_GREEN}; background: transparent;")
        else:
            self._lbl_window_suggest.setText(f"suggested: {window_names[suggested]} for {fmt_freq(f_shift) if f_shift > 0 else '---'}")
            self._lbl_window_suggest.setStyleSheet(f"color: {_AMBER}; background: transparent;")

    def _do_apply(self):
        if not self._live or self._output_mode != "modulated":
            return
        n           = self._sp_n.value()
        off_hz      = self._sp_offset.value()
        offset_word = hz_to_phase(off_hz)
        actual_hz   = phase_to_hz(offset_word)
        self._be.apply(n, offset_word)
        self._log(
            f"Apply  N={n}  shift={actual_hz:+.6f} Hz  phase_offset={offset_word:+d}"
        )

    def _do_soft_reset(self):
        if not self._live:
            return
        self._refresh_input_pending = True
        self._be.soft_reset()

    def _do_upload(self):
        here    = Path(__file__).resolve().parent
        c_src   = os.fspath(here / "rp_harmonic_ctl.c")
        bit_src = os.fspath(here / "red_pitaya_top.bit.bin")
        if not Path(c_src).exists():
            self._log(f"ERROR: source file not found: {Path(c_src).as_posix()}")
            return
        if not self._live:
            self._log("Not connected — queuing upload for after connect …")
            host = self._w_host.text().strip()
            port = int(self._w_port.text().strip() or "22")
            user = self._w_user.text().strip() or "root"
            key  = self._w_key.text().strip() or None
            self._be.upload(c_src, bit_src if Path(bit_src).exists() else None)
            self._be.start_connect(host, port, user, key, DEFAULT_BASE)
        else:
            self._be.upload(c_src, bit_src if Path(bit_src).exists() else None)

    # ── Status update from hardware ───────────────────────────────────────────

    @Slot(dict)
    def _on_status(self, d: dict):
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

        # Sync output mode indicator from FPGA control register
        ctrl = int(d.get("control") or 0)
        if (ctrl >> 2) & 1:
            fpga_mode = "on"
        elif ctrl & 1:
            fpga_mode = "modulated"
        else:
            fpga_mode = "off"
        if fpga_mode != self._output_mode:
            self._output_mode = fpga_mode
            self._update_mode_styles()
            self._update_mode_controls()

        period = int(d.get("period_avg") or d.get("period") or 0)
        stable = bool(d.get("period_stable"))
        mult_n = int(d.get("mult_n") or 1)

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
                self._log(f"Input measured: {fmt_freq(in_hz)} ({'stable' if stable else 'acquiring'})")
            else:
                self._d_in.set_data("---", "no input signal", _RED)

        if period > 0:
            step_off  = int(d.get("phase_step_offset") or 0)
            step_live = int(d.get("phase_step") or 0)
            out_hz    = phase_to_hz(step_live)
            delta     = phase_to_hz(step_off)

            self._d_out.set_data(fmt_freq(out_hz), f"shift {fmt_signed_freq(delta)}")
            self._d_n.set_data(str(mult_n), "harmonic order")
            self._d_status.set_data(
                "LOCKED" if stable else "ACQUIRING",
                "freerun active" if d.get("freerun_active") else "measuring",
                _GREEN if stable else _AMBER,
            )
            self._update_shift_detail()
            self._poll.setInterval(400 if not stable else 800)
        else:
            self._d_status.set_data("NO INPUT", "waiting for signal", _RED)
            self._poll.setInterval(400)

    # ── Error / log ───────────────────────────────────────────────────────────

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
    app.setApplicationName("RP Harmonic Generator")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
