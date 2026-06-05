#!/usr/bin/env python3
"""
redpitaya_combined_gui_qt.py — Unified PySide6 GUI for Red Pitaya.

Two modes in a single window, sharing one SSH session:
  • Pulse / Freq-Shift  tab: f_out = f_in + f_shift, variable duty cycle
  • Harmonic Generator  tab: f_out = N × f_in + f_shift, 50% duty cycle

Both modes are supported by a single unified FPGA bitfile. Switching between
modes is instant — the C helper for the active tab sets the harmonic_mode bit.
Click "Upload && Compile" on either tab to flash the bitfile if needed.

Assets are read from the same directory as this script:
  rp_ctl.c                — unified board-side C helper (compiled as both rp_pulse_ctl and rp_harmonic_ctl)
  red_pitaya_top.bit.bin  — unified FPGA bitfile
  Vivado files/           — RTL source files

Run with:  python redpitaya_combined_gui_qt.py
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
        QSizePolicy, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
    )
except ImportError as exc:
    raise SystemExit(
        "PySide6 is required.\n  python -m pip install PySide6-Essentials"
    ) from exc

try:
    import paramiko
    _HAS_PARAMIKO = True
except ImportError:
    _HAS_PARAMIKO = False

# Hardware constants and pure conversion math live in a Qt-free module so they
# can be unit-tested in isolation (see tests/test_rp_math.py).
from rp_math import (  # noqa: E402
    CLK_HZ, PHASE_BITS, DEFAULT_BASE, CTRL_ENABLE, CTRL_FORCE_HIGH,
    PHASE_RES_HZ, MAX_SHIFT_HZ, WINDOW_OPTIONS_US, WINDOW_NAMES,
    hz_to_phase, phase_to_hz, duty_to_cycles, fmt_freq, fmt_signed_freq,
    suggest_window, trig_hz_to_half_period, fmt_dur,
)


# ─────────────────────────────────────────────────────────────────────────────
# SSH Backend — single session, mode-aware
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
    Single persistent paramiko SSH session shared by both panels.

    self._mode ("pulse" | "harmonic") tracks which helper binary is called for
    polls and generic operations. Each tab's apply/control calls use the
    mode-specific helper directly, which enforces the harmonic_mode bit.

    Priority queue:
      P_USER (0)   – register writes / window changes
      P_UPLOAD (1) – C source upload, compile, bitfile flash
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
    sig_mode_changed = Signal(str)   # "pulse" | "harmonic"

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._ssh:  Optional[paramiko.SSHClient]  = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._live  = False
        self._base  = DEFAULT_BASE
        self._mode  = "pulse"
        self._q: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._upload_pending: Optional[tuple] = None   # (mode, c_src, bit_src)
        self._thread = threading.Thread(target=self._loop, name="rp-ssh", daemon=True)
        self._thread.start()

    # ── public API ─────────────────────────────────────────────────────────────

    def start_connect(self, host: str, port: int, user: str,
                      key: Optional[str], base: int):
        self._base = base
        self._enqueue(self.P_INIT, lambda: self._do_connect(host, port, user, key))

    def start_disconnect(self):
        self._enqueue(self.P_INIT, self._do_disconnect)

    def poll(self):
        if self._live:
            self._enqueue(self.P_POLL, self._do_read, self.sig_status.emit)

    def apply_pulse(self, width_cycles: int, offset_word: int):
        """Pulse-mode modulated write (enable=1, harmonic_mode=0 via helper)."""
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_write_pulse(width_cycles, offset_word),
                          self.sig_status.emit)

    def apply_harmonic(self, mult_n: int, offset_word: int):
        """Harmonic-mode modulated write (enable=1, harmonic_mode=1 via helper)."""
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_write_harmonic(mult_n, offset_word),
                          self.sig_status.emit)

    def set_control_pulse(self, ctrl: int):
        """Set control register via the pulse helper (keeps harmonic_mode=0)."""
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_set_control_pulse(ctrl),
                          self.sig_status.emit)

    def set_control_harmonic(self, ctrl: int):
        """Set control register via the harmonic helper (keeps harmonic_mode=1)."""
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_set_control_harmonic(ctrl),
                          self.sig_status.emit)

    def set_trig(self, half_period: int):
        """Write trig_half_period register (DIO2 square wave). half_period=0 disables."""
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_set_trig(half_period),
                          self.sig_status.emit)

    def set_window(self, window_us: int):
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_window(window_us),
                          self.sig_status.emit)

    def soft_reset(self):
        if self._live:
            self._enqueue(self.P_USER, self._do_reset, self.sig_status.emit)

    def upload_pulse(self, c_src: str, bit_src: Optional[str]):
        if self._live:
            self._enqueue(self.P_UPLOAD, lambda: self._do_upload_pulse(c_src, bit_src))
        else:
            self._upload_pending = ("pulse", c_src, bit_src)

    def upload_harmonic(self, c_src: str, bit_src: Optional[str]):
        if self._live:
            self._enqueue(self.P_UPLOAD, lambda: self._do_upload_harmonic(c_src, bit_src))
        else:
            self._upload_pending = ("harmonic", c_src, bit_src)

    def set_active_mode(self, mode: str):
        """Switch poll target between pulse and harmonic helper."""
        if mode in ("pulse", "harmonic"):
            self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def live(self) -> bool:
        return self._live

    # ── internal ───────────────────────────────────────────────────────────────

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
                # A single failed poll (helper not compiled yet, transient
                # non-JSON output, momentary hiccup) must not tear down the
                # whole session — log it and keep the connection alive.
                if job.pri == self.P_POLL and self._live:
                    self.sig_log.emit(f"[poll skipped] {exc}")
                    continue
                self._live = False
                self.sig_error.emit(str(exc))
                self.sig_disconnected.emit(str(exc))

    def _exec(self, cmd: str, timeout: float = 10.0) -> str:
        _, stdout, stderr = self._ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode()
        err = stderr.read().decode().strip()
        status = stdout.channel.recv_exit_status()
        if err:
            self.sig_log.emit(f"[stderr] {err}")
        if status != 0:
            self.sig_log.emit(f"[exit {status}] {cmd}")
        return out

    def _active_cmd(self) -> str:
        binary = "rp_pulse_ctl" if self._mode == "pulse" else "rp_harmonic_ctl"
        return f"/root/{binary} 0x{self._base:08X}"

    # ── SSH operations (worker thread) ─────────────────────────────────────────

    def _do_connect(self, host: str, port: int, user: str, key: Optional[str]):
        self.sig_log.emit(f"Connecting to {user}@{host}:{port} …")
        for obj in (self._sftp, self._ssh):
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        client = paramiko.SSHClient()
        # AutoAddPolicy trusts any unknown host key on first contact. This is a
        # deliberate convenience for a directly-cabled lab instrument (Red Pitaya
        # boards have per-unit keys and are reached over a trusted local link);
        # it does NOT authenticate the host, so do not use this over untrusted
        # networks. Swap in RejectPolicy + known_hosts for a hardened deployment.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw: dict = dict(hostname=host, port=port, username=user,
                        timeout=12, banner_timeout=20, auth_timeout=12)
        if key:
            kw["key_filename"] = key
        client.connect(**kw)
        self._ssh  = client
        self._sftp = client.open_sftp()
        self._live = True
        self.sig_log.emit("SSH connected.")
        if self._upload_pending:
            mode, c_src, bit_src = self._upload_pending
            self._upload_pending = None
            self.sig_log.emit("Starting pending upload…")
            if mode == "pulse":
                self._do_upload_pulse(c_src, bit_src)
            else:
                self._do_upload_harmonic(c_src, bit_src)
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
        return json.loads(self._exec(f"{self._active_cmd()} read"))

    def _do_write_pulse(self, width: int, offset: int) -> dict:
        return json.loads(self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} write {width} {offset} {CTRL_ENABLE}"
        ))

    def _do_write_harmonic(self, mult_n: int, offset: int) -> dict:
        return json.loads(self._exec(
            f"/root/rp_harmonic_ctl 0x{self._base:08X} write {mult_n} {offset} {CTRL_ENABLE}"
        ))

    def _do_set_control_pulse(self, ctrl: int) -> dict:
        return json.loads(self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} control {ctrl}"
        ))

    def _do_set_control_harmonic(self, ctrl: int) -> dict:
        return json.loads(self._exec(
            f"/root/rp_harmonic_ctl 0x{self._base:08X} control {ctrl}"
        ))

    def _do_reset(self) -> dict:
        return json.loads(self._exec(f"{self._active_cmd()} soft_reset"))

    def _do_window(self, meas_us: int) -> dict:
        return json.loads(self._exec(f"{self._active_cmd()} window {meas_us}"))

    def _do_set_trig(self, half_period: int) -> dict:
        return json.loads(self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} trig {half_period}"
        ))

    def _do_upload_pulse(self, c_src: str, bit_src: Optional[str]):
        self.sig_log.emit(f"[Pulse] Uploading {Path(c_src).name} …")
        self._sftp.put(c_src, "/root/rp_ctl.c")
        self.sig_log.emit("[Pulse] Compiling on board …")
        self._exec(
            "gcc -O2 -o /root/rp_ctl /root/rp_ctl.c && "
            "ln -sf /root/rp_ctl /root/rp_pulse_ctl && "
            "ln -sf /root/rp_ctl /root/rp_harmonic_ctl",
            timeout=60,
        )
        self.sig_log.emit("[Pulse] Compiled OK.")
        if bit_src and Path(bit_src).exists():
            self.sig_log.emit("[Pulse] Uploading FPGA bitfile …")
            self._sftp.put(bit_src, "/root/red_pitaya_top.bit.bin")
            self._exec(
                "/opt/redpitaya/bin/fpgautil -b /root/red_pitaya_top.bit.bin",
                timeout=30,
            )
            self.sig_log.emit("[Pulse] FPGA loaded.")
        self._mode = "pulse"
        self.sig_mode_changed.emit("pulse")

    def _do_upload_harmonic(self, c_src: str, bit_src: Optional[str]):
        self.sig_log.emit(f"[Harmonic] Uploading {Path(c_src).name} …")
        self._sftp.put(c_src, "/root/rp_ctl.c")
        self.sig_log.emit("[Harmonic] Compiling on board …")
        self._exec(
            "gcc -O2 -o /root/rp_ctl /root/rp_ctl.c && "
            "ln -sf /root/rp_ctl /root/rp_pulse_ctl && "
            "ln -sf /root/rp_ctl /root/rp_harmonic_ctl",
            timeout=60,
        )
        self.sig_log.emit("[Harmonic] Compiled OK.")
        if bit_src and Path(bit_src).exists():
            self.sig_log.emit("[Harmonic] Uploading FPGA bitfile …")
            self._sftp.put(bit_src, "/root/red_pitaya_top.bit.bin")
            self._exec(
                "/opt/redpitaya/bin/fpgautil -b /root/red_pitaya_top.bit.bin",
                timeout=30,
            )
            self.sig_log.emit("[Harmonic] FPGA loaded.")
        self._mode = "harmonic"
        self.sig_mode_changed.emit("harmonic")


# ─────────────────────────────────────────────────────────────────────────────
# Colour palette & style helpers
# ─────────────────────────────────────────────────────────────────────────────
_BG      = "#0d1117"
_SURFACE = "#101722"
_ACCENT  = "#00d4ff"
_GREEN   = "#3fb950"
_AMBER   = "#d29922"
_RED     = "#f85149"
_WHITE   = "#e6edf3"
_TEXT    = "#e6edf3"
_DIM     = "#8b949e"
_BORDER  = "#263241"

# Spacing scale (px) — used for consistent rhythm throughout
_SP_XS  = 4
_SP_SM  = 8
_SP_MD  = 12
_SP_LG  = 16
_SP_XL  = 20
_SP_2XL = 28
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
            color: {_ACCENT}; border: 1px solid {_BORDER};
            border-radius: 10px; margin-top: 18px;
            padding: 16px 14px 14px 14px;
            font-family: {_MONO}; font-size: 10px; font-weight: bold;
        }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 16px; padding: 0 6px; }}
    """


def _btn_style(color: str = _ACCENT) -> str:
    return f"""
        QPushButton {{
            background: #111923; color: {color};
            border: 1px solid {color}; border-radius: 7px;
            padding: 7px 14px;
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
                padding: 7px 18px;
                font-family: {_MONO}; font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {color}40; }}
        """
    return f"""
        QPushButton {{
            background: #111923; color: {_DIM};
            border: 1px solid {_BORDER}; border-radius: 7px;
            padding: 7px 18px;
            font-family: {_MONO}; font-size: 11px;
        }}
        QPushButton:hover {{ background: #182536; color: {color}; border-color: {color}; }}
    """


def _le_style() -> str:
    return f"""
        QLineEdit {{
            background: {_BG}; color: {_TEXT};
            border: 1px solid {_BORDER}; border-radius: 4px;
            padding: 3px 6px; font-family: {_MONO}; font-size: 10px;
        }}
        QLineEdit:focus {{ border-color: {_ACCENT}; }}
    """


def _spin_style() -> str:
    return f"""
        QDoubleSpinBox, QSpinBox, QComboBox {{
            background: #0b111a; color: {_TEXT};
            border: 1px solid {_BORDER}; border-radius: 7px;
            padding: 5px 8px; font-family: {_MONO}; font-size: 10px;
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


# ── Shared widget helpers ──────────────────────────────────────────────────────

def _make_group(title: str) -> QGroupBox:
    g = QGroupBox(title)
    g.setStyleSheet(_group_style())
    return g


def _dim_label(text: str, width: int = 104) -> QLabel:
    lbl = QLabel(text)
    lbl.setFixedWidth(width)
    lbl.setFont(_mono_font(10))
    lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
    return lbl


class BigDisplay(QFrame):
    """Large labelled readout tile."""

    def __init__(self, title: str, sub_hint: str = "",
                 accent: str = _ACCENT, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._accent = accent
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE}; border: 1px solid {_BORDER}; border-radius: 14px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 20)
        lay.setSpacing(4)

        title_lbl = QLabel(title.upper())
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(_mono_font(9, bold=True))
        title_lbl.setStyleSheet(f"color: {_DIM}; background: transparent; border: none;")
        lay.addWidget(title_lbl)

        rule = QLabel()
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {accent}50; border: none; border-radius: 0;")
        lay.addWidget(rule)

        lay.addSpacing(_SP_XS)

        self._val = QLabel("---")
        self._val.setAlignment(Qt.AlignCenter)
        self._val.setFont(_mono_font(32, bold=True))
        self._val.setStyleSheet(f"color: {_DIM}; background: transparent; border: none;")
        self._val.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self._val, 1)

        lay.addSpacing(_SP_XS)

        self._sub = QLabel(sub_hint)
        self._sub.setAlignment(Qt.AlignCenter)
        self._sub.setFont(_mono_font(11))
        self._sub.setStyleSheet(f"color: {_DIM}; background: transparent; border: none;")
        lay.addWidget(self._sub)

    def set_data(self, value: str, sub: str = "", color: Optional[str] = None):
        self._val.setText(value)
        resolved = color or (self._accent if value != "---" else _DIM)
        self._val.setStyleSheet(
            f"color: {resolved}; background: transparent; border: none;"
        )
        if sub is not None:
            self._sub.setText(sub)


# ─────────────────────────────────────────────────────────────────────────────
# _NcoPanel — shared base for the Pulse and Harmonic tabs
# ─────────────────────────────────────────────────────────────────────────────

class _NcoPanel(QWidget):
    """
    Shared UI and logic for the two NCO control tabs.

    Both tabs poll the same status dict, share the 2×2 monitor grid (left
    column), the output-mode bar, the freq-shift/window/auto-apply controls and
    the action column. Subclasses define ``MODE`` and a handful of hooks for the
    parts that genuinely differ:

      _out_hint()                 sub-text under the Output Frequency tile
      _make_right_tiles()         the two mode-specific monitor tiles (returns top, bottom)
      _build_secondary_field()    the row-0 col-2/3 input (Width % or Harmonic N); returns the widget
      _be_set_control(ctrl)       route a control write to the mode's helper
      _be_upload(c_src, bit_src)  route an upload to the mode's helper
      _update_shift_detail()      mode-specific "target output" detail line
      _do_apply()                 mode-specific modulated write
      _update_status_tiles(...)   refresh the right tiles from a status dict
      _update_status_noinput()    right tiles when no input signal is present
      _update_local_displays()    right tiles from local control values (optional)
      _on_disconnected_extra()    extra teardown (optional)
    """

    MODE: str = "pulse"   # "pulse" | "harmonic" — overridden by subclasses

    def __init__(self, backend: SshBackend, log_fn: Callable[[str], None],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._be   = backend
        self._log_fn = log_fn
        self._period_c  = 0
        self._live      = False
        self._refresh_pending = False
        self._output_mode: str = "modulated"   # "off" | "modulated" | "on"
        self._tag = self.MODE.capitalize()
        self._harmonic_json = 1 if self.MODE == "harmonic" else 0

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._do_apply)

        self._build_ui()

        backend.sig_connected.connect(self._on_connected)
        backend.sig_disconnected.connect(self._on_disconnected)
        backend.sig_status.connect(self._on_status)
        backend.sig_mode_changed.connect(self._on_mode_changed)

    def _log(self, msg: str):
        self._log_fn(f"[{self._tag}] {msg}")

    # ── mode-specific hooks (defaults are no-ops where sensible) ────────────────

    def _out_hint(self) -> str:
        return "NCO output"

    def _make_right_tiles(self) -> tuple:
        raise NotImplementedError

    def _build_secondary_field(self, fields: QGridLayout) -> QWidget:
        raise NotImplementedError

    def _be_set_control(self, ctrl: int):
        raise NotImplementedError

    def _be_upload(self, c_src: str, bit_src: Optional[str]):
        raise NotImplementedError

    def _do_apply(self):
        raise NotImplementedError

    def _update_shift_detail(self):
        raise NotImplementedError

    def _update_status_tiles(self, d: dict, step_base: int, stable: bool):
        pass

    def _update_status_noinput(self):
        pass

    def _update_local_displays(self):
        self._update_shift_detail()

    def _on_disconnected_extra(self):
        pass

    def _warn_text(self, active_mode: str) -> str:
        return (f"  Active helper: '{active_mode}' mode  —  "
                f"click 'Upload && Compile ({self._tag} mode)' to switch")

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, _SP_SM, 0, 0)
        root.setSpacing(_SP_MD)

        # Mode mismatch warning (shown when the active helper != this panel's mode)
        self._lbl_warn = QLabel()
        self._lbl_warn.setFont(_mono_font(9))
        self._lbl_warn.setAlignment(Qt.AlignCenter)
        self._lbl_warn.setStyleSheet(
            f"background: #2a1500; color: {_AMBER}; "
            f"border: 1px solid {_AMBER}; border-radius: 6px; padding: 6px 8px;"
        )
        if self._be.mode != self.MODE:
            self._lbl_warn.setText(self._warn_text(self._be.mode))
            self._lbl_warn.setVisible(True)
        else:
            self._lbl_warn.setVisible(False)
        root.addWidget(self._lbl_warn)

        # Monitors 2×2 — left column shared, right column mode-specific
        self._d_in  = BigDisplay("Input Frequency",  "measured input period", _ACCENT)
        self._d_out = BigDisplay("Output Frequency", self._out_hint(),        _GREEN)
        self._d_tr, self._d_br = self._make_right_tiles()

        for d in (self._d_in, self._d_tr, self._d_out, self._d_br):
            d.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            d.setMinimumHeight(155)

        grid = QGridLayout()
        grid.setSpacing(_SP_MD)
        grid.addWidget(self._d_in,  0, 0)
        grid.addWidget(self._d_tr,  0, 1)
        grid.addWidget(self._d_out, 1, 0)
        grid.addWidget(self._d_br,  1, 1)
        for i in range(2):
            grid.setRowStretch(i, 1)
            grid.setColumnStretch(i, 1)
        root.addLayout(grid, 1)

        # Controls group
        controls = _make_group("Controls")
        cl = QHBoxLayout(controls)
        cl.setContentsMargins(_SP_LG, 14, _SP_LG, _SP_LG)
        cl.setSpacing(_SP_XL)

        left_col = QVBoxLayout()
        left_col.setSpacing(14)

        # ── Output mode bar ───────────────────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(_SP_MD)
        mode_lbl = QLabel("Output mode:")
        mode_lbl.setFont(_mono_font(10, bold=True))
        mode_lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        mode_row.addWidget(mode_lbl)
        self._btn_off = QPushButton("■  LASER OFF")
        self._btn_mod = QPushButton("~  MODULATED")
        self._btn_on  = QPushButton("●  LASER ON")
        for btn in (self._btn_off, self._btn_mod, self._btn_on):
            btn.setFixedHeight(38)
            btn.setFont(_mono_font(11))
            mode_row.addWidget(btn)
        mode_row.addStretch()
        self._btn_off.clicked.connect(lambda: self._set_output_mode("off"))
        self._btn_mod.clicked.connect(lambda: self._set_output_mode("modulated"))
        self._btn_on.clicked.connect(lambda: self._set_output_mode("on"))
        left_col.addLayout(mode_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        left_col.addWidget(sep)

        fields = QGridLayout()
        fields.setHorizontalSpacing(14)
        fields.setVerticalSpacing(14)

        # Row 0: freq shift (shared) + mode-specific secondary field
        fields.addWidget(_dim_label("Freq shift:"), 0, 0)
        self._sp_offset = QDoubleSpinBox()
        self._sp_offset.setRange(-MAX_SHIFT_HZ, MAX_SHIFT_HZ)
        self._sp_offset.setDecimals(6)
        self._sp_offset.setSingleStep(1.0)
        self._sp_offset.setSuffix(" Hz")
        self._sp_offset.setFixedHeight(46)
        self._sp_offset.setMinimumWidth(280)
        self._sp_offset.setFont(_mono_font(15, bold=True))
        self._sp_offset.setStyleSheet(_spin_style())
        self._sp_offset.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_offset, 0, 1)

        self._secondary_widget = self._build_secondary_field(fields)

        # Row 1: measurement window
        fields.addWidget(_dim_label("Meas. window:"), 1, 0)
        self._cb_window = QComboBox()
        self._cb_window.addItems(WINDOW_NAMES)
        self._cb_window.setCurrentIndex(2)
        self._cb_window.setFixedHeight(36)
        self._cb_window.setFixedWidth(118)
        self._cb_window.setStyleSheet(_spin_style())
        self._cb_window.currentIndexChanged.connect(self._on_window_changed)
        fields.addWidget(self._cb_window, 1, 1)

        self._lbl_win_suggest = QLabel()
        self._lbl_win_suggest.setFont(_mono_font(9))
        self._lbl_win_suggest.setStyleSheet(f"color: {_AMBER}; background: transparent;")
        fields.addWidget(self._lbl_win_suggest, 1, 2, 1, 2)

        # Row 2: auto-apply
        auto_row = QHBoxLayout()
        self._cb_auto = QCheckBox("Auto-Apply")
        self._cb_auto.setChecked(True)
        self._cb_auto.setFont(_mono_font(10))
        self._cb_auto.setStyleSheet(f"color: {_TEXT}; background: transparent;")
        auto_row.addWidget(self._cb_auto)
        auto_row.addStretch()
        fields.addLayout(auto_row, 2, 0, 1, 4)

        # Row 3: shift detail
        self._lbl_shift = QLabel()
        self._lbl_shift.setFont(_mono_font(9))
        self._lbl_shift.setWordWrap(True)
        self._lbl_shift.setStyleSheet(f"color: {_DIM}; background: transparent;")
        fields.addWidget(self._lbl_shift, 3, 0, 1, 4)
        fields.setColumnStretch(1, 1)

        left_col.addLayout(fields)
        cl.addLayout(left_col, 1)

        # Action column
        actions = QVBoxLayout()
        actions.setSpacing(10)

        self._btn_apply = QPushButton("Apply Now\nCtrl+↵")
        self._btn_apply.setFixedWidth(220)
        self._btn_apply.setFixedHeight(86)
        self._btn_apply.setFont(_mono_font(13, bold=True))
        self._btn_apply.setStyleSheet(_btn_style(_GREEN))
        self._btn_apply.clicked.connect(self._do_apply)
        actions.addWidget(self._btn_apply)

        self._btn_reset = QPushButton("Soft Reset")
        self._btn_reset.setFixedWidth(220)
        self._btn_reset.setFixedHeight(38)
        self._btn_reset.setStyleSheet(_btn_style(_AMBER))
        self._btn_reset.clicked.connect(self._do_soft_reset)
        actions.addWidget(self._btn_reset)

        self._btn_upload = QPushButton(f"Upload && Compile\n({self._tag} mode)")
        self._btn_upload.setFixedWidth(220)
        self._btn_upload.setFixedHeight(50)
        self._btn_upload.setStyleSheet(_btn_style(_ACCENT))
        self._btn_upload.clicked.connect(self._do_upload)
        actions.addWidget(self._btn_upload)
        actions.addStretch()
        cl.addLayout(actions)

        root.addWidget(controls)
        self._update_mode_styles()
        self._update_shift_detail()
        self._update_window_suggestion()

    # ── output mode ────────────────────────────────────────────────────────────

    def _set_output_mode(self, mode: str):
        self._output_mode = mode
        self._update_mode_styles()
        self._update_mode_controls()
        if self._live:
            if mode == "off":
                self._be_set_control(0x00)
                self._log("Laser OFF  (output = constant 0)")
            elif mode == "on":
                self._be_set_control(CTRL_FORCE_HIGH)
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
        for w in (self._sp_offset, self._secondary_widget, self._cb_window,
                  self._cb_auto, self._btn_apply):
            w.setEnabled(enabled)

    # ── backend signal handlers ────────────────────────────────────────────────

    @Slot()
    def _on_connected(self):
        self._live = True
        if self._be.mode == self.MODE:
            self._be.set_window(WINDOW_OPTIONS_US[self._cb_window.currentIndex()])

    @Slot(str)
    def _on_disconnected(self, _reason: str):
        self._live = False
        self._refresh_pending = False
        self._period_c = 0
        self._on_disconnected_extra()

    @Slot(str)
    def _on_mode_changed(self, mode: str):
        if mode == self.MODE:
            self._lbl_warn.setVisible(False)
            if self._live:
                self._be.set_window(WINDOW_OPTIONS_US[self._cb_window.currentIndex()])
        else:
            self._lbl_warn.setText(self._warn_text(mode))
            self._lbl_warn.setVisible(True)

    @Slot(dict)
    def _on_status(self, d: dict):
        if int(d.get("harmonic_mode", 0)) != self._harmonic_json:
            return  # JSON from the other helper; skip

        raw_us = d.get("meas_time_us")
        if raw_us is not None:
            us_val = int(raw_us)
            try:
                idx = WINDOW_OPTIONS_US.index(us_val)
            except ValueError:
                idx = min(range(len(WINDOW_OPTIONS_US)),
                          key=lambda i: abs(WINDOW_OPTIONS_US[i] - us_val))
            if idx != self._cb_window.currentIndex():
                self._cb_window.blockSignals(True)
                self._cb_window.setCurrentIndex(idx)
                self._cb_window.blockSignals(False)

        # Sync output mode from FPGA control register
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

        stable    = bool(d.get("period_stable"))
        step_base = int(d.get("phase_step_base") or 0)

        if self._refresh_pending:
            if step_base > 0:
                # Derive period in clock cycles from phase_step_base (avoids integer truncation)
                self._period_c = (1 << PHASE_BITS) // step_base
                in_hz = phase_to_hz(step_base)
                self._d_in.set_data(
                    fmt_freq(in_hz),
                    "stable" if stable else "acquiring …",
                    _ACCENT if stable else _AMBER,
                )
                self._refresh_pending = False
                self._log(f"Input: {fmt_freq(in_hz)} ({'stable' if stable else 'acquiring'})")
            else:
                self._d_in.set_data("---", "no input signal", _RED)

        if step_base > 0:
            step_live = int(d.get("phase_step") or step_base)
            step_off  = int(d.get("phase_step_offset") or (step_live - step_base))
            out_hz    = phase_to_hz(step_live)
            delta     = phase_to_hz(step_off)
            self._d_out.set_data(fmt_freq(out_hz), f"shift {fmt_signed_freq(delta)}")
            self._update_shift_detail()
            self._update_status_tiles(d, step_base, stable)
        else:
            self._update_status_noinput()

    # ── parameter controls ─────────────────────────────────────────────────────

    def _param_changed(self, *_):
        if self._output_mode == "modulated" and self._cb_auto.isChecked():
            self._debounce.start()
        self._update_local_displays()
        self._update_window_suggestion()

    def _on_window_changed(self, idx: int):
        if self._live and self._be.mode == self.MODE:
            self._be.set_window(WINDOW_OPTIONS_US[idx])
        self._update_window_suggestion()

    def _update_window_suggestion(self):
        f_shift  = abs(self._sp_offset.value())
        sug      = suggest_window(f_shift)
        current  = self._cb_window.currentIndex()
        freq_str = fmt_freq(f_shift) if f_shift > 0 else "---"
        if current == sug:
            self._lbl_win_suggest.setText(f"✓ optimal for {freq_str}")
            self._lbl_win_suggest.setStyleSheet(f"color: {_GREEN}; background: transparent;")
        else:
            self._lbl_win_suggest.setText(f"suggested: {WINDOW_NAMES[sug]} for {freq_str}")
            self._lbl_win_suggest.setStyleSheet(f"color: {_AMBER}; background: transparent;")

    # ── actions ────────────────────────────────────────────────────────────────

    def apply(self):
        """Public entry point (e.g. the Ctrl+Return shortcut) → mode-specific write."""
        self._do_apply()

    def _do_soft_reset(self):
        if not self._live:
            return
        self._refresh_pending = True
        self._be.soft_reset()
        self._log("Soft reset sent.")

    def _do_upload(self):
        here    = Path(__file__).resolve().parent
        c_src   = os.fspath(here / "rp_ctl.c")
        bit_src = os.fspath(here / "red_pitaya_top.bit.bin")
        if not Path(c_src).exists():
            self._log(f"ERROR: {c_src} not found")
            return
        self._be_upload(c_src, bit_src if Path(bit_src).exists() else None)
        self._log("Upload queued.")


# ─────────────────────────────────────────────────────────────────────────────
# PulsePanel — f_out = f_in + f_shift, variable duty cycle
# ─────────────────────────────────────────────────────────────────────────────

class PulsePanel(_NcoPanel):
    """Pulse / frequency-shift mode (harmonic_mode=0): f_out = f_in + f_shift."""

    MODE = "pulse"

    def _out_hint(self) -> str:
        return "NCO output"

    def _make_right_tiles(self) -> tuple:
        self._d_dur = BigDisplay("Pulse Duration", "pulse high-time", _AMBER)
        self._d_dut = BigDisplay("Duty Cycle",     "width / period",  _AMBER)
        return self._d_dur, self._d_dut

    def _build_secondary_field(self, fields: QGridLayout) -> QWidget:
        fields.addWidget(_dim_label("Width:"), 0, 2)
        self._sp_width = QDoubleSpinBox()
        self._sp_width.setRange(0.1, 99.9)
        self._sp_width.setDecimals(2)
        self._sp_width.setSuffix(" %")
        self._sp_width.setValue(50.0)
        self._sp_width.setFixedHeight(46)
        self._sp_width.setMinimumWidth(140)
        self._sp_width.setFont(_mono_font(15, bold=True))
        self._sp_width.setStyleSheet(_spin_style())
        self._sp_width.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_width, 0, 3)
        return self._sp_width

    def _be_set_control(self, ctrl: int):
        self._be.set_control_pulse(ctrl)

    def _be_upload(self, c_src: str, bit_src: Optional[str]):
        self._be.upload_pulse(c_src, bit_src)

    def _update_local_displays(self):
        self._update_shift_detail()
        if self._period_c <= 0:
            return
        frac = self._sp_width.value() / 100.0
        wc   = duty_to_cycles(frac, self._period_c)
        self._d_dur.set_data(fmt_dur(wc / CLK_HZ))
        self._d_dut.set_data(f"{self._sp_width.value():.2f} %")

    def _update_status_tiles(self, d: dict, step_base: int, stable: bool):
        period = self._period_c if self._period_c > 0 else ((1 << PHASE_BITS) // step_base)
        wc = int(d.get("width") or 0)
        if wc > 0:
            self._d_dur.set_data(fmt_dur(wc / CLK_HZ))
            self._d_dut.set_data(f"{wc / period * 100:.2f} %")

    def _update_shift_detail(self):
        req_hz      = self._sp_offset.value()
        offset_word = hz_to_phase(req_hz)
        actual_hz   = phase_to_hz(offset_word)
        out_text    = ""
        if self._period_c > 0:
            output_hz = CLK_HZ / self._period_c + actual_hz
            if output_hz > 0:
                out_text = f", target output {fmt_freq(output_hz)}"
        self._lbl_shift.setText(
            f"shift: requested {req_hz:+.6f} Hz, actual {actual_hz:+.6f} Hz, "
            f"register {offset_word:+d}, resolution {PHASE_RES_HZ:.9f} Hz/LSB{out_text}"
        )

    def _do_apply(self):
        if not self._live or self._output_mode != "modulated":
            return
        frac        = self._sp_width.value() / 100.0
        off_hz      = self._sp_offset.value()
        period      = self._period_c if self._period_c > 0 else 1000
        wc          = duty_to_cycles(frac, period)
        offset_word = hz_to_phase(off_hz)
        actual_hz   = phase_to_hz(offset_word)
        self._be.apply_pulse(wc, offset_word)
        self._log(
            f"Apply  width={self._sp_width.value():.2f}%  "
            f"shift={actual_hz:+.6f} Hz  offset={offset_word:+d}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# HarmonicPanel — f_out = N × f_in + f_shift, 50% duty cycle
# ─────────────────────────────────────────────────────────────────────────────

class HarmonicPanel(_NcoPanel):
    """Harmonic generator mode (harmonic_mode=1): f_out = N × f_in + f_shift."""

    MODE = "harmonic"

    def _out_hint(self) -> str:
        return "N·f_in + f_shift"

    def _make_right_tiles(self) -> tuple:
        self._d_n      = BigDisplay("Harmonic N", "applied multiplier", _AMBER)
        self._d_status = BigDisplay("NCO Status", "lock / acquiring",   _AMBER)
        return self._d_n, self._d_status

    def _build_secondary_field(self, fields: QGridLayout) -> QWidget:
        fields.addWidget(_dim_label("Harmonic N:"), 0, 2)
        self._sp_n = QSpinBox()
        self._sp_n.setRange(1, 5)
        self._sp_n.setValue(1)
        self._sp_n.setFixedHeight(46)
        self._sp_n.setMinimumWidth(100)
        self._sp_n.setFont(_mono_font(15, bold=True))
        self._sp_n.setStyleSheet(_spin_style())
        self._sp_n.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_n, 0, 3)
        return self._sp_n

    def _be_set_control(self, ctrl: int):
        self._be.set_control_harmonic(ctrl)

    def _be_upload(self, c_src: str, bit_src: Optional[str]):
        self._be.upload_harmonic(c_src, bit_src)

    def _on_disconnected_extra(self):
        self._d_status.set_data("---", "", _AMBER)

    def _update_status_tiles(self, d: dict, step_base: int, stable: bool):
        mult_n = int(d.get("mult_n") or 1)
        self._d_n.set_data(str(mult_n), "harmonic order")
        self._d_status.set_data(
            "LOCKED" if stable else "ACQUIRING",
            "freerun active" if d.get("freerun_active") else "measuring",
            _GREEN if stable else _AMBER,
        )

    def _update_status_noinput(self):
        self._d_status.set_data("NO INPUT", "waiting for signal", _RED)

    def _update_shift_detail(self):
        n           = self._sp_n.value()
        req_hz      = self._sp_offset.value()
        offset_word = hz_to_phase(req_hz)
        actual_hz   = phase_to_hz(offset_word)
        out_text    = ""
        if self._period_c > 0:
            output_hz = n * CLK_HZ / self._period_c + actual_hz
            if output_hz > 0:
                out_text = f", target output {fmt_freq(output_hz)}"
        self._lbl_shift.setText(
            f"N={n}  shift: requested {req_hz:+.6f} Hz, actual {actual_hz:+.6f} Hz, "
            f"register {offset_word:+d}, resolution {PHASE_RES_HZ:.9f} Hz/LSB{out_text}"
        )

    def _do_apply(self):
        if not self._live or self._output_mode != "modulated":
            return
        n           = self._sp_n.value()
        off_hz      = self._sp_offset.value()
        offset_word = hz_to_phase(off_hz)
        actual_hz   = phase_to_hz(offset_word)
        self._be.apply_harmonic(n, offset_word)
        self._log(
            f"Apply  N={n}  shift={actual_hz:+.6f} Hz  "
            f"offset={offset_word:+d}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# MainWindow — shared connection + tab host + shared log
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Red Pitaya — Combined Control")
        self.setMinimumSize(1000, 760)

        self._be = SshBackend(self)
        self._be.sig_connected.connect(self._on_connected)
        self._be.sig_disconnected.connect(self._on_disconnected)
        self._be.sig_log.connect(self._log)
        self._be.sig_error.connect(self._on_error)
        self._be.sig_mode_changed.connect(self._on_mode_changed)

        self._poll = QTimer(self)
        self._poll.setInterval(700)
        self._poll.timeout.connect(self._be.poll)
        # Match the poll cadence to the hardware measurement window so we don't
        # re-read the same registers far faster than they can update.
        self._be.sig_status.connect(self._adapt_poll_interval)

        self._build_ui()
        self.setStyleSheet(f"QMainWindow, QWidget {{ background: {_BG}; color: {_TEXT}; }}")

        act = QAction(self)
        act.setShortcut(QKeySequence("Ctrl+Return"))
        act.triggered.connect(self._active_panel_apply)
        self.addAction(act)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)
        root.setContentsMargins(_SP_XL, 14, _SP_XL, 14)
        root.setSpacing(10)

        root.addWidget(self._build_connection())

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {_BORDER}; border-radius: 8px;
                background: {_BG};
                padding: 8px;
            }}
            QTabBar::tab {{
                background: #111923; color: {_DIM};
                border: 1px solid {_BORDER}; border-bottom: none;
                border-radius: 6px 6px 0 0;
                padding: 8px 24px;
                font-family: {_MONO}; font-size: 11px;
            }}
            QTabBar::tab:selected {{ background: #182536; color: {_TEXT}; border-bottom: 1px solid {_BG}; }}
            QTabBar::tab:hover    {{ background: #14202e; color: {_TEXT}; }}
        """)

        self._pulse_panel    = PulsePanel(self._be, self._log)
        self._harmonic_panel = HarmonicPanel(self._be, self._log)

        self._tabs.addTab(self._pulse_panel,    "Pulse / Freq-Shift")
        self._tabs.addTab(self._harmonic_panel, "Harmonic Generator")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self._tabs, 1)

        root.addWidget(self._build_trigger())
        root.addWidget(self._build_log())

    def _build_connection(self) -> QGroupBox:
        g = _make_group("Connection")
        row = QHBoxLayout(g)
        row.setSpacing(10)

        self._w_host = QLineEdit("rp-f06a51.local")
        self._w_port = QLineEdit("22");    self._w_port.setFixedWidth(55)
        self._w_user = QLineEdit("root");  self._w_user.setFixedWidth(70)
        self._w_key  = QLineEdit();        self._w_key.setPlaceholderText("SSH key (optional)")

        btn_key = QPushButton("…"); btn_key.setFixedWidth(28)
        btn_key.clicked.connect(self._pick_key)

        self._btn_conn = QPushButton("Connect")
        self._btn_conn.setFixedWidth(95)
        self._btn_conn.clicked.connect(self._toggle_connect)

        self._lbl_status = QLabel("●  Disconnected")
        self._lbl_status.setFont(_mono_font(10))
        self._lbl_status.setStyleSheet(f"color: {_RED}; background: transparent;")

        self._lbl_mode = QLabel("mode: —")
        self._lbl_mode.setFont(_mono_font(9))
        self._lbl_mode.setStyleSheet(f"color: {_DIM}; background: transparent;")

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
        row.addWidget(self._lbl_mode)
        row.addStretch()

        for le in (self._w_host, self._w_port, self._w_user, self._w_key):
            le.setStyleSheet(_le_style())
        for b in (self._btn_conn, btn_key):
            b.setStyleSheet(_btn_style())
        return g

    def _build_trigger(self) -> QGroupBox:
        """DIO2 free-running square wave — independent of NCO mode."""
        g = _make_group("DIO2 Trigger Output  (independent free-running square wave)")
        row = QHBoxLayout(g)
        row.setContentsMargins(_SP_LG, 10, _SP_LG, 10)
        row.setSpacing(_SP_LG)

        lbl = QLabel("Frequency:")
        lbl.setFont(_mono_font(10, bold=True))
        lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        row.addWidget(lbl)

        self._sp_trig = QDoubleSpinBox()
        self._sp_trig.setRange(0.0, 1000.0)
        self._sp_trig.setDecimals(3)
        self._sp_trig.setSingleStep(1.0)
        self._sp_trig.setSuffix(" Hz")
        self._sp_trig.setSpecialValueText("Off")
        self._sp_trig.setFixedHeight(36)
        self._sp_trig.setMinimumWidth(180)
        self._sp_trig.setFont(_mono_font(13, bold=True))
        self._sp_trig.setStyleSheet(_spin_style())
        self._sp_trig.setEnabled(False)
        row.addWidget(self._sp_trig)

        self._btn_trig_apply = QPushButton("Set")
        self._btn_trig_apply.setFixedWidth(70)
        self._btn_trig_apply.setFixedHeight(36)
        self._btn_trig_apply.setStyleSheet(_btn_style(_GREEN))
        self._btn_trig_apply.setEnabled(False)
        self._btn_trig_apply.clicked.connect(self._on_trig_apply)
        row.addWidget(self._btn_trig_apply)

        self._lbl_trig_actual = QLabel("actual: —")
        self._lbl_trig_actual.setFont(_mono_font(9))
        self._lbl_trig_actual.setStyleSheet(f"color: {_DIM}; background: transparent;")
        row.addWidget(self._lbl_trig_actual)

        row.addStretch()

        self._be.sig_connected.connect(self._on_trig_connected)
        self._be.sig_disconnected.connect(self._on_trig_disconnected)
        self._be.sig_status.connect(self._on_trig_status)
        return g

    @Slot()
    def _on_trig_connected(self):
        self._sp_trig.setEnabled(True)
        self._btn_trig_apply.setEnabled(True)

    @Slot(str)
    def _on_trig_disconnected(self, _reason: str):
        self._sp_trig.setEnabled(False)
        self._btn_trig_apply.setEnabled(False)
        self._lbl_trig_actual.setText("actual: —")

    @Slot(dict)
    def _on_trig_status(self, d: dict):
        raw = d.get("trig_half_period")
        if raw is None:
            return
        half = int(raw)
        if half == 0:
            self._lbl_trig_actual.setText("actual: Off")
        else:
            actual_hz = CLK_HZ / (2.0 * half)
            self._lbl_trig_actual.setText(f"actual: {actual_hz:.3f} Hz")

    def _on_trig_apply(self):
        f_hz = self._sp_trig.value()
        half = trig_hz_to_half_period(f_hz)
        self._be.set_trig(half)
        self._log(f"[Trig] DIO2 → {f_hz:.3f} Hz  (half_period={half})")

    def _build_log(self) -> QGroupBox:
        g = _make_group("Log")
        lay = QVBoxLayout(g)
        lay.setContentsMargins(_SP_MD, _SP_MD, _SP_MD, 10)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMinimumHeight(72)
        self._log_box.setMaximumHeight(110)
        self._log_box.setFont(_mono_font(9))
        self._log_box.setStyleSheet(
            f"background: #090e15; color: {_DIM}; border: none; border-radius: 6px;"
        )
        lay.addWidget(self._log_box)
        return g

    # ── connection ─────────────────────────────────────────────────────────────

    def _pick_key(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SSH private key", str(Path.home() / ".ssh")
        )
        if path:
            self._w_key.setText(path)

    def _toggle_connect(self):
        if self._be.live:
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
        self._btn_conn.setText("Disconnect")
        self._btn_conn.setEnabled(True)
        self._lbl_status.setText("●  Connected")
        self._lbl_status.setStyleSheet(f"color: {_GREEN}; background: transparent;")
        self._poll.start()
        self._be.poll()
        self._log("Connected.")

    @Slot(str)
    def _on_disconnected(self, reason: str):
        self._poll.stop()
        self._btn_conn.setText("Connect")
        self._btn_conn.setEnabled(True)
        self._lbl_status.setText("●  Disconnected")
        self._lbl_status.setStyleSheet(f"color: {_RED}; background: transparent;")
        self._lbl_mode.setText("mode: —")
        self._log(f"Disconnected: {reason}")

    @Slot(str)
    def _on_mode_changed(self, mode: str):
        self._lbl_mode.setText(f"mode: {mode}")
        color = _ACCENT if mode == "pulse" else _GREEN
        self._lbl_mode.setStyleSheet(f"color: {color}; background: transparent;")

    @Slot(int)
    def _on_tab_changed(self, idx: int):
        """Switch the poll helper when the user changes tabs."""
        mode = "pulse" if idx == 0 else "harmonic"
        self._be.set_active_mode(mode)

    # ── Ctrl+Return routes to whichever tab is active ─────────────────────────

    def _active_panel_apply(self):
        panel = self._tabs.currentWidget()
        if isinstance(panel, _NcoPanel):
            panel.apply()

    # ── adaptive polling ──────────────────────────────────────────────────────

    POLL_MIN_MS = 300    # keep the UI responsive for short windows
    POLL_MAX_MS = 1000   # don't lag far behind a long measurement window

    @Slot(dict)
    def _adapt_poll_interval(self, d: dict):
        """Track the active measurement window (clamped) as the poll period."""
        raw = d.get("meas_time_us")
        if raw is None:
            return
        interval = max(self.POLL_MIN_MS, min(self.POLL_MAX_MS, int(raw) // 1000))
        if interval != self._poll.interval():
            self._poll.setInterval(interval)

    # ── error / log ────────────────────────────────────────────────────────────

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
    app.setApplicationName("RP Combined Control")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
