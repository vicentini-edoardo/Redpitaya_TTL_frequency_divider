#!/usr/bin/env python3
"""
rp_gui.py — Red Pitaya PLL Remote Control GUI

Requirements: Python 3.8+, standard library only (tkinter, socket, threading,
              json, time, collections).
Usage: python3 rp_gui.py
"""

import tkinter as tk
import socket
import threading
import json
import time
from collections import deque

# ═══════════════════════════════════════════════════════════════════════════
#  Theme
# ═══════════════════════════════════════════════════════════════════════════

BG          = "#1a1a1a"    # window / outer background
BG_CARD     = "#222222"    # card / panel background
BG_FIELD    = "#2c2c2c"    # entry field
BG_TOPBAR   = "#141414"    # top connection bar
SEP         = "#3a3a3a"    # separator / border
FG          = "#e2e2e2"    # primary text
FG_DIM      = "#666666"    # secondary labels
FG_MID      = "#999999"    # unit / hint text
ACCENT      = "#4fc3f7"    # light-blue accent (section headers)
GREEN       = "#4caf50"
ORANGE      = "#ff9800"
RED_C       = "#f44336"
CHART_BG    = "#0d0d0d"

# Fonts — Segoe UI for labels, Courier New for numeric values
F_LABEL     = ("Segoe UI",     9)
F_LABEL_B   = ("Segoe UI",     9,  "bold")
F_SECTION   = ("Segoe UI",     9,  "bold")
F_MONO      = ("Courier New", 10)
F_BIGVAL    = ("Courier New", 28,  "bold")   # prominent value display
F_UNIT      = ("Segoe UI",    13)
F_READLBL   = ("Segoe UI",     9)
F_READVAL   = ("Courier New", 12,  "bold")
F_STEP      = ("Segoe UI",     8,  "bold")
F_BTN       = ("Segoe UI",     9)

# ═══════════════════════════════════════════════════════════════════════════
#  Layout
# ═══════════════════════════════════════════════════════════════════════════

OUTER_PAD   = 10           # window edge padding
COL_GAP     = 8            # gap between left and right columns
CTRL_W      = 370          # left column (controls) width
READ_W      = 360          # right column (readouts) width
WIN_W       = CTRL_W + COL_GAP + READ_W + OUTER_PAD * 2   # ≈ 728

# Chart
CHART_H     = 170
CHART_CL    = 42           # left margin (y-axis labels)
CHART_CR    = 12           # right margin
CHART_CT    = 12           # top margin
CHART_CB    = 22           # bottom margin (x-axis labels)
CHART_Y_MAX = 20.0         # ±degrees shown
CHART_DUR_S = 10           # seconds of history

TCP_TIMEOUT = 2.0


# ═══════════════════════════════════════════════════════════════════════════
#  Small widget helpers
# ═══════════════════════════════════════════════════════════════════════════

def _sep(parent, **kw):
    """Horizontal separator line."""
    return tk.Frame(parent, height=1, bg=SEP, **kw)


def _section_header(parent, text):
    """Coloured all-caps section label."""
    return tk.Label(parent, text=text.upper(), bg=BG_CARD,
                    fg=ACCENT, font=F_SECTION, anchor="w")


def _step_btn(parent, text, command):
    """Small ± step button."""
    return tk.Button(
        parent, text=text, command=command,
        bg=BG_FIELD, fg=FG, activebackground=SEP, activeforeground=FG,
        relief="flat", font=F_STEP, width=5, height=1,
        cursor="hand2", bd=0, highlightthickness=0,
    )


def _readout_row(parent, label_text, row):
    """One label + value pair in the readout panel. Returns the value Label."""
    tk.Label(parent, text=label_text, bg=BG_CARD, fg=FG_DIM,
             font=F_READLBL, anchor="w").grid(
        row=row, column=0, sticky="w", padx=(12, 6), pady=3)
    val = tk.Label(parent, text="—", bg=BG_CARD, fg=FG,
                   font=F_READVAL, anchor="w", width=16)
    val.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=3)
    return val


# ═══════════════════════════════════════════════════════════════════════════
#  Main application
# ═══════════════════════════════════════════════════════════════════════════

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Red Pitaya PLL")
        self.configure(bg=BG)
        self.resizable(False, False)

        # TCP state
        self._sock       = None
        self._conn_lock  = threading.Lock()
        self._running    = True
        self._connected  = False

        # Chart history: deque of (monotonic_time_s, phase_error_deg)
        self._chart_data = deque()

        self._build_ui()

        threading.Thread(target=self._tcp_loop, daemon=True).start()
        self._tick()  # start 100 ms UI refresh loop

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─────────────────────────────────────────────────────────────────────
    #  UI construction
    # ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_topbar()

        # Two-column middle section
        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="x", padx=OUTER_PAD, pady=(8, 0))
        mid.columnconfigure(0, weight=0)
        mid.columnconfigure(1, weight=0)

        left  = tk.Frame(mid, bg=BG, width=CTRL_W)
        right = tk.Frame(mid, bg=BG, width=READ_W)
        left.grid (row=0, column=0, sticky="n")
        right.grid(row=0, column=1, sticky="n", padx=(COL_GAP, 0))
        left.pack_propagate(False)
        right.pack_propagate(False)

        self._build_controls(left)
        self._build_readouts(right)
        self._build_chart()

    # ── Top bar ────────────────────────────────────────────────────────────

    def _build_topbar(self):
        bar = tk.Frame(self, bg=BG_TOPBAR)
        bar.pack(fill="x")

        inner = tk.Frame(bar, bg=BG_TOPBAR)
        inner.pack(side="left", padx=OUTER_PAD, pady=8)

        tk.Label(inner, text="Board IP", bg=BG_TOPBAR, fg=FG_DIM,
                 font=F_LABEL).grid(row=0, column=0, sticky="w")
        tk.Label(inner, text="Port", bg=BG_TOPBAR, fg=FG_DIM,
                 font=F_LABEL).grid(row=0, column=1, sticky="w", padx=(12, 0))

        self._ip_var   = tk.StringVar(value="192.168.1.100")
        self._port_var = tk.StringVar(value="5555")

        ip_e = tk.Entry(inner, textvariable=self._ip_var, width=17,
                        bg=BG_FIELD, fg=FG, insertbackground=FG,
                        relief="flat", font=F_MONO, bd=4)
        ip_e.grid(row=1, column=0, sticky="ew")

        port_e = tk.Entry(inner, textvariable=self._port_var, width=6,
                          bg=BG_FIELD, fg=FG, insertbackground=FG,
                          relief="flat", font=F_MONO, bd=4)
        port_e.grid(row=1, column=1, sticky="ew", padx=(12, 0))

        # Bind Enter on either field to connect
        ip_e.bind("<Return>",   lambda _e: self._toggle_connect())
        port_e.bind("<Return>", lambda _e: self._toggle_connect())

        # Connect button
        self._conn_btn = tk.Button(
            bar, text="Connect",
            command=self._toggle_connect,
            bg="#1a4a8a", fg="white",
            activebackground="#2a5aaa", activeforeground="white",
            relief="flat", font=F_BTN, width=12, cursor="hand2",
            bd=0, highlightthickness=0,
        )
        self._conn_btn.pack(side="left", padx=(16, 0))

        # Status pill
        self._conn_lbl = tk.Label(
            bar, text="  ●  DISCONNECTED  ",
            bg=BG_TOPBAR, fg=RED_C, font=F_LABEL_B,
        )
        self._conn_lbl.pack(side="left", padx=14)

    # ── Controls column ────────────────────────────────────────────────────

    def _build_controls(self, parent):
        self._build_phase_card(parent)
        tk.Frame(parent, height=8, bg=BG).pack()
        self._build_duty_card(parent)

    def _build_phase_card(self, parent):
        card = tk.Frame(parent, bg=BG_CARD, bd=0)
        card.pack(fill="x")

        # Header
        hdr = tk.Frame(card, bg=BG_CARD)
        hdr.pack(fill="x", padx=12, pady=(10, 2))
        _section_header(hdr, "Phase Shift").pack(side="left")
        tk.Label(hdr, text="degrees", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="right")

        _sep(card).pack(fill="x")

        # Big value display
        disp = tk.Frame(card, bg=BG_CARD)
        disp.pack(pady=(10, 0))
        self._phase_var     = tk.DoubleVar(value=0.0)
        self._phase_big_lbl = tk.Label(disp, text="  0.0", bg=BG_CARD, fg=FG,
                                       font=F_BIGVAL, width=7, anchor="e")
        self._phase_big_lbl.pack(side="left")
        tk.Label(disp, text="°", bg=BG_CARD, fg=FG_MID,
                 font=F_UNIT).pack(side="left", anchor="s", pady=(0, 6))

        # Slider
        sl_frame = tk.Frame(card, bg=BG_CARD)
        sl_frame.pack(fill="x", padx=12, pady=(6, 0))
        self._phase_slider = tk.Scale(
            sl_frame, from_=-360, to=360, resolution=0.1,
            orient="horizontal", variable=self._phase_var,
            bg=BG_CARD, fg=FG_DIM, troughcolor=BG_FIELD,
            highlightthickness=0, activebackground=ACCENT,
            showvalue=0, sliderlength=18, width=8,
            command=self._on_phase_slider,
        )
        self._phase_slider.pack(fill="x")

        # Range labels under slider
        rng = tk.Frame(card, bg=BG_CARD)
        rng.pack(fill="x", padx=14)
        tk.Label(rng, text="−360°", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="left")
        tk.Label(rng, text="+360°", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="right")

        # Step buttons
        steps = tk.Frame(card, bg=BG_CARD)
        steps.pack(pady=(8, 0))
        for delta, label in [(-90, "−90°"), (-10, "−10°"), (-1, "−1°"),
                              (+1, "+1°"), (+10, "+10°"), (+90, "+90°")]:
            _step_btn(steps, label,
                      lambda d=delta: self._step_phase(d)).pack(
                side="left", padx=2)

        # Direct-entry row
        entry_row = tk.Frame(card, bg=BG_CARD)
        entry_row.pack(pady=(8, 12))
        tk.Label(entry_row, text="Go to:", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="left", padx=(0, 6))
        self._phase_entry_var = tk.StringVar()
        e = tk.Entry(entry_row, textvariable=self._phase_entry_var, width=9,
                     bg=BG_FIELD, fg=FG, insertbackground=FG,
                     relief="flat", font=F_MONO, bd=4)
        e.pack(side="left")
        e.bind("<Return>", lambda _ev: self._apply_phase_entry())
        tk.Label(entry_row, text="°", bg=BG_CARD, fg=FG_MID,
                 font=F_LABEL).pack(side="left", padx=(2, 8))
        tk.Button(
            entry_row, text="Set", command=self._apply_phase_entry,
            bg=BG_FIELD, fg=FG, activebackground=SEP, activeforeground=FG,
            relief="flat", font=F_BTN, width=5, cursor="hand2",
            bd=0, highlightthickness=0,
        ).pack(side="left")

    def _build_duty_card(self, parent):
        card = tk.Frame(parent, bg=BG_CARD, bd=0)
        card.pack(fill="x")

        # Header
        hdr = tk.Frame(card, bg=BG_CARD)
        hdr.pack(fill="x", padx=12, pady=(10, 2))
        _section_header(hdr, "Duty Cycle").pack(side="left")
        tk.Label(hdr, text="percent", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="right")

        _sep(card).pack(fill="x")

        # Big value display
        disp = tk.Frame(card, bg=BG_CARD)
        disp.pack(pady=(10, 0))
        self._duty_pct_var  = tk.DoubleVar(value=50.0)
        self._duty_big_lbl  = tk.Label(disp, text=" 50.0", bg=BG_CARD, fg=FG,
                                       font=F_BIGVAL, width=7, anchor="e")
        self._duty_big_lbl.pack(side="left")
        tk.Label(disp, text="%", bg=BG_CARD, fg=FG_MID,
                 font=F_UNIT).pack(side="left", anchor="s", pady=(0, 6))

        # Slider
        sl_frame = tk.Frame(card, bg=BG_CARD)
        sl_frame.pack(fill="x", padx=12, pady=(6, 0))
        self._duty_slider = tk.Scale(
            sl_frame, from_=1, to=99, resolution=0.1,
            orient="horizontal", variable=self._duty_pct_var,
            bg=BG_CARD, fg=FG_DIM, troughcolor=BG_FIELD,
            highlightthickness=0, activebackground=ACCENT,
            showvalue=0, sliderlength=18, width=8,
            command=self._on_duty_slider,
        )
        self._duty_slider.pack(fill="x")

        # Range labels
        rng = tk.Frame(card, bg=BG_CARD)
        rng.pack(fill="x", padx=14)
        tk.Label(rng, text="1%", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="left")
        tk.Label(rng, text="99%", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="right")

        # Step buttons
        steps = tk.Frame(card, bg=BG_CARD)
        steps.pack(pady=(8, 0))
        for delta, label in [(-10, "−10%"), (-5, "−5%"), (-1, "−1%"),
                              (+1, "+1%"), (+5, "+5%"), (+10, "+10%")]:
            _step_btn(steps, label,
                      lambda d=delta: self._step_duty(d)).pack(
                side="left", padx=2)

        # Direct-entry row
        entry_row = tk.Frame(card, bg=BG_CARD)
        entry_row.pack(pady=(8, 12))
        tk.Label(entry_row, text="Go to:", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="left", padx=(0, 6))
        self._duty_entry_var = tk.StringVar()
        e = tk.Entry(entry_row, textvariable=self._duty_entry_var, width=9,
                     bg=BG_FIELD, fg=FG, insertbackground=FG,
                     relief="flat", font=F_MONO, bd=4)
        e.pack(side="left")
        e.bind("<Return>", lambda _ev: self._apply_duty_entry())
        tk.Label(entry_row, text="%", bg=BG_CARD, fg=FG_MID,
                 font=F_LABEL).pack(side="left", padx=(2, 8))
        tk.Button(
            entry_row, text="Set", command=self._apply_duty_entry,
            bg=BG_FIELD, fg=FG, activebackground=SEP, activeforeground=FG,
            relief="flat", font=F_BTN, width=5, cursor="hand2",
            bd=0, highlightthickness=0,
        ).pack(side="left")

    # ── Readouts column ────────────────────────────────────────────────────

    def _build_readouts(self, parent):
        # ── Signal ──────────────────────────────────────────────────────────
        sig = tk.Frame(parent, bg=BG_CARD)
        sig.pack(fill="x")
        _section_header(sig, "Signal").pack(anchor="w", padx=12, pady=(10, 4))
        _sep(sig).pack(fill="x")
        sig_grid = tk.Frame(sig, bg=BG_CARD)
        sig_grid.pack(fill="x", pady=4)
        self._freq_lbl = _readout_row(sig_grid, "Frequency",   0)
        self._lock_lbl = _readout_row(sig_grid, "Lock Status", 1)
        self._up_lbl   = _readout_row(sig_grid, "Uptime",      2)

        tk.Frame(parent, height=8, bg=BG).pack()

        # ── Phase ────────────────────────────────────────────────────────────
        ph = tk.Frame(parent, bg=BG_CARD)
        ph.pack(fill="x")
        _section_header(ph, "Phase").pack(anchor="w", padx=12, pady=(10, 4))
        _sep(ph).pack(fill="x")
        ph_grid = tk.Frame(ph, bg=BG_CARD)
        ph_grid.pack(fill="x", pady=4)
        self._tgt_lbl  = _readout_row(ph_grid, "Target",  0)
        self._app_lbl  = _readout_row(ph_grid, "Applied", 1)

        # Phase error row with colour indicator dot
        tk.Label(ph_grid, text="Error", bg=BG_CARD, fg=FG_DIM,
                 font=F_READLBL, anchor="w").grid(
            row=2, column=0, sticky="w", padx=(12, 6), pady=3)
        err_cell = tk.Frame(ph_grid, bg=BG_CARD)
        err_cell.grid(row=2, column=1, sticky="w", padx=(0, 12), pady=3)
        self._err_lbl = tk.Label(err_cell, text="—", bg=BG_CARD, fg=FG,
                                 font=F_READVAL, anchor="w", width=10)
        self._err_lbl.pack(side="left")
        self._err_dot = tk.Label(err_cell, text="●", bg=BG_CARD,
                                 fg=FG_DIM, font=F_READLBL)
        self._err_dot.pack(side="left", padx=(4, 0))

        tk.Frame(parent, height=8, bg=BG).pack()

        # ── Output ───────────────────────────────────────────────────────────
        out = tk.Frame(parent, bg=BG_CARD)
        out.pack(fill="x")
        _section_header(out, "Output").pack(anchor="w", padx=12, pady=(10, 4))
        _sep(out).pack(fill="x")
        out_grid = tk.Frame(out, bg=BG_CARD)
        out_grid.pack(fill="x", pady=4)
        self._duty_ro_lbl = _readout_row(out_grid, "Duty Cycle", 0)

    # ── Chart ──────────────────────────────────────────────────────────────

    def _build_chart(self):
        cw = WIN_W - OUTER_PAD * 2

        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="x", padx=OUTER_PAD, pady=(8, OUTER_PAD))

        hdr = tk.Frame(outer, bg=BG_CARD)
        hdr.pack(fill="x")
        _section_header(hdr, "Phase Error History").pack(
            side="left", padx=12, pady=(8, 4))
        tk.Label(hdr, text=f"last {CHART_DUR_S} s", bg=BG_CARD,
                 fg=FG_DIM, font=F_LABEL).pack(
            side="right", padx=12, pady=(8, 4))
        _sep(hdr).pack(fill="x")

        canvas_frame = tk.Frame(outer, bg=BG_CARD)
        canvas_frame.pack(fill="x")

        self._chart_w = cw
        self._canvas = tk.Canvas(
            canvas_frame, width=cw, height=CHART_H,
            bg=CHART_BG, highlightthickness=0,
        )
        self._canvas.pack(padx=0, pady=(4, 8))
        self._draw_chart_static()

    # ─────────────────────────────────────────────────────────────────────
    #  Control event handlers
    # ─────────────────────────────────────────────────────────────────────

    def _on_phase_slider(self, _val=None):
        deg = self._phase_var.get()
        self._phase_big_lbl.config(text=f"{deg:+.1f}" if deg != 0 else "  0.0")
        self._send_phase(deg)

    def _step_phase(self, delta: float):
        new = max(-360.0, min(360.0, round(self._phase_var.get() + delta, 1)))
        self._phase_var.set(new)
        self._on_phase_slider()

    def _apply_phase_entry(self):
        try:
            deg = float(self._phase_entry_var.get())
            deg = max(-360.0, min(360.0, round(deg, 1)))
            self._phase_var.set(deg)
            self._on_phase_slider()
            self._phase_entry_var.set("")
        except ValueError:
            self._phase_entry_var.set("")

    def _on_duty_slider(self, _val=None):
        pct = self._duty_pct_var.get()
        self._duty_big_lbl.config(text=f"{pct:5.1f}")
        self._send_duty(pct / 100.0)

    def _step_duty(self, delta: float):
        new = max(1.0, min(99.0, round(self._duty_pct_var.get() + delta, 1)))
        self._duty_pct_var.set(new)
        self._on_duty_slider()

    def _apply_duty_entry(self):
        try:
            pct = float(self._duty_entry_var.get())
            pct = max(1.0, min(99.0, round(pct, 1)))
            self._duty_pct_var.set(pct)
            self._on_duty_slider()
            self._duty_entry_var.set("")
        except ValueError:
            self._duty_entry_var.set("")

    # ─────────────────────────────────────────────────────────────────────
    #  Connection
    # ─────────────────────────────────────────────────────────────────────

    def _toggle_connect(self):
        if self._connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        ip = self._ip_var.get().strip()
        try:
            port = int(self._port_var.get().strip())
        except ValueError:
            return
        self._conn_lbl.config(text="  ●  CONNECTING…  ", fg=ORANGE)
        threading.Thread(target=self._do_connect, args=(ip, port),
                         daemon=True).start()

    def _disconnect(self):
        with self._conn_lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
        self._connected = False

    def _on_close(self):
        self._running = False
        self._disconnect()
        self.destroy()

    # ─────────────────────────────────────────────────────────────────────
    #  TCP helpers
    # ─────────────────────────────────────────────────────────────────────

    def _do_connect(self, ip, port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(TCP_TIMEOUT)
            s.connect((ip, port))
            s.settimeout(None)
            with self._conn_lock:
                self._sock = s
            self._connected = True
        except Exception as exc:
            self.after(0, lambda e=exc: self._conn_lbl.config(
                text=f"  ●  {e}  ", fg=RED_C))

    def _send_raw(self, msg: str) -> bool:
        with self._conn_lock:
            s = self._sock
        if s is None:
            return False
        try:
            s.sendall((msg + "\n").encode())
            return True
        except Exception:
            self._disconnect()
            return False

    def _send_phase(self, deg: float):
        self._send_raw(f"SET_PHASE {deg:.1f}")

    def _send_duty(self, duty: float):
        self._send_raw(f"SET_DUTY {duty:.4f}")

    # ─────────────────────────────────────────────────────────────────────
    #  TCP receive loop (daemon thread)
    # ─────────────────────────────────────────────────────────────────────

    def _tcp_loop(self):
        buf = ""
        while self._running:
            with self._conn_lock:
                s = self._sock
            if s is None:
                time.sleep(0.05)
                continue
            try:
                s.settimeout(1.0)
                chunk = s.recv(4096).decode(errors="replace")
                if not chunk:
                    self._disconnect()
                    continue
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._process_line(line.strip())
            except socket.timeout:
                continue
            except Exception:
                self._disconnect()

    def _process_line(self, line: str):
        if not line.startswith("STATUS "):
            return
        try:
            data = json.loads(line[7:])
        except json.JSONDecodeError:
            return
        ts = time.monotonic()
        self._chart_data.append((ts, data.get("phase_error", 0.0)))
        # Evict data older than the chart window
        cutoff = ts - CHART_DUR_S
        while self._chart_data and self._chart_data[0][0] < cutoff:
            self._chart_data.popleft()
        self.after(0, lambda d=data: self._update_readouts(d))

    # ─────────────────────────────────────────────────────────────────────
    #  UI refresh (100 ms tick)
    # ─────────────────────────────────────────────────────────────────────

    def _tick(self):
        self._refresh_conn_ui()
        self._redraw_chart()
        self.after(100, self._tick)

    def _refresh_conn_ui(self):
        if self._connected:
            self._conn_lbl.config(text="  ●  CONNECTED  ", fg=GREEN)
            self._conn_btn.config(text="Disconnect",
                                  bg="#6a1a1a",
                                  activebackground="#8a2a2a")
        else:
            if "CONNECTING" not in self._conn_lbl.cget("text"):
                self._conn_lbl.config(text="  ●  DISCONNECTED  ", fg=RED_C)
            self._conn_btn.config(text="Connect",
                                  bg="#1a4a8a",
                                  activebackground="#2a5aaa")

    def _update_readouts(self, d: dict):
        freq   = d.get("freq",          0.0)
        tgt    = d.get("phase_target",  0.0)
        app    = d.get("phase_applied", 0.0)
        err    = d.get("phase_error",   0.0)
        duty   = d.get("duty",          0.5) * 100.0
        locked = d.get("locked",        False)
        uptime = d.get("uptime_s",      0)

        self._freq_lbl.config(text=f"{freq:.2f} Hz")
        self._tgt_lbl.config(text=f"{tgt:+.1f}°")
        self._app_lbl.config(text=f"{app:+.1f}°")

        # Phase error: colour both the value and the indicator dot
        abs_err = abs(err)
        if abs_err < 2.0:
            err_col = GREEN
        elif abs_err < 5.0:
            err_col = ORANGE
        else:
            err_col = RED_C
        self._err_lbl.config(text=f"{err:+.2f}°", fg=err_col)
        self._err_dot.config(fg=err_col)

        self._duty_ro_lbl.config(text=f"{duty:.1f}%")

        if locked:
            self._lock_lbl.config(text="LOCKED", fg=GREEN)
        else:
            self._lock_lbl.config(text="NO SIGNAL", fg=RED_C)

        mins, secs = divmod(uptime, 60)
        self._up_lbl.config(text=f"{mins:02d}:{secs:02d}")

    # ─────────────────────────────────────────────────────────────────────
    #  Chart
    # ─────────────────────────────────────────────────────────────────────

    def _draw_chart_static(self):
        """Draw fixed chart elements: grid, axes, labels."""
        c = self._canvas
        c.delete("static")
        cw = self._chart_w

        cl, cr, ct, cb = CHART_CL, CHART_CR, CHART_CT, CHART_CB
        plot_x0 = cl
        plot_x1 = cw - cr
        plot_y0 = ct
        plot_y1 = CHART_H - cb
        cy = (plot_y0 + plot_y1) // 2

        def y_for(deg):
            return cy - (deg / CHART_Y_MAX) * (cy - plot_y0)

        # ── Horizontal grid lines ──
        grid_spec = [
            (CHART_Y_MAX,   FG_DIM,   1, ()),
            (5.0,           "#3a2a2a", 1, (4, 4)),
            (2.0,           "#2a3a2a", 1, (4, 4)),
            (0.0,           "#444444", 1, ()),
            (-2.0,          "#2a3a2a", 1, (4, 4)),
            (-5.0,          "#3a2a2a", 1, (4, 4)),
            (-CHART_Y_MAX,  FG_DIM,   1, ()),
        ]
        for deg, col, w, dash in grid_spec:
            y = y_for(deg)
            kw = dict(fill=col, width=w, tags="static")
            if dash:
                c.create_line(plot_x0, y, plot_x1, y, dash=dash, **kw)
            else:
                c.create_line(plot_x0, y, plot_x1, y, **kw)

        # ── Y-axis labels ──
        for deg, label in [(CHART_Y_MAX, f"+{CHART_Y_MAX:.0f}"),
                           (5.0,  "+5"), (2.0, "+2"),
                           (0.0,  "0"),
                           (-2.0, "−2"), (-5.0, "−5"),
                           (-CHART_Y_MAX, f"−{CHART_Y_MAX:.0f}")]:
            c.create_text(cl - 4, y_for(deg),
                          text=label, fill=FG_DIM,
                          font=("Courier New", 8), anchor="e", tags="static")

        # ── X-axis labels ──
        c.create_text(plot_x0, CHART_H - 4,
                      text=f"−{CHART_DUR_S}s", fill=FG_DIM,
                      font=("Courier New", 8), anchor="sw", tags="static")
        c.create_text(plot_x1, CHART_H - 4,
                      text="now", fill=FG_DIM,
                      font=("Courier New", 8), anchor="se", tags="static")

        # ── Threshold zone tints (subtle fill between ±2) ──
        y_pos2 = y_for(2.0)
        y_neg2 = y_for(-2.0)
        c.create_rectangle(plot_x0, y_pos2, plot_x1, y_neg2,
                            fill="#0d1a0d", outline="", tags="static")

    def _redraw_chart(self):
        c = self._canvas
        c.delete("trace")

        data = list(self._chart_data)
        if len(data) < 2:
            return

        cw   = self._chart_w
        now  = time.monotonic()
        cl, cr, ct, cb = CHART_CL, CHART_CR, CHART_CT, CHART_CB
        plot_x0 = cl
        plot_x1 = cw - cr
        plot_y0 = ct
        plot_y1 = CHART_H - cb
        cy      = (plot_y0 + plot_y1) / 2
        pw      = plot_x1 - plot_x0
        ph_half = cy - plot_y0

        t_start = now - CHART_DUR_S

        def to_xy(ts, err):
            x = plot_x0 + (ts - t_start) / CHART_DUR_S * pw
            y = cy - (err / CHART_Y_MAX) * ph_half
            y = max(plot_y0, min(plot_y1, y))
            return x, y

        # Draw each segment coloured by error at the end point
        for i in range(1, len(data)):
            t0, e0 = data[i - 1]
            t1, e1 = data[i]
            x0, y0 = to_xy(t0, e0)
            x1, y1 = to_xy(t1, e1)
            abs_e = abs(e1)
            color = GREEN if abs_e < 2.0 else (ORANGE if abs_e < 5.0 else RED_C)
            c.create_line(x0, y0, x1, y1, fill=color, width=2, tags="trace")

        # Live value annotation at the right edge
        if data:
            _, last_err = data[-1]
            abs_e = abs(last_err)
            col = GREEN if abs_e < 2.0 else (ORANGE if abs_e < 5.0 else RED_C)
            _, ly = to_xy(now, last_err)
            c.create_text(plot_x1 + cr - 2, ly,
                          text=f"{last_err:+.1f}°", fill=col,
                          font=("Courier New", 8, "bold"),
                          anchor="e", tags="trace")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
