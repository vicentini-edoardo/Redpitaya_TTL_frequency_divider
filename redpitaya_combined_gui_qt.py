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
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
    from PySide6.QtGui import QAction, QFont, QFontMetrics, QIcon, QIntValidator, QKeySequence
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFrame,
        QInputDialog,
        QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
        QMessageBox, QSizePolicy, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
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
    CLK_HZ, PHASE_BITS, DEFAULT_BASE, CTRL_ENABLE, CTRL_FORCE_HIGH, CTRL_OSC_MODE,
    CTRL_EDGE_LOCK, CTRL_EDGE_RESPONSE_MASK, CTRL_EDGE_RESPONSE_BALANCED,
    CTRL_EDGE_RESPONSE_FAST, CTRL_EDGE_RESPONSE_SMOOTH,
    DEFAULT_EDGE_LOCK_RESPONSE, EDGE_LOCK_RESPONSES,
    PHASE_RES_HZ, MAX_SHIFT_HZ, WINDOW_OPTIONS_US, WINDOW_NAMES,
    hz_to_phase, phase_to_hz, duty_to_cycles, fmt_freq, fmt_signed_freq,
    suggest_window, trig_hz_to_phase_step, trig_phase_step_to_hz, fmt_dur,
    strobe_step_word, dwell_s_to_cycles,
    phase_offset_to_preload, preload_to_phase_offset,
    harmonic_phase_offset_to_preload, harmonic_preload_to_phase_offset,
)

_APP_DIR = Path(__file__).resolve().parent
_APP_ICON_PATH = _APP_DIR / "6713611.ico"
_WINDOWS_APP_ID = "VicentiniEdoardo.RedPitayaTTLFrequencyDivider"
_LEGACY_REPO_STATE = "rp_state.json"


def _set_windows_app_id():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_WINDOWS_APP_ID)
    except Exception:
        pass


def _apply_app_icon(window: Optional[QWidget] = None) -> Optional[QIcon]:
    if not _APP_ICON_PATH.exists():
        return None
    icon = QIcon(os.fspath(_APP_ICON_PATH))
    if icon.isNull():
        return None
    app = QApplication.instance()
    if app is not None:
        app.setWindowIcon(icon)
    if window is not None:
        window.setWindowIcon(icon)
    return icon


def _default_state_file() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "RedPitayaTTLFrequencyDivider" / _LEGACY_REPO_STATE


def _confirmed_state(
    status: Optional[dict], *, connected: bool, sequence: int, now: Optional[float] = None
) -> dict:
    """Build the cross-application state from FPGA register readback."""
    d = status or {}
    control = int(d.get("control") or 0)
    trig_step = int(d.get("trig_phase_step") or 0)
    shift_step = int(d.get("phase_step_offset") or 0)
    base_step = int(d.get("phase_step_base") or 0)
    live_step = int(d.get("phase_step") or 0)
    width_cycles = int(d.get("width") or 0)
    dwell = int(d.get("dwell_cycles") or 0)
    osc_mode = bool(int(d.get("osc_mode") or 0))
    harmonic_mode = bool(int(d.get("harmonic_mode") or 0))
    mode = "osc" if osc_mode else ("harmonic" if harmonic_mode else "pulse")
    output_mode = "on" if control & CTRL_FORCE_HIGH else (
        "modulated" if control & CTRL_ENABLE else "off"
    )
    shift_hz = phase_to_hz(shift_step)
    # osc (strobe) mode holds a constant phase per point — no beat peak; the
    # shift register carries the per-step phase increment, not a frequency.
    expected_hz = 0.0 if osc_mode else abs(shift_hz)
    period_cycles = (1 << PHASE_BITS) // base_step if base_step > 0 else 0
    duty_pct = (
        50.0 if harmonic_mode else (
            100.0 * width_cycles / period_cycles if period_cycles > 0 else 0.0
        )
    )
    confirmed = bool(connected and status)
    return {
        "schema_version": 1,
        "source": "redpitaya_ttl_frequency_divider",
        "connected": bool(connected),
        "hardware_confirmed": confirmed,
        "sequence": int(sequence),
        "updated_at": time.time() if now is None else float(now),
        "mode": mode,
        "output_mode": output_mode,
        "period_stable": bool(d.get("period_stable")) if confirmed else False,
        "trigger_frequency_hz": trig_phase_step_to_hz(trig_step),
        "frequency_shift_hz": shift_hz,
        "pulse_freq_shift_hz": None if harmonic_mode else shift_hz,
        "harmonic_freq_shift_hz": shift_hz if harmonic_mode else None,
        "expected_peak_hz": expected_hz,
        "input_frequency_hz": phase_to_hz(base_step),
        "output_frequency_hz": phase_to_hz(live_step),
        "control": control,
        "edge_lock": bool(int(d.get("edge_lock") or 0)),
        "harmonic_n": int(d.get("mult_n") or 1),
        "width_cycles": width_cycles,
        "duty_cycle_pct": duty_pct,
        "trig_phase_step": trig_step,
        "phase_step_offset": shift_step,
        "phase_step_base": base_step,
        "phase_step": live_step,
        "dwell_cycles": dwell,
        "osc_phase_preload": int(d.get("osc_phase_preload") or 0),
        "n_steps": int(d.get("n_steps") or 0),
        "step_index": int(d.get("step_index") or 0),
        "strobe_done": bool(int(d.get("strobe_done") or 0)),
    }


def _run_git_command(repo_dir: Path, cmd: list[str], run=subprocess.run) -> str:
    result = run(
        cmd,
        capture_output=True,
        text=True,
        cwd=repo_dir,
        timeout=30,
    )
    out = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(out or f"{' '.join(cmd)} failed with exit code {result.returncode}")
    return out


def _parse_remote_branches(output: str) -> list[str]:
    branches = []
    for raw in output.splitlines():
        branch = raw.strip()
        if not branch or "->" in branch or branch.endswith("/HEAD"):
            continue
        branches.append(branch)
    return sorted(set(branches))


def _remote_branch_name(remote_ref: str) -> str:
    return remote_ref.split("/", 1)[1] if "/" in remote_ref else remote_ref


def _list_remote_branches(repo_dir: Path, run=subprocess.run) -> list[str]:
    return _parse_remote_branches(_run_git_command(repo_dir, ["git", "branch", "-r"], run=run))


def _fetch_remote_branches(repo_dir: Path, run=subprocess.run) -> list[str]:
    _run_git_command(repo_dir, ["git", "fetch", "--prune", "origin"], run=run)
    return _list_remote_branches(repo_dir, run=run)


def _cleanup_legacy_repo_state(repo_dir: Path, run=subprocess.run) -> str:
    legacy_state = repo_dir / _LEGACY_REPO_STATE
    if not legacy_state.exists():
        return ""
    status = _run_git_command(
        repo_dir,
        ["git", "status", "--porcelain", "--", _LEGACY_REPO_STATE],
        run=run,
    )
    if not status:
        return ""
    lines = [line for line in status.splitlines() if line.strip()]
    if lines and all(line.startswith("?? ") for line in lines):
        legacy_state.unlink(missing_ok=True)
        return "Removed legacy repo state file."
    _run_git_command(
        repo_dir,
        ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", _LEGACY_REPO_STATE],
        run=run,
    )
    return "Restored legacy repo state file."


def _run_git_update(repo_dir: Path, remote_ref: str, run=subprocess.run) -> tuple[str, bool]:
    branch = _remote_branch_name(remote_ref)
    before_head = _run_git_command(repo_dir, ["git", "rev-parse", "HEAD"], run=run)
    cleanup_out = _cleanup_legacy_repo_state(repo_dir, run=run)
    _run_git_command(repo_dir, ["git", "fetch", "--prune", "origin"], run=run)

    current_branch = _run_git_command(repo_dir, ["git", "branch", "--show-current"], run=run)
    if current_branch != branch:
        try:
            _run_git_command(repo_dir, ["git", "checkout", branch], run=run)
        except RuntimeError:
            _run_git_command(
                repo_dir,
                ["git", "checkout", "-b", branch, "--track", remote_ref],
                run=run,
            )
    _run_git_command(
        repo_dir,
        ["git", "branch", "--set-upstream-to", remote_ref, branch],
        run=run,
    )
    _run_git_command(repo_dir, ["git", "pull", "--ff-only"], run=run)

    after_head = _run_git_command(repo_dir, ["git", "rev-parse", "HEAD"], run=run)
    if before_head == after_head:
        return cleanup_out or "Already up to date.", False
    changelog = _run_git_command(
        repo_dir,
        ["git", "log", f"{before_head}..{after_head}", "--pretty=format:• %s", "--no-merges"],
        run=run,
    )
    return changelog or "Updated.", True


class _CommitLineEdit(QLineEdit):
    focus_left = Signal()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.focus_left.emit()


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

    def apply_pulse(self, width_cycles: int, offset_word: int,
                    edge_lock: bool = False, preload: Optional[int] = None,
                    edge_response: int = DEFAULT_EDGE_LOCK_RESPONSE):
        """Pulse-mode modulated write (enable=1, harmonic_mode=0 via helper).

        When ``edge_lock`` is on and ``preload`` is given, the phase-offset
        preload is written and the lock is re-armed so the new phase takes
        effect (see ``_do_apply_pulse_locked``)."""
        if self._live:
            ctrl = CTRL_ENABLE | (edge_response & CTRL_EDGE_RESPONSE_MASK)
            ctrl |= CTRL_EDGE_LOCK if edge_lock else 0
            if edge_lock and preload is not None:
                self._enqueue(self.P_USER,
                              lambda: self._do_apply_pulse_locked(
                                  width_cycles, offset_word, preload, ctrl),
                              self.sig_status.emit)
            else:
                self._enqueue(self.P_USER,
                              lambda: self._do_write_pulse(width_cycles, offset_word, ctrl),
                              self.sig_status.emit)

    def apply_harmonic(self, mult_n: int, offset_word: int,
                       edge_lock: bool = False, preload: Optional[int] = None,
                       edge_response: int = DEFAULT_EDGE_LOCK_RESPONSE):
        """Harmonic-mode modulated write (enable=1, harmonic_mode=1 via helper).

        When ``edge_lock`` is on and ``preload`` is given, the phase-offset
        preload is written and the lock is re-armed so the new phase takes
        effect (see ``_do_apply_harmonic_locked``)."""
        if self._live:
            ctrl = CTRL_ENABLE | (edge_response & CTRL_EDGE_RESPONSE_MASK)
            ctrl |= CTRL_EDGE_LOCK if edge_lock else 0
            if edge_lock and preload is not None:
                self._enqueue(self.P_USER,
                              lambda: self._do_apply_harmonic_locked(
                                  mult_n, offset_word, preload, ctrl),
                              self.sig_status.emit)
            else:
                self._enqueue(self.P_USER,
                              lambda: self._do_write_harmonic(mult_n, offset_word, ctrl),
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

    def set_trig(self, phase_step: int):
        """Write DIO2 48-bit NCO step. phase_step=0 disables."""
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_set_trig(phase_step),
                          self.sig_status.emit)

    def set_window(self, window_us: int):
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_window(window_us),
                          self.sig_status.emit)

    def soft_reset(self):
        if self._live:
            self._enqueue(self.P_USER, self._do_reset, self.sig_status.emit)

    def apply_osc(self, width_cycles: int, dwell_cycles: int, preload: int,
                  step_word: int, n_steps: int):
        """Set strobe registers then write to arm (osc_mode + enable bits)."""
        if self._live:
            ctrl = CTRL_ENABLE | CTRL_OSC_MODE
            self._enqueue(self.P_USER,
                          lambda: self._do_apply_osc(width_cycles, dwell_cycles,
                                                     preload, step_word, n_steps, ctrl),
                          self.sig_status.emit)

    def disable_osc(self):
        """Clear osc_mode bit, keep enable."""
        if self._live:
            self._enqueue(self.P_USER,
                          lambda: self._do_set_control_pulse(CTRL_ENABLE),
                          self.sig_status.emit)

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

    def _do_apply_osc(self, wc: int, dwell: int, preload: int,
                      step_word: int, n_steps: int, ctrl: int) -> dict:
        self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} osc {dwell} {preload} {n_steps}"
        )
        # Clear the osc bit first: the FPGA latches the preload and re-arms
        # the scan on the RISING edge of osc_mode, so a re-apply while osc
        # is already running needs an explicit off→on toggle. The output and
        # the frequency measurement keep running throughout.
        self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} control {CTRL_ENABLE}"
        )
        return json.loads(self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} write {wc} {step_word} {ctrl}"
        ))

    def _do_apply_pulse_locked(self, width: int, offset: int, preload: int,
                               ctrl: int) -> dict:
        self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} preload {preload}"
        )
        # The FPGA reloads osc_phase_preload into the accumulator on the RISING
        # edge of edge_lock, so applying a new phase offset while the lock is
        # already engaged needs an explicit off→on toggle. Drop the lock bit
        # first, then re-assert it with the modulated write. The output and the
        # frequency measurement keep running; pulses resume on the next input
        # edge that re-anchors the phase.
        self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} control {ctrl & ~CTRL_EDGE_LOCK}"
        )
        return json.loads(self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} write {width} {offset} {ctrl}"
        ))

    def _do_write_pulse(self, width: int, offset: int,
                        ctrl: int = CTRL_ENABLE) -> dict:
        return json.loads(self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} write {width} {offset} {ctrl}"
        ))

    def _do_apply_harmonic_locked(self, mult_n: int, offset: int, preload: int,
                                  ctrl: int) -> dict:
        self._exec(
            f"/root/rp_harmonic_ctl 0x{self._base:08X} preload {preload}"
        )
        # See _do_apply_pulse_locked: the preload latches on the rising edge of
        # edge_lock, so re-arm it (drop bit5, then the modulated write) to apply
        # a new phase while the lock is already engaged.
        self._exec(
            f"/root/rp_harmonic_ctl 0x{self._base:08X} control {ctrl & ~CTRL_EDGE_LOCK}"
        )
        return json.loads(self._exec(
            f"/root/rp_harmonic_ctl 0x{self._base:08X} write {mult_n} {offset} {ctrl}"
        ))

    def _do_write_harmonic(self, mult_n: int, offset: int,
                           ctrl: int = CTRL_ENABLE) -> dict:
        return json.loads(self._exec(
            f"/root/rp_harmonic_ctl 0x{self._base:08X} write {mult_n} {offset} {ctrl}"
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

    def _do_set_trig(self, phase_step: int) -> dict:
        return json.loads(self._exec(
            f"/root/rp_pulse_ctl 0x{self._base:08X} trig {phase_step}"
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
_BG       = "#0a0f16"
_PANEL    = "#0f1722"
_SURFACE  = "#131d2b"
_SURFACE2 = "#182334"
_FIELD    = "#09111c"
_ACCENT   = "#62a8ff"
_GREEN    = "#4fc17b"
_AMBER    = "#e2aa4f"
_RED      = "#f26d6d"
_WHITE    = "#f2f6fb"
_TEXT     = "#e7edf6"
_DIM      = "#93a4ba"
_MUTED    = "#6f8198"
_BORDER   = "#2b3a4d"

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
            background: {_PANEL};
            color: {_DIM};
            border: 1px solid {_BORDER};
            border-radius: 10px;
            margin-top: 15px;
            padding: 12px 14px 12px 14px;
            font-family: {_MONO};
            font-size: 10px;
            font-weight: bold;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 8px;
            color: {_ACCENT};
            background: {_BG};
        }}
    """


def _btn_style(color: str = _ACCENT) -> str:
    return f"""
        QPushButton {{
            background: {_SURFACE2};
            color: {color};
            border: 1px solid {color};
            border-radius: 7px;
            padding: 7px 14px;
            font-family: {_MONO};
            font-size: 10px;
            font-weight: 600;
        }}
        QPushButton:hover   {{ background: {_PANEL}; }}
        QPushButton:pressed {{ background: {_FIELD}; }}
        QPushButton:disabled {{
            background: {_FIELD};
            color: {_MUTED};
            border-color: {_BORDER};
        }}
    """


def _mode_btn_style(color: str, active: bool) -> str:
    """Style for the three output-mode buttons (OFF / MODULATED / ON)."""
    if active:
        return f"""
            QPushButton {{
                background: {color}24;
                color: {color};
                border: 1px solid {color};
                border-radius: 7px;
                padding: 6px 14px;
                font-family: {_MONO};
                font-size: 10px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background: {color}32; }}
        """
    return f"""
        QPushButton {{
            background: {_FIELD};
            color: {_DIM};
            border: 1px solid {_BORDER};
            border-radius: 7px;
            padding: 6px 14px;
            font-family: {_MONO};
            font-size: 10px;
        }}
        QPushButton:hover {{ background: {_SURFACE}; color: {color}; border-color: {color}; }}
    """


def _le_style() -> str:
    return f"""
        QLineEdit {{
            background: {_FIELD};
            color: {_TEXT};
            border: 1px solid {_BORDER};
            border-radius: 6px;
            padding: 5px 8px;
            font-family: {_MONO};
            font-size: 10px;
        }}
        QLineEdit:focus {{ border-color: {_ACCENT}; }}
        QLineEdit:disabled {{ color: {_MUTED}; }}
    """


def _spin_style() -> str:
    return f"""
        QDoubleSpinBox, QSpinBox, QComboBox {{
            background: {_FIELD};
            color: {_TEXT};
            border: 1px solid {_BORDER};
            border-radius: 7px;
            padding: 5px 8px;
            font-family: {_MONO};
            font-size: 10px;
        }}
        QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {_ACCENT}; }}
        QDoubleSpinBox:disabled, QSpinBox:disabled, QComboBox:disabled {{
            color: {_MUTED};
            background: #07101a;
        }}
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
        QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; }}
        QComboBox::drop-down {{ width: 22px; border: none; }}
        QComboBox QAbstractItemView {{
            background: {_FIELD};
            color: {_TEXT};
            selection-background-color: {_SURFACE2};
            border: 1px solid {_BORDER};
        }}
    """


def _checkbox_style() -> str:
    return f"""
        QCheckBox {{
            color: {_TEXT};
            background: transparent;
            spacing: 8px;
            font-family: {_MONO};
            font-size: 10px;
        }}
        QCheckBox:disabled {{ color: {_MUTED}; }}
        QCheckBox::indicator {{
            width: 14px;
            height: 14px;
            border: 1px solid {_BORDER};
            border-radius: 3px;
            background: {_FIELD};
        }}
        QCheckBox::indicator:checked {{
            background: {_ACCENT};
            border-color: {_ACCENT};
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

    VALUE_FONT_MAX = 22
    VALUE_FONT_MIN = 12

    def __init__(self, title: str, sub_hint: str = "",
                 accent: str = _ACCENT, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._accent = accent
        self._value_color = _DIM
        self.setObjectName(f"readout{title.replace(' ', '')}")
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"""
            QFrame {{
                background: {_SURFACE};
                border: 1px solid {_BORDER};
                border-radius: 10px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 10, 18, 10)
        lay.setSpacing(2)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title_lbl.setFont(_mono_font(9, bold=True))
        title_lbl.setFixedHeight(16)
        title_lbl.setStyleSheet(
            f"color: {_DIM}; background: transparent; border: none;"
        )
        lay.addWidget(title_lbl)

        self._val = QLabel("---")
        self._val.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._val.setFont(_mono_font(self.VALUE_FONT_MAX, bold=True))
        self._val.setStyleSheet(f"color: {_DIM}; background: transparent; border: none;")
        self._val.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._val.setMinimumHeight(24)
        lay.addWidget(self._val, 1)

        self._sub = QLabel(sub_hint)
        self._sub.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._sub.setFont(_mono_font(9))
        self._sub.setFixedHeight(16)
        self._sub.setStyleSheet(
            f"color: {_MUTED}; background: transparent; border: none;"
        )
        lay.addWidget(self._sub)

    def set_data(self, value: str, sub: str = "", color: Optional[str] = None):
        self._val.setText(value)
        self._value_color = color or (self._accent if value != "---" else _DIM)
        self._fit_value_font()
        if sub is not None:
            self._sub.setText(sub)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_value_font()

    def _fit_value_font(self):
        text = self._val.text()
        if not text:
            return

        rect = self._val.contentsRect()
        available_w = max(1, rect.width())
        available_h = max(1, rect.height())
        chosen = self.VALUE_FONT_MIN

        for size in range(self.VALUE_FONT_MAX, self.VALUE_FONT_MIN - 1, -1):
            font = _mono_font(size, bold=True)
            metrics = QFontMetrics(font)
            if (metrics.horizontalAdvance(text) <= available_w and
                    metrics.height() <= available_h):
                chosen = size
                break

        self._val.setFont(_mono_font(chosen, bold=True))
        self._val.setStyleSheet(
            f"color: {self._value_color}; background: transparent; border: none;"
        )


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
    sig_params_changed = Signal()

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
        self._sp_phase = None   # edge-lock phase-offset spin (pulse mode only)
        self._window_ms = 100

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

    def _build_lock_extras(self, auto_row) -> None:
        """A phase-offset spin placed next to the Edge-lock checkbox. The offset
        is only meaningful while the phase is anchored to the input, so it is
        enabled together with edge lock (see _update_mode_controls)."""
        lbl = _dim_label("Phase:")
        lbl.setToolTip(
            "Constant phase offset of the output relative to each anchored\n"
            "input rising edge, as a fraction of one output period.\n"
            "Only active with Edge lock on (the phase needs an input anchor).\n"
            "0° = output edge aligned to the input edge; 90° = quarter period later."
        )
        auto_row.addSpacing(12)
        auto_row.addWidget(lbl)
        self._sp_phase = QDoubleSpinBox()
        self._sp_phase.setRange(0.0, 360.0)
        self._sp_phase.setWrapping(True)
        self._sp_phase.setDecimals(1)
        self._sp_phase.setSingleStep(1.0)
        self._sp_phase.setSuffix(" °")
        self._sp_phase.setValue(0.0)
        self._sp_phase.setFixedHeight(30)
        self._sp_phase.setMinimumWidth(96)
        self._sp_phase.setFont(_mono_font(11, bold=True))
        self._sp_phase.setStyleSheet(_spin_style())
        self._sp_phase.setEnabled(False)   # gated on edge lock via _update_mode_controls
        self._sp_phase.valueChanged.connect(self._param_changed)
        auto_row.addWidget(self._sp_phase)

    # Phase-offset ↔ NCO preload conversion. Defaults are the pulse-mode
    # formulas (pulse fires on the 2^48 carry); HarmonicPanel overrides these
    # because its square-wave output rises at the 2^47 MSB crossing.
    def _phase_turns_to_preload(self, turns: float) -> int:
        return phase_offset_to_preload(turns)

    def _preload_to_phase_turns(self, word: int) -> float:
        return preload_to_phase_offset(word)

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

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(_SP_SM, _SP_MD, _SP_SM, _SP_SM)
        root.setSpacing(_SP_LG)

        # Monitors 2×2 — left column shared, right column mode-specific
        self._d_in  = BigDisplay("Input Frequency",  "measured input period", _ACCENT)
        self._d_out = BigDisplay("Output Frequency", self._out_hint(),        _GREEN)
        self._d_tr, self._d_br = self._make_right_tiles()

        for d in (self._d_in, self._d_tr, self._d_out, self._d_br):
            d.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            d.setMinimumHeight(80)

        readouts = QFrame()
        readouts.setObjectName("rpReadoutGrid")
        readouts.setStyleSheet("QFrame#rpReadoutGrid { background: transparent; border: none; }")
        grid = QGridLayout(readouts)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(_SP_MD)
        grid.addWidget(self._d_in,  0, 0)
        grid.addWidget(self._d_tr,  0, 1)
        grid.addWidget(self._d_out, 1, 0)
        grid.addWidget(self._d_br,  1, 1)
        for i in range(2):
            grid.setRowStretch(i, 1)
            grid.setColumnStretch(i, 1)
        root.addWidget(readouts, 1)

        # Controls group
        controls = _make_group("Controls")
        controls.setObjectName("rpControlDeck")
        controls.setMinimumHeight(235)
        cl = QHBoxLayout(controls)
        cl.setContentsMargins(_SP_LG, 12, _SP_LG, _SP_LG)
        cl.setSpacing(_SP_LG)

        left_col = QVBoxLayout()
        left_col.setSpacing(_SP_MD)

        # ── Output mode bar ───────────────────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(_SP_SM)
        mode_lbl = QLabel("Output mode:")
        mode_lbl.setFont(_mono_font(10, bold=True))
        mode_lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        mode_row.addWidget(mode_lbl)
        self._btn_off = QPushButton("Laser off")
        self._btn_mod = QPushButton("Modulated")
        self._btn_on  = QPushButton("Laser on")
        for btn in (self._btn_off, self._btn_mod, self._btn_on):
            btn.setFixedHeight(34)
            btn.setFont(_mono_font(10))
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
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(14)
        fields.setRowMinimumHeight(0, 44)
        fields.setRowMinimumHeight(1, 44)
        fields.setRowMinimumHeight(2, 30)
        fields.setRowMinimumHeight(3, 42)

        # Row 0: freq shift (shared) + mode-specific secondary field
        fields.addWidget(_dim_label("Freq shift:"), 0, 0)
        self._sp_offset = QDoubleSpinBox()
        self._sp_offset.setRange(-MAX_SHIFT_HZ, MAX_SHIFT_HZ)
        self._sp_offset.setDecimals(6)
        self._sp_offset.setSingleStep(1.0)
        self._sp_offset.setSuffix(" Hz")
        self._sp_offset.setFixedHeight(34)
        self._sp_offset.setMinimumWidth(210)
        self._sp_offset.setFont(_mono_font(12, bold=True))
        self._sp_offset.setStyleSheet(_spin_style())
        self._sp_offset.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_offset, 0, 1)

        self._secondary_widget = self._build_secondary_field(fields)

        # Row 1: measurement window
        fields.addWidget(_dim_label("Meas. window:"), 1, 0)
        self._window_field = _CommitLineEdit(str(self._window_ms))
        self._window_field.setValidator(QIntValidator(0, 1_000_000, self))
        self._window_field.setFixedHeight(32)
        self._window_field.setFixedWidth(86)
        self._window_field.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._window_field.setFont(_mono_font(12, bold=True))
        self._window_field.setStyleSheet(_le_style())
        self._window_field.returnPressed.connect(self._commit_window_field)
        self._window_field.focus_left.connect(self._commit_window_field)
        fields.addWidget(self._window_field, 1, 1)
        fields.addWidget(_dim_label("ms"), 1, 2)

        self._lbl_win_suggest = QLabel()
        self._lbl_win_suggest.setFont(_mono_font(9))
        self._lbl_win_suggest.setStyleSheet(f"color: {_AMBER}; background: transparent;")
        fields.addWidget(self._lbl_win_suggest, 1, 3)

        # Row 2: auto-apply + edge lock
        auto_row = QHBoxLayout()
        self._cb_auto = QCheckBox("Auto-Apply")
        self._cb_auto.setChecked(True)
        self._cb_auto.setFont(_mono_font(10))
        self._cb_auto.setStyleSheet(_checkbox_style())
        auto_row.addWidget(self._cb_auto)

        self._cb_lock = QCheckBox("Edge lock")
        self._cb_lock.setChecked(False)
        self._cb_lock.setFont(_mono_font(10))
        self._cb_lock.setStyleSheet(_checkbox_style())
        self._cb_lock.setToolTip(
            "Anchor the NCO phase to every input rising edge.\n"
            "The output-to-input frequency shift becomes exactly f_shift with\n"
            "the beat phase coherent indefinitely (no drift from measurement\n"
            "error) — recommended for stroboscopic / FFT measurements.\n"
            "Cost: output pulses inherit the input edge timing jitter\n"
            "(~±8 ns synchronizer quantization + source jitter)."
        )
        self._cb_lock.toggled.connect(self._on_lock_toggled)
        auto_row.addWidget(self._cb_lock)
        auto_row.addSpacing(12)
        auto_row.addWidget(_dim_label("Response:"))
        self._edge_response = QComboBox()
        for label, control_bits in EDGE_LOCK_RESPONSES:
            self._edge_response.addItem(label, control_bits)
        self._edge_response.setCurrentIndex(
            self._edge_response.findData(DEFAULT_EDGE_LOCK_RESPONSE)
        )
        self._edge_response.setToolTip(
            "Hard snaps at the next accepted edge. Fast, Balanced, and Smooth "
            "apply progressively smaller continuous phase corrections."
        )
        self._edge_response.setStyleSheet(_spin_style())
        self._edge_response.currentIndexChanged.connect(self._param_changed)
        auto_row.addWidget(self._edge_response)
        self._build_lock_extras(auto_row)
        auto_row.addStretch()
        fields.addLayout(auto_row, 2, 0, 1, 4)

        # Row 3: shift detail
        self._lbl_shift = QLabel()
        self._lbl_shift.setFont(_mono_font(9))
        self._lbl_shift.setWordWrap(True)
        self._lbl_shift.setStyleSheet(f"color: {_MUTED}; background: transparent;")
        fields.addWidget(self._lbl_shift, 3, 0, 1, 4)
        fields.setColumnStretch(1, 1)

        left_col.addLayout(fields)
        cl.addLayout(left_col, 1)

        # Action column
        actions = QVBoxLayout()
        actions.setSpacing(8)

        self._btn_apply = QPushButton("Apply now\nCtrl+Return")
        self._btn_apply.setFixedWidth(190)
        self._btn_apply.setMinimumHeight(60)
        self._btn_apply.setFont(_mono_font(12, bold=True))
        self._btn_apply.setStyleSheet(_btn_style(_GREEN))
        self._btn_apply.clicked.connect(self._do_apply)
        self._btn_apply.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        actions.addWidget(self._btn_apply)

        self._btn_reset = QPushButton("Soft reset")
        self._btn_reset.setFixedWidth(190)
        self._btn_reset.setFixedHeight(34)
        self._btn_reset.setStyleSheet(_btn_style(_AMBER))
        self._btn_reset.clicked.connect(self._do_soft_reset)
        actions.addWidget(self._btn_reset)

        self._btn_upload = QPushButton(f"Upload && compile\n{self._tag} mode")
        self._btn_upload.setFixedWidth(190)
        self._btn_upload.setFixedHeight(44)
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
        self.sig_params_changed.emit()
        if self._live:
            response = self._edge_response_bits()
            if mode == "off":
                self._be_set_control(response)
                self._log("Laser OFF  (output = constant 0)")
            elif mode == "on":
                self._be_set_control(CTRL_FORCE_HIGH | response)
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
        for w in (self._sp_offset, self._secondary_widget, self._window_field,
                  self._cb_auto, self._cb_lock, self._edge_response, self._btn_apply):
            w.setEnabled(enabled)
        # The phase offset is only meaningful while the phase is anchored to the
        # input, so it follows both modulated-mode and the edge-lock checkbox.
        if self._sp_phase is not None:
            self._sp_phase.setEnabled(enabled and self._cb_lock.isChecked())

    def _on_lock_toggled(self, checked: bool):
        self._log(f"Edge lock {'ON' if checked else 'OFF'} "
                  "(applies with the next write)")
        if self._sp_phase is not None:
            self._sp_phase.setEnabled(checked and self._output_mode == "modulated")
        self.sig_params_changed.emit()
        if self._live and self._output_mode == "modulated":
            self._do_apply()

    def _edge_response_bits(self) -> int:
        return int(self._edge_response.currentData()) & CTRL_EDGE_RESPONSE_MASK

    # ── backend signal handlers ────────────────────────────────────────────────

    @Slot()
    def _on_connected(self):
        self._live = True
        self._refresh_pending = True
        if self._be.mode == self.MODE:
            self._be.set_window(self._window_ms * 1000)

    @Slot(str)
    def _on_disconnected(self, _reason: str):
        self._live = False
        self._refresh_pending = False
        self._period_c = 0
        self._on_disconnected_extra()

    @Slot(str)
    def _on_mode_changed(self, mode: str):
        if mode == self.MODE and self._live:
            self._be.set_window(self._window_ms * 1000)

    @Slot(dict)
    def _on_status(self, d: dict):
        if int(d.get("harmonic_mode", 0)) != self._harmonic_json:
            return  # JSON from the other helper; skip

        raw_us = d.get("meas_time_us")
        if raw_us is not None and not self._window_field.hasFocus():
            self._set_window_field_ms(max(1, int(raw_us) // 1000))

        # Sync output mode from FPGA control register
        ctrl = int(d.get("control") or 0)
        response_index = self._edge_response.findData(ctrl & CTRL_EDGE_RESPONSE_MASK)
        if response_index >= 0 and response_index != self._edge_response.currentIndex():
            self._edge_response.blockSignals(True)
            self._edge_response.setCurrentIndex(response_index)
            self._edge_response.blockSignals(False)
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

        if step_base > 0:
            # Derive period in clock cycles from phase_step_base (avoids integer truncation)
            self._period_c = (1 << PHASE_BITS) // step_base
            in_hz = phase_to_hz(step_base)
            self._d_in.set_data(
                fmt_freq(in_hz),
                "stable" if stable else "acquiring …",
                _ACCENT if stable else _AMBER,
            )
            if self._refresh_pending:
                self._refresh_pending = False
                self._log(f"Input: {fmt_freq(in_hz)} ({'stable' if stable else 'acquiring'})")
        else:
            self._d_in.set_data("---", "no input signal", _RED)

        if step_base > 0:
            step_live = int(d.get("phase_step") or step_base)
            step_off  = int(d.get("phase_step_offset") or (step_live - step_base))
            out_hz    = phase_to_hz(step_live)
            delta     = phase_to_hz(step_off)
            locked    = bool(int(d.get("edge_lock") or 0))
            sub       = f"shift {fmt_signed_freq(delta)}"
            if locked:
                sub += f" · edge-locked · {self._edge_response.currentText().lower()}"
                # Show the device's live phase offset (mode-specific mapping).
                if self._sp_phase is not None:
                    ph = self._preload_to_phase_turns(int(d.get("osc_phase_preload") or 0)) * 360.0
                    sub += f" · φ={ph:.1f}°"
            self._d_out.set_data(fmt_freq(out_hz), sub)
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
        self.sig_params_changed.emit()

    def _set_window_field_ms(self, window_ms: int):
        self._window_ms = max(1, int(window_ms))
        self._window_field.setText(str(self._window_ms))

    def _commit_window_field(self):
        text = self._window_field.text().strip()
        if not text:
            self._set_window_field_ms(self._window_ms)
            self._update_window_suggestion()
            return

        window_ms = max(1, int(text))
        changed = (window_ms != self._window_ms)
        self._set_window_field_ms(window_ms)
        if changed and self._live and self._be.mode == self.MODE:
            self._be.set_window(self._window_ms * 1000)
        self._update_window_suggestion()

    def _update_window_suggestion(self):
        f_shift  = abs(self._sp_offset.value())
        sug      = suggest_window(f_shift)
        current_us = self._window_ms * 1000
        freq_str = fmt_freq(f_shift) if f_shift > 0 else "---"
        if current_us == WINDOW_OPTIONS_US[sug]:
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
        self._sp_width.setFixedHeight(34)
        self._sp_width.setMinimumWidth(120)
        self._sp_width.setFont(_mono_font(12, bold=True))
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
        lock        = self._cb_lock.isChecked()
        phase_deg   = self._sp_phase.value() if self._sp_phase is not None else 0.0
        preload     = self._phase_turns_to_preload(phase_deg / 360.0) if lock else None
        self._be.apply_pulse(
            wc, offset_word, edge_lock=lock, preload=preload,
            edge_response=self._edge_response_bits(),
        )
        self._log(
            f"Apply  width={self._sp_width.value():.2f}%  "
            f"shift={actual_hz:+.6f} Hz  offset={offset_word:+d}"
            + (f"  edge-locked  phase={phase_deg:.1f}°" if lock else "")
        )

    def get_params(self) -> dict:
        return {
            "freq_shift_hz": self._sp_offset.value(),
            "duty_cycle_pct": self._sp_width.value(),
            "output_mode": self._output_mode,
            "edge_lock": self._cb_lock.isChecked(),
            "edge_lock_response": self._edge_response.currentText().lower(),
            "phase_offset_deg": self._sp_phase.value() if self._sp_phase is not None else 0.0,
        }


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
        self._sp_n.setFixedHeight(34)
        self._sp_n.setMinimumWidth(80)
        self._sp_n.setFont(_mono_font(12, bold=True))
        self._sp_n.setStyleSheet(_spin_style())
        self._sp_n.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_n, 0, 3)
        return self._sp_n

    def _be_set_control(self, ctrl: int):
        self._be.set_control_harmonic(ctrl)

    def _be_upload(self, c_src: str, bit_src: Optional[str]):
        self._be.upload_harmonic(c_src, bit_src)

    # The square-wave output rises at the MSB (2^47) crossing, not the 2^48
    # carry, so harmonic phase-offset preload is shifted a half turn.
    def _phase_turns_to_preload(self, turns: float) -> int:
        return harmonic_phase_offset_to_preload(turns)

    def _preload_to_phase_turns(self, word: int) -> float:
        return harmonic_preload_to_phase_offset(word)

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
        lock        = self._cb_lock.isChecked()
        phase_deg   = self._sp_phase.value() if self._sp_phase is not None else 0.0
        preload     = self._phase_turns_to_preload(phase_deg / 360.0) if lock else None
        self._be.apply_harmonic(
            n, offset_word, edge_lock=lock, preload=preload,
            edge_response=self._edge_response_bits(),
        )
        self._log(
            f"Apply  N={n}  shift={actual_hz:+.6f} Hz  "
            f"offset={offset_word:+d}"
            + (f"  edge-locked  phase={phase_deg:.1f}°" if lock else "")
        )

    def get_params(self) -> dict:
        return {
            "freq_shift_hz": self._sp_offset.value(),
            "harmonic_n": self._sp_n.value(),
            "output_mode": self._output_mode,
            "edge_lock": self._cb_lock.isChecked(),
            "edge_lock_response": self._edge_response.currentText().lower(),
            "phase_offset_deg": self._sp_phase.value() if self._sp_phase is not None else 0.0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# OscPanel — stepped strobe scan
#   The output pulse is held at a constant phase φ_k = start + k·step of the
#   input period for a dwell time, then advances; after n_steps points the
#   scan is done and the last phase is held until re-armed.
#
#   The FPGA re-anchors the NCO to the input on every rising edge
#   (edge-locked osc mode), so every sampled phase is referenced to the
#   input edge and does not drift with measurement error.
#   Requires a bitstream built from the current pulse_gen.sv.
# ─────────────────────────────────────────────────────────────────────────────

class OscPanel(QWidget):
    """Stepped strobe: single-shot phase scan over a fraction of the period."""

    sig_params_changed = Signal()

    def __init__(self, backend: SshBackend, log_fn: Callable[[str], None],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._be      = backend
        self._log_fn  = log_fn
        self._period_c = 0
        self._live     = False
        self._osc_active = False

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._do_apply)

        self._build_ui()

        backend.sig_connected.connect(self._on_connected)
        backend.sig_disconnected.connect(self._on_disconnected)
        backend.sig_status.connect(self._on_status)

    def _log(self, msg: str):
        self._log_fn(f"[Osc] {msg}")

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(_SP_SM, _SP_MD, _SP_SM, _SP_SM)
        root.setSpacing(_SP_LG)

        # ── Status tiles ────────────────────────────────────────────────────
        self._d_in   = BigDisplay("Input Frequency",  "measured input period", _ACCENT)
        self._d_prog = BigDisplay("Progress",         "step k / N",            _GREEN)
        self._d_scan = BigDisplay("Scan Range",       "start → end (% of T_in)", _AMBER)
        self._d_time = BigDisplay("Total Time",       "N × dwell",             _AMBER)

        for d in (self._d_in, self._d_prog, self._d_scan, self._d_time):
            d.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            d.setMinimumHeight(80)

        readouts = QFrame()
        readouts.setObjectName("oscReadoutGrid")
        readouts.setStyleSheet("QFrame#oscReadoutGrid { background: transparent; border: none; }")
        grid = QGridLayout(readouts)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(_SP_MD)
        grid.addWidget(self._d_in,   0, 0)
        grid.addWidget(self._d_prog, 0, 1)
        grid.addWidget(self._d_scan, 1, 0)
        grid.addWidget(self._d_time, 1, 1)
        for i in range(2):
            grid.setRowStretch(i, 1)
            grid.setColumnStretch(i, 1)
        root.addWidget(readouts, 1)

        # ── Controls ────────────────────────────────────────────────────────
        controls = _make_group("Strobe Scan Controls")
        controls.setMinimumHeight(220)
        cl = QHBoxLayout(controls)
        cl.setContentsMargins(_SP_LG, 12, _SP_LG, _SP_LG)
        cl.setSpacing(_SP_LG)

        left_col = QVBoxLayout()
        left_col.setSpacing(_SP_MD)

        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(14)

        def _spin(lo, hi, dec, step, suffix, val, width=160):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setDecimals(dec)
            s.setSingleStep(step)
            s.setSuffix(suffix)
            s.setValue(val)
            s.setFixedHeight(34)
            s.setMinimumWidth(width)
            s.setFont(_mono_font(12, bold=True))
            s.setStyleSheet(_spin_style())
            return s

        # Row 0: start phase
        fields.addWidget(_dim_label("Start:"), 0, 0)
        self._sp_start = _spin(-100.0, 100.0, 3, 1.0, " %", 0.0)
        self._sp_start.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_start, 0, 1)

        # Row 0 col 2-3: Width %
        fields.addWidget(_dim_label("Width:", 80), 0, 2)
        self._sp_width = _spin(0.1, 99.9, 2, 1.0, " %", 1.0, 120)
        self._sp_width.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_width, 0, 3)

        # Row 1: step size
        fields.addWidget(_dim_label("Step:"), 1, 0)
        self._sp_step = _spin(0.001, 100.0, 3, 0.1, " %", 1.0)
        self._sp_step.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_step, 1, 1)

        # Row 1 col 2-3: number of steps
        fields.addWidget(_dim_label("N steps:", 80), 1, 2)
        self._sp_nsteps = QSpinBox()
        self._sp_nsteps.setRange(1, 1_000_000)
        self._sp_nsteps.setValue(10)
        self._sp_nsteps.setFixedHeight(34)
        self._sp_nsteps.setMinimumWidth(120)
        self._sp_nsteps.setFont(_mono_font(12, bold=True))
        self._sp_nsteps.setStyleSheet(_spin_style())
        self._sp_nsteps.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_nsteps, 1, 3)

        # Row 2: dwell per point
        fields.addWidget(_dim_label("Dwell:"), 2, 0)
        self._sp_dwell = _spin(0.01, 34_000.0, 2, 10.0, " ms", 100.0)
        self._sp_dwell.valueChanged.connect(self._param_changed)
        fields.addWidget(self._sp_dwell, 2, 1)

        # Row 3: auto-apply
        auto_row = QHBoxLayout()
        self._cb_auto = QCheckBox("Auto-Apply")
        self._cb_auto.setChecked(False)
        self._cb_auto.setFont(_mono_font(10))
        self._cb_auto.setStyleSheet(_checkbox_style())
        auto_row.addWidget(self._cb_auto)
        auto_row.addStretch()
        fields.addLayout(auto_row, 3, 0, 1, 4)

        # Row 4: derived detail label
        self._lbl_detail = QLabel()
        self._lbl_detail.setFont(_mono_font(9))
        self._lbl_detail.setWordWrap(True)
        self._lbl_detail.setStyleSheet(f"color: {_MUTED}; background: transparent;")
        fields.addWidget(self._lbl_detail, 4, 0, 1, 4)
        fields.setColumnStretch(1, 1)

        left_col.addLayout(fields)
        cl.addLayout(left_col, 1)

        # ── Action column ────────────────────────────────────────────────────
        actions = QVBoxLayout()
        actions.setSpacing(8)

        self._btn_apply = QPushButton("Start / Re-arm\nCtrl+Return")
        self._btn_apply.setFixedWidth(190)
        self._btn_apply.setMinimumHeight(60)
        self._btn_apply.setFont(_mono_font(12, bold=True))
        self._btn_apply.setStyleSheet(_btn_style(_GREEN))
        self._btn_apply.clicked.connect(self._do_apply)
        self._btn_apply.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        actions.addWidget(self._btn_apply)

        self._btn_stop = QPushButton("Stop scan")
        self._btn_stop.setFixedWidth(190)
        self._btn_stop.setFixedHeight(34)
        self._btn_stop.setStyleSheet(_btn_style(_RED))
        self._btn_stop.clicked.connect(self._do_stop)
        actions.addWidget(self._btn_stop)

        self._btn_upload = QPushButton("Upload && compile\nPulse mode")
        self._btn_upload.setFixedWidth(190)
        self._btn_upload.setFixedHeight(44)
        self._btn_upload.setStyleSheet(_btn_style(_ACCENT))
        self._btn_upload.clicked.connect(self._do_upload)
        actions.addWidget(self._btn_upload)
        actions.addStretch()
        cl.addLayout(actions)

        root.addWidget(controls)
        self._update_detail()
        self._set_controls_enabled(False)

    # ── helpers ────────────────────────────────────────────────────────────────

    def _set_controls_enabled(self, enabled: bool):
        for w in (self._sp_start, self._sp_step, self._sp_nsteps, self._sp_dwell,
                  self._sp_width, self._cb_auto, self._btn_apply, self._btn_stop):
            w.setEnabled(enabled)

    def _update_detail(self):
        start_pct = self._sp_start.value()
        step_pct = self._sp_step.value()
        n = self._sp_nsteps.value()
        dwell_ms = self._sp_dwell.value()
        end_pct = start_pct + (n - 1) * step_pct
        total_s = n * dwell_ms / 1e3
        dwell_c = dwell_s_to_cycles(dwell_ms / 1e3)
        self._lbl_detail.setText(
            f"dwell = {dwell_c} clks    "
            f"scan: {start_pct:.3f}% → {end_pct:.3f}% of T_in in {n} steps    "
            f"total = {fmt_dur(total_s)}"
        )
        self._d_scan.set_data(
            f"{start_pct:.2f} → {end_pct:.2f} %",
            f"step {step_pct:.3f}% of T_in", _AMBER,
        )
        self._d_time.set_data(fmt_dur(total_s), f"{n} × {dwell_ms:.2f} ms", _AMBER)

    # ── backend slots ──────────────────────────────────────────────────────────

    @Slot()
    def _on_connected(self):
        self._live = True
        self._set_controls_enabled(True)

    @Slot(str)
    def _on_disconnected(self, _reason: str):
        self._live = False
        self._period_c = 0
        self._set_controls_enabled(False)
        self._d_in.set_data("---", "disconnected", _RED)

    @Slot(dict)
    def _on_status(self, d: dict):
        if int(d.get("harmonic_mode", 0)) != 0:
            return  # harmonic helper JSON, skip

        step_base = int(d.get("phase_step_base") or 0)
        if step_base > 0:
            self._period_c = (1 << PHASE_BITS) // step_base
            in_hz  = phase_to_hz(step_base)
            stable = bool(d.get("period_stable"))
            self._d_in.set_data(
                fmt_freq(in_hz),
                "stable" if stable else "acquiring …",
                _ACCENT if stable else _AMBER,
            )

        # Reflect hardware osc_mode state + scan progress
        ctrl = int(d.get("control") or 0)
        self._osc_active = bool((ctrl >> 4) & 1)
        if self._osc_active:
            k = int(d.get("step_index") or 0)
            n = int(d.get("n_steps") or 0)
            done = bool(int(d.get("strobe_done") or 0))
            running = bool(d.get("freerun_active"))
            if done:
                self._d_prog.set_data(f"{n} / {n}", "DONE — holding last phase", _GREEN)
            elif running:
                self._d_prog.set_data(f"{k + 1} / {n}", "scanning", _AMBER)
            else:
                self._d_prog.set_data(f"0 / {n}", "armed — measuring input", _AMBER)
        else:
            self._d_prog.set_data("---", "scan not armed", _MUTED)

    # ── parameter controls ─────────────────────────────────────────────────────

    def _param_changed(self, *_):
        self._update_detail()
        if self._live and self._cb_auto.isChecked():
            self._debounce.start()
        self.sig_params_changed.emit()

    # ── actions ────────────────────────────────────────────────────────────────

    def apply(self):
        self._do_apply()

    def _do_apply(self):
        if not self._live:
            return
        start_frac = self._sp_start.value() / 100.0
        step_frac  = self._sp_step.value() / 100.0
        n_steps    = self._sp_nsteps.value()
        dwell_ms   = self._sp_dwell.value()
        width_pct  = self._sp_width.value()

        preload   = phase_offset_to_preload(start_frac)
        step_word = strobe_step_word(step_frac)
        dwell_c   = dwell_s_to_cycles(dwell_ms / 1e3)
        period    = self._period_c if self._period_c > 0 else 1000
        wc        = duty_to_cycles(width_pct / 100.0, period)

        # Sanity-check against the measured input, when available.
        if self._period_c > 0:
            min_step = 2.0 / self._period_c
            if step_frac < min_step:
                self._log(
                    f"WARNING: step = {step_frac*100:.4f}% is below the NCO/edge "
                    f"resolution ({min_step*100:.4f}% of T_in at this f_in); "
                    "adjacent points will be indistinguishable."
                )
            t_in_s = self._period_c / CLK_HZ
            if dwell_ms / 1e3 < 3 * t_in_s:
                self._log(
                    f"WARNING: dwell {dwell_ms:.2f} ms < 3 input periods "
                    f"({3 * t_in_s * 1e3:.2f} ms) — too few pulses per point."
                )
            # A level at exactly phase 0 (pulse coincident with the input
            # edge) can lose its pulses: the edge re-anchor snap preempts the
            # NCO carry every period when the measured f_in is slightly low.
            tick = 1.0 / self._period_c
            bad = [k for k in range(n_steps)
                   if min((start_frac + k * step_frac) % 1.0,
                          1.0 - (start_frac + k * step_frac) % 1.0) < tick]
            if bad:
                self._log(
                    f"WARNING: point(s) {bad} land within one NCO tick of "
                    "phase 0 — pulses may vanish there; offset the start "
                    "phase slightly."
                )
        end_pct = (start_frac + (n_steps - 1) * step_frac) * 100.0
        if end_pct > 100.0:
            self._log(
                f"WARNING: scan end {end_pct:.2f}% exceeds 100% of T_in — the "
                "phase wraps around into the next period."
            )

        self._be.apply_osc(wc, dwell_c, preload, step_word, n_steps)
        self._log(
            f"Arm  start={start_frac*100:.3f}%  step={step_frac*100:.3f}%  "
            f"n={n_steps}  dwell={dwell_ms:.2f} ms ({dwell_c} clks)  "
            f"preload={preload}  step_word={step_word}"
        )

    def _do_stop(self):
        if not self._live:
            return
        self._be.disable_osc()
        self._log("Strobe scan disabled.")

    def _do_upload(self):
        here    = Path(__file__).resolve().parent
        c_src   = os.fspath(here / "rp_ctl.c")
        bit_src = os.fspath(here / "red_pitaya_top.bit.bin")
        if not Path(c_src).exists():
            self._log(f"ERROR: {c_src} not found")
            return
        self._be.upload_pulse(c_src, Path(bit_src) if Path(bit_src).exists() else None)
        self._log("Upload queued.")

    def get_params(self) -> dict:
        return {
            "start_pct":  self._sp_start.value(),
            "step_pct":   self._sp_step.value(),
            "n_steps":    self._sp_nsteps.value(),
            "dwell_ms":   self._sp_dwell.value(),
            "width_pct":  self._sp_width.value(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# MainWindow — shared connection + tab host + shared log
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    sig_update_done = Signal(str, bool)
    _STATE_FILE = _default_state_file()

    def __init__(self):
        super().__init__()
        self.setObjectName("rpDarkWorkbench")
        self.setWindowTitle("Red Pitaya TTL Frequency Control")
        self.setMinimumSize(1040, 900)
        _apply_app_icon(self)

        self._state_sequence = 0
        self._last_status: Optional[dict] = None
        self._be = SshBackend(self)
        self._be.sig_connected.connect(self._on_connected)
        self._be.sig_disconnected.connect(self._on_disconnected)
        self._be.sig_log.connect(self._log)
        self._be.sig_error.connect(self._on_error)
        self._be.sig_mode_changed.connect(self._on_mode_changed)
        self._be.sig_status.connect(self._on_state_status)

        self._poll = QTimer(self)
        self._poll.setInterval(700)
        self._poll.timeout.connect(self._be.poll)
        # Match the poll cadence to the hardware measurement window so we don't
        # re-read the same registers far faster than they can update.
        self._be.sig_status.connect(self._adapt_poll_interval)

        self._build_ui()
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background: {_BG};
                color: {_TEXT};
            }}
            QToolTip {{
                background: {_SURFACE};
                color: {_TEXT};
                border: 1px solid {_BORDER};
                padding: 4px 6px;
            }}
        """)

        act = QAction(self)
        act.setShortcut(QKeySequence("Ctrl+Return"))
        act.triggered.connect(self._active_panel_apply)
        self.addAction(act)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)
        root.setContentsMargins(_SP_XL, _SP_LG, _SP_XL, _SP_LG)
        root.setSpacing(_SP_MD)

        root.addWidget(self._build_connection())

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {_BORDER};
                border-radius: 10px;
                background: {_PANEL};
                padding: 6px;
            }}
            QTabBar::tab {{
                background: {_FIELD};
                color: {_DIM};
                border: 1px solid {_BORDER};
                border-bottom: none;
                border-radius: 8px 8px 0 0;
                padding: 9px 24px;
                font-family: {_MONO};
                font-size: 11px;
                font-weight: 600;
            }}
            QTabBar::tab:selected {{
                background: {_SURFACE};
                color: {_TEXT};
                border-color: {_ACCENT};
                border-bottom: 1px solid {_SURFACE};
            }}
            QTabBar::tab:hover {{ background: {_SURFACE2}; color: {_TEXT}; }}
        """)

        self._pulse_panel    = PulsePanel(self._be, self._log)
        self._harmonic_panel = HarmonicPanel(self._be, self._log)
        self._osc_panel      = OscPanel(self._be, self._log)

        self._tabs.addTab(self._pulse_panel,    "Pulse / Freq-Shift")
        self._tabs.addTab(self._harmonic_panel, "Harmonic Generator")
        self._tabs.addTab(self._osc_panel,      "Strobe Scan")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self._tabs, 1)

        shared = QWidget()
        shared.setObjectName("rpSharedTools")
        shared.setStyleSheet("QWidget#rpSharedTools { background: transparent; }")
        shared_lay = QVBoxLayout(shared)
        shared_lay.setContentsMargins(0, 0, 0, 0)
        shared_lay.setSpacing(_SP_MD)
        shared_lay.addWidget(self._build_trigger())
        shared_lay.addWidget(self._build_log())
        root.addWidget(shared)
        self._write_state_file()

    def _do_git_update(self):
        here = Path(__file__).resolve().parent
        try:
            branches = _fetch_remote_branches(here)
        except Exception as exc:
            self._on_update_done(f"ERROR: {exc}", False)
            return
        if not branches:
            self._on_update_done("ERROR: No remote branches found", False)
            return

        choice, ok = QInputDialog.getItem(
            self,
            "Select branch",
            "Remote branch:",
            branches,
            0,
            False,
        )
        if not ok:
            return

        self._btn_update.setEnabled(False)
        self._lbl_update_status.setText(f"Updating {choice}…")
        self._lbl_update_status.setStyleSheet(f"color: {_ACCENT}; background: transparent;")

        def _run():
            try:
                msg, restart_needed = _run_git_update(here, choice)
                self.sig_update_done.emit(msg, restart_needed)
            except Exception as exc:
                self.sig_update_done.emit(f"ERROR: {exc}", False)

        threading.Thread(target=_run, name="git-pull", daemon=True).start()

    def _restart_app(self):
        script = os.fspath(Path(__file__).resolve())
        subprocess.Popen([sys.executable, script, *sys.argv[1:]], cwd=os.fspath(_APP_DIR))
        app = QApplication.instance()
        if app is not None:
            app.quit()

    @Slot(str, bool)
    def _on_update_done(self, msg: str, restart_needed: bool):
        self._btn_update.setEnabled(True)
        ok = not msg.startswith("ERROR")
        color = _GREEN if ok else _RED
        short = msg.split("\n")[0][:80]
        self._lbl_update_status.setText(short)
        self._lbl_update_status.setStyleSheet(f"color: {color}; background: transparent;")
        self._log(f"[Update] {msg}")
        if ok and restart_needed:
            box = QMessageBox(self)
            box.setWindowTitle("Update")
            box.setText("What's new:")
            box.setInformativeText(f"{msg}\n\nRestart now?")
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.button(QMessageBox.StandardButton.Yes).setText("Restart now")
            box.button(QMessageBox.StandardButton.No).setText("Later")
            if box.exec() == QMessageBox.StandardButton.Yes:
                self._restart_app()

    def _build_connection(self) -> QGroupBox:
        g = QFrame()
        g.setObjectName("rpWorkbenchHeader")
        g.setStyleSheet(f"""
            QFrame#rpWorkbenchHeader {{
                background: {_PANEL};
                border: 1px solid {_BORDER};
                border-radius: 10px;
            }}
        """)
        outer = QVBoxLayout(g)
        outer.setContentsMargins(_SP_LG, _SP_MD, _SP_LG, _SP_MD)
        outer.setSpacing(_SP_SM)

        top_row = QHBoxLayout()
        top_row.setSpacing(_SP_MD)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)

        title = QLabel("Red Pitaya TTL")
        title.setObjectName("rpWorkbenchTitle")
        title.setFont(QFont("Arial", 15, QFont.Bold))
        title.setStyleSheet(f"color: {_WHITE}; background: transparent;")
        title_col.addWidget(title)

        subtitle = QLabel("FPGA pulse and harmonic generator")
        subtitle.setFont(_mono_font(9))
        subtitle.setStyleSheet(f"color: {_MUTED}; background: transparent;")
        title_col.addWidget(subtitle)
        top_row.addLayout(title_col)
        top_row.addStretch()

        self._lbl_status = QLabel("Disconnected")
        self._lbl_status.setFont(_mono_font(10))
        self._lbl_status.setStyleSheet(
            f"color: {_RED}; background: {_RED}18; border: 1px solid {_RED}; "
            "border-radius: 999px; padding: 4px 10px;"
        )
        top_row.addWidget(self._lbl_status)

        self._lbl_mode = QLabel("mode: -")
        self._lbl_mode.setFont(_mono_font(9))
        self._lbl_mode.setStyleSheet(
            f"color: {_DIM}; background: {_FIELD}; border: 1px solid {_BORDER}; "
            "border-radius: 999px; padding: 4px 10px;"
        )
        top_row.addWidget(self._lbl_mode)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.VLine)
        sep2.setStyleSheet(f"color: {_BORDER};")
        top_row.addWidget(sep2)

        self._btn_update = QPushButton("Update")
        self._btn_update.setFixedHeight(30)
        self._btn_update.setStyleSheet(_btn_style(_DIM))
        self._btn_update.clicked.connect(self._do_git_update)
        top_row.addWidget(self._btn_update)

        self._lbl_update_status = QLabel()
        self._lbl_update_status.setFont(_mono_font(9))
        self._lbl_update_status.setStyleSheet(f"color: {_DIM}; background: transparent;")
        top_row.addWidget(self._lbl_update_status)
        outer.addLayout(top_row)

        row = QHBoxLayout()
        row.setSpacing(_SP_MD)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.HLine)
        sep1.setStyleSheet(f"color: {_BORDER};")
        outer.addWidget(sep1)

        self._w_host = QLineEdit("rp-f06a51.local")
        self._w_host.setFixedWidth(170)
        self._w_port = QLineEdit("22");    self._w_port.setFixedWidth(55)
        self._w_user = QLineEdit("root");  self._w_user.setFixedWidth(70)
        self._w_key  = QLineEdit();        self._w_key.setPlaceholderText("SSH key (optional)")
        self._w_key.setMinimumWidth(190)

        btn_key = QPushButton("…"); btn_key.setFixedWidth(28)
        btn_key.clicked.connect(self._pick_key)

        self._btn_conn = QPushButton("Connect")
        self._btn_conn.setFixedWidth(104)
        self._btn_conn.clicked.connect(self._toggle_connect)

        for w, lbl in ((self._w_host, "Host:"), (self._w_port, "Port:"),
                       (self._w_user, "User:"), (self._w_key, "Key:")):
            cap = QLabel(lbl)
            cap.setFont(_mono_font(9))
            cap.setStyleSheet(f"color: {_DIM}; background: transparent;")
            row.addWidget(cap)
            row.addWidget(w)

        row.addWidget(btn_key)
        row.addWidget(self._btn_conn)
        row.addStretch()
        outer.addLayout(row)

        self.sig_update_done.connect(self._on_update_done)

        for le in (self._w_host, self._w_port, self._w_user, self._w_key):
            le.setStyleSheet(_le_style())
        for b in (self._btn_conn, btn_key):
            b.setStyleSheet(_btn_style())
        return g

    def _build_trigger(self) -> QGroupBox:
        """DIO2 free-running square wave — independent of NCO mode."""
        g = _make_group("DIO2 Trigger Output  (independent free-running square wave)")
        row = QHBoxLayout(g)
        row.setContentsMargins(_SP_LG, _SP_SM, _SP_LG, _SP_MD)
        row.setSpacing(_SP_MD)

        lbl = QLabel("Frequency:")
        lbl.setFont(_mono_font(10, bold=True))
        lbl.setStyleSheet(f"color: {_DIM}; background: transparent;")
        row.addWidget(lbl)

        self._sp_trig = QDoubleSpinBox()
        self._sp_trig.setRange(0.0, MAX_SHIFT_HZ)
        self._sp_trig.setDecimals(6)
        self._sp_trig.setSingleStep(1.0)
        self._sp_trig.setSuffix(" Hz")
        self._sp_trig.setSpecialValueText("Off")
        self._sp_trig.setFixedHeight(30)
        self._sp_trig.setMinimumWidth(150)
        self._sp_trig.setFont(_mono_font(11, bold=True))
        self._sp_trig.setStyleSheet(_spin_style())
        self._sp_trig.setEnabled(False)
        row.addWidget(self._sp_trig)

        self._btn_trig_apply = QPushButton("Set")
        self._btn_trig_apply.setFixedWidth(68)
        self._btn_trig_apply.setFixedHeight(30)
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
        raw = d.get("trig_phase_step")
        if raw is None:
            return
        phase_step = int(raw)
        if phase_step == 0:
            self._lbl_trig_actual.setText("actual: Off")
        else:
            actual_hz = trig_phase_step_to_hz(phase_step)
            self._lbl_trig_actual.setText(f"actual: {actual_hz:.6f} Hz")

    def _on_trig_apply(self):
        f_hz = self._sp_trig.value()
        phase_step = trig_hz_to_phase_step(f_hz)
        actual_hz = trig_phase_step_to_hz(phase_step)
        self._be.set_trig(phase_step)
        self._log(f"[Trig] DIO2 → {actual_hz:.6f} Hz  (phase_step={phase_step})")

    def _build_log(self) -> QGroupBox:
        g = _make_group("Log")
        lay = QVBoxLayout(g)
        lay.setContentsMargins(_SP_SM, _SP_SM, _SP_SM, _SP_SM)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(88)
        self._log_box.setFont(_mono_font(9))
        self._log_box.setStyleSheet(
            f"background: {_FIELD}; color: {_DIM}; border: 1px solid {_BORDER}; border-radius: 7px;"
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
            self._lbl_status.setText("Connecting")
            self._lbl_status.setStyleSheet(
                f"color: {_ACCENT}; background: {_ACCENT}18; border: 1px solid {_ACCENT}; "
                "border-radius: 999px; padding: 4px 10px;"
            )
            self._be.start_connect(host, port, user, key, DEFAULT_BASE)

    @Slot()
    def _on_connected(self):
        self._last_status = None
        self._state_sequence += 1
        self._write_state_file()
        self._btn_conn.setText("Disconnect")
        self._btn_conn.setEnabled(True)
        self._lbl_status.setText("Connected")
        self._lbl_status.setStyleSheet(
            f"color: {_GREEN}; background: {_GREEN}18; border: 1px solid {_GREEN}; "
            "border-radius: 999px; padding: 4px 10px;"
        )
        self._poll.start()
        self._be.poll()
        self._log("Connected.")

    @Slot(str)
    def _on_disconnected(self, reason: str):
        self._last_status = None
        self._state_sequence += 1
        self._write_state_file()
        self._poll.stop()
        self._btn_conn.setText("Connect")
        self._btn_conn.setEnabled(True)
        self._lbl_status.setText("Disconnected")
        self._lbl_status.setStyleSheet(
            f"color: {_RED}; background: {_RED}18; border: 1px solid {_RED}; "
            "border-radius: 999px; padding: 4px 10px;"
        )
        self._lbl_mode.setText("mode: -")
        self._log(f"Disconnected: {reason}")

    @Slot(str)
    def _on_mode_changed(self, mode: str):
        self._lbl_mode.setText(f"mode: {mode}")
        color = _ACCENT if mode == "pulse" else _GREEN
        self._lbl_mode.setStyleSheet(
            f"color: {color}; background: {color}18; border: 1px solid {color}; "
            "border-radius: 999px; padding: 4px 10px;"
        )

    @Slot(int)
    def _on_tab_changed(self, idx: int):
        """Switch the poll helper when the user changes tabs."""
        mode = "harmonic" if idx == 1 else "pulse"  # osc (idx=2) uses pulse helper
        self._be.set_active_mode(mode)

    # ── Ctrl+Return routes to whichever tab is active ─────────────────────────

    def _active_panel_apply(self):
        panel = self._tabs.currentWidget()
        if isinstance(panel, (_NcoPanel, OscPanel)):
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

    @Slot(dict)
    def _on_state_status(self, status: dict):
        self._last_status = dict(status)
        self._state_sequence += 1
        self._write_state_file()

    def _write_state_file(self):
        state = _confirmed_state(
            self._last_status,
            connected=self._be.live,
            sequence=self._state_sequence,
        )
        try:
            self._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, indent=2))
            os.replace(tmp, self._STATE_FILE)
        except Exception as exc:
            self._log(f"[state] write failed: {exc}")

    @Slot(str)
    def _on_error(self, msg: str):
        self._lbl_status.setText("Error")
        self._lbl_status.setStyleSheet(
            f"color: {_RED}; background: {_RED}18; border: 1px solid {_RED}; "
            "border-radius: 999px; padding: 4px 10px;"
        )
        self._log(f"ERROR  {msg}")

    @Slot(str)
    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._log_box.append(f"[{ts}]  {msg}")
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())


# ─────────────────────────────────────────────────────────────────────────────

def main():
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("RP Combined Control")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
