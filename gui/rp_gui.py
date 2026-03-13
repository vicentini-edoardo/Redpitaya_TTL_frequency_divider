#!/usr/bin/env python3
"""
rp_gui.py — Red Pitaya PLL Remote Control GUI

Connects to rp_pll (running on the board) via TCP, sends SET_PHASE / SET_DUTY
commands, and displays live status including a rolling phase-error chart.

Requirements: Python 3.8+, standard library only (tkinter, socket, threading,
              json, time, collections).

Usage: python3 rp_gui.py
"""

import tkinter as tk
from tkinter import ttk
import socket
import threading
import json
import time
from collections import deque

# ── Appearance constants ────────────────────────────────────────────────────
BG          = "#1e1e1e"
BG2         = "#2a2a2a"
BG3         = "#333333"
FG          = "#e0e0e0"
FG_DIM      = "#888888"
ACCENT      = "#4fc3f7"
GREEN       = "#66bb6a"
ORANGE      = "#ffa726"
RED         = "#ef5350"
FONT_MONO   = ("Courier New", 11)
FONT_LABEL  = ("Segoe UI", 10)
FONT_TITLE  = ("Segoe UI", 12, "bold")
FONT_VALUE  = ("Courier New", 14, "bold")

# ── Chart settings ──────────────────────────────────────────────────────────
CHART_DURATION_S = 10       # seconds of history shown
CHART_W          = 700
CHART_H          = 140
CHART_PAD        = 10
CHART_Y_RANGE    = 20.0     # ±Y degrees shown

# ── TCP settings ────────────────────────────────────────────────────────────
TCP_TIMEOUT      = 2.0
RECONNECT_DELAY  = 3.0


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Red Pitaya PLL Control")
        self.configure(bg=BG)
        self.resizable(False, False)

        # Connection state
        self._sock       = None
        self._conn_lock  = threading.Lock()
        self._running    = True
        self._connected  = False

        # Chart data: deque of (timestamp_s, phase_error_deg)
        self._chart_data = deque()

        # Build UI
        self._build_ui()

        # Start background threads
        threading.Thread(target=self._tcp_loop, daemon=True).start()

        # Periodic UI refresh
        self._schedule_refresh()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self):
        pad = dict(padx=8, pady=4)

        # ── Top bar: connection ──────────────────────────────────────────────
        conn_frame = tk.Frame(self, bg=BG2, relief="flat", bd=0)
        conn_frame.pack(fill="x", padx=6, pady=(6, 0))

        tk.Label(conn_frame, text="IP:", bg=BG2, fg=FG, font=FONT_LABEL).pack(side="left", padx=(8, 2))
        self._ip_var = tk.StringVar(value="192.168.1.100")
        tk.Entry(conn_frame, textvariable=self._ip_var, width=16,
                 bg=BG3, fg=FG, insertbackground=FG, relief="flat",
                 font=FONT_MONO).pack(side="left", padx=(0, 8))

        tk.Label(conn_frame, text="Port:", bg=BG2, fg=FG, font=FONT_LABEL).pack(side="left", padx=(0, 2))
        self._port_var = tk.StringVar(value="5555")
        tk.Entry(conn_frame, textvariable=self._port_var, width=6,
                 bg=BG3, fg=FG, insertbackground=FG, relief="flat",
                 font=FONT_MONO).pack(side="left", padx=(0, 12))

        self._conn_btn = tk.Button(conn_frame, text="Connect",
                                   command=self._toggle_connect,
                                   bg="#1565c0", fg="white", relief="flat",
                                   font=FONT_LABEL, width=10,
                                   activebackground="#1976d2", activeforeground="white")
        self._conn_btn.pack(side="left")

        self._conn_status = tk.Label(conn_frame, text="● DISCONNECTED",
                                     bg=BG2, fg=RED, font=FONT_LABEL)
        self._conn_status.pack(side="left", padx=12)

        # ── Controls ─────────────────────────────────────────────────────────
        ctrl_frame = tk.Frame(self, bg=BG)
        ctrl_frame.pack(fill="x", padx=6, pady=6)

        # Phase control
        phase_frame = tk.LabelFrame(ctrl_frame, text="Phase Shift (°)",
                                    bg=BG, fg=ACCENT, font=FONT_LABEL,
                                    relief="groove", bd=1)
        phase_frame.pack(side="left", fill="both", expand=True, padx=(0, 4))

        self._phase_var = tk.DoubleVar(value=0.0)
        self._phase_slider = tk.Scale(
            phase_frame, from_=-360, to=360, resolution=0.1,
            orient="horizontal", variable=self._phase_var,
            bg=BG, fg=FG, troughcolor=BG3, highlightthickness=0,
            activebackground=ACCENT, showvalue=0,
            command=self._on_phase_slider)
        self._phase_slider.pack(fill="x", padx=4, pady=(0, 2))

        phase_entry_frame = tk.Frame(phase_frame, bg=BG)
        phase_entry_frame.pack()
        tk.Label(phase_entry_frame, text="Value:", bg=BG, fg=FG_DIM,
                 font=FONT_LABEL).pack(side="left")
        self._phase_entry = tk.Entry(phase_entry_frame, width=8,
                                     bg=BG3, fg=FG, insertbackground=FG,
                                     relief="flat", font=FONT_MONO,
                                     textvariable=self._phase_var)
        self._phase_entry.pack(side="left", padx=4)
        self._phase_entry.bind("<Return>", self._on_phase_entry)

        # Duty control
        duty_frame = tk.LabelFrame(ctrl_frame, text="Duty Cycle (%)",
                                   bg=BG, fg=ACCENT, font=FONT_LABEL,
                                   relief="groove", bd=1)
        duty_frame.pack(side="left", fill="both", expand=True, padx=(4, 0))

        self._duty_pct_var = tk.DoubleVar(value=50.0)
        self._duty_slider = tk.Scale(
            duty_frame, from_=1, to=99, resolution=0.1,
            orient="horizontal", variable=self._duty_pct_var,
            bg=BG, fg=FG, troughcolor=BG3, highlightthickness=0,
            activebackground=ACCENT, showvalue=0,
            command=self._on_duty_slider)
        self._duty_slider.pack(fill="x", padx=4, pady=(0, 2))

        duty_entry_frame = tk.Frame(duty_frame, bg=BG)
        duty_entry_frame.pack()
        tk.Label(duty_entry_frame, text="Value:", bg=BG, fg=FG_DIM,
                 font=FONT_LABEL).pack(side="left")
        self._duty_entry = tk.Entry(duty_entry_frame, width=8,
                                    bg=BG3, fg=FG, insertbackground=FG,
                                    relief="flat", font=FONT_MONO,
                                    textvariable=self._duty_pct_var)
        self._duty_entry.pack(side="left", padx=4)
        self._duty_entry.bind("<Return>", self._on_duty_entry)

        # ── Live readouts ─────────────────────────────────────────────────────
        read_frame = tk.LabelFrame(self, text="Live Readouts",
                                   bg=BG, fg=ACCENT, font=FONT_LABEL,
                                   relief="groove", bd=1)
        read_frame.pack(fill="x", padx=6, pady=(0, 6))

        # Two-column grid of readouts
        labels = [
            ("Frequency",     "freq_lbl"),
            ("Target Phase",  "tgt_lbl"),
            ("Applied Phase", "app_lbl"),
            ("Phase Error",   "err_lbl"),
            ("Duty Cycle",    "duty_lbl"),
            ("Lock Status",   "lock_lbl"),
            ("Uptime",        "up_lbl"),
        ]
        for i, (text, attr) in enumerate(labels):
            row, col = divmod(i, 2)
            tk.Label(read_frame, text=text + ":", bg=BG, fg=FG_DIM,
                     font=FONT_LABEL, anchor="e", width=14).grid(
                row=row, column=col * 2, sticky="e", padx=(8, 2), pady=2)
            lbl = tk.Label(read_frame, text="—", bg=BG, fg=FG,
                           font=FONT_VALUE, anchor="w", width=16)
            lbl.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 12), pady=2)
            setattr(self, "_" + attr, lbl)

        # ── Phase error chart ─────────────────────────────────────────────────
        chart_outer = tk.LabelFrame(self, text="Phase Error History (10 s)",
                                    bg=BG, fg=ACCENT, font=FONT_LABEL,
                                    relief="groove", bd=1)
        chart_outer.pack(fill="x", padx=6, pady=(0, 6))

        self._canvas = tk.Canvas(chart_outer, width=CHART_W, height=CHART_H,
                                 bg="#111111", highlightthickness=0)
        self._canvas.pack(padx=4, pady=4)
        self._draw_chart_axes()

    # ── Event handlers ───────────────────────────────────────────────────────

    def _on_phase_slider(self, _val=None):
        self._send_phase(self._phase_var.get())

    def _on_phase_entry(self, _event=None):
        try:
            deg = float(self._phase_entry.get())
            deg = max(-360.0, min(360.0, deg))
            self._phase_var.set(round(deg, 1))
            self._send_phase(deg)
        except ValueError:
            pass

    def _on_duty_slider(self, _val=None):
        self._send_duty(self._duty_pct_var.get() / 100.0)

    def _on_duty_entry(self, _event=None):
        try:
            pct = float(self._duty_entry.get())
            pct = max(1.0, min(99.0, pct))
            self._duty_pct_var.set(round(pct, 1))
            self._send_duty(pct / 100.0)
        except ValueError:
            pass

    def _toggle_connect(self):
        if self._connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        ip   = self._ip_var.get().strip()
        try:
            port = int(self._port_var.get().strip())
        except ValueError:
            return
        threading.Thread(target=self._do_connect, args=(ip, port), daemon=True).start()

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

    # ── TCP helpers ──────────────────────────────────────────────────────────

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
            self.after(0, lambda: self._conn_status.config(
                text=f"● {exc}", fg=RED))

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

    # ── TCP receive loop (background thread) ─────────────────────────────────

    def _tcp_loop(self):
        """Background thread: reads STATUS lines from socket."""
        buf = ""
        while self._running:
            with self._conn_lock:
                s = self._sock
            if s is None:
                time.sleep(0.1)
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
        # Trim old data
        cutoff = ts - CHART_DURATION_S
        while self._chart_data and self._chart_data[0][0] < cutoff:
            self._chart_data.popleft()
        # Marshal UI update to main thread
        self.after(0, lambda d=data: self._update_readouts(d))

    # ── UI update ────────────────────────────────────────────────────────────

    def _schedule_refresh(self):
        self._update_conn_ui()
        self._redraw_chart()
        self.after(100, self._schedule_refresh)

    def _update_conn_ui(self):
        if self._connected:
            self._conn_status.config(text="● CONNECTED", fg=GREEN)
            self._conn_btn.config(text="Disconnect")
        else:
            self._conn_status.config(text="● DISCONNECTED", fg=RED)
            self._conn_btn.config(text="Connect")

    def _update_readouts(self, d: dict):
        freq  = d.get("freq", 0.0)
        tgt   = d.get("phase_target", 0.0)
        app   = d.get("phase_applied", 0.0)
        err   = d.get("phase_error", 0.0)
        duty  = d.get("duty", 0.5) * 100.0
        locked = d.get("locked", False)
        uptime = d.get("uptime_s", 0)

        self._freq_lbl.config(text=f"{freq:.2f} Hz")
        self._tgt_lbl.config(text=f"{tgt:.1f}°")
        self._app_lbl.config(text=f"{app:.1f}°")

        # Phase error with colour coding
        abs_err = abs(err)
        if abs_err < 2.0:
            err_color = GREEN
        elif abs_err < 5.0:
            err_color = ORANGE
        else:
            err_color = RED
        self._err_lbl.config(text=f"{err:.2f}°", fg=err_color)

        self._duty_lbl.config(text=f"{duty:.1f}%")
        if locked:
            self._lock_lbl.config(text="LOCKED", fg=GREEN)
        else:
            self._lock_lbl.config(text="NO SIGNAL", fg=RED)
        self._up_lbl.config(text=f"{uptime} s")

    # ── Chart drawing ─────────────────────────────────────────────────────────

    def _draw_chart_axes(self):
        c = self._canvas
        c.delete("axes")
        w, h = CHART_W, CHART_H
        px, py = CHART_PAD, CHART_PAD

        # Zero line
        cy = h // 2
        c.create_line(px, cy, w - px, cy, fill="#444444", dash=(4, 4), tags="axes")

        # Y labels
        c.create_text(px - 2, cy, text="0°", fill=FG_DIM,
                      font=("Courier New", 9), anchor="e", tags="axes")
        c.create_text(px - 2, py, text=f"+{CHART_Y_RANGE:.0f}°",
                      fill=FG_DIM, font=("Courier New", 9), anchor="e", tags="axes")
        c.create_text(px - 2, h - py, text=f"-{CHART_Y_RANGE:.0f}°",
                      fill=FG_DIM, font=("Courier New", 9), anchor="e", tags="axes")

        # X labels
        c.create_text(px, h - 2, text=f"-{CHART_DURATION_S}s",
                      fill=FG_DIM, font=("Courier New", 9), anchor="sw", tags="axes")
        c.create_text(w - px, h - 2, text="now",
                      fill=FG_DIM, font=("Courier New", 9), anchor="se", tags="axes")

    def _redraw_chart(self):
        c = self._canvas
        c.delete("trace")

        data = list(self._chart_data)
        if len(data) < 2:
            return

        now   = time.monotonic()
        w, h  = CHART_W, CHART_H
        px, py = CHART_PAD, CHART_PAD
        chart_w = w - 2 * px
        chart_h = h - 2 * py
        cy = py + chart_h // 2

        def to_xy(ts, err):
            x = px + (ts - (now - CHART_DURATION_S)) / CHART_DURATION_S * chart_w
            y = cy - (err / CHART_Y_RANGE) * (chart_h / 2)
            y = max(py, min(h - py, y))
            return x, y

        # Draw segments coloured by error magnitude
        for i in range(1, len(data)):
            t0, e0 = data[i - 1]
            t1, e1 = data[i]
            x0, y0 = to_xy(t0, e0)
            x1, y1 = to_xy(t1, e1)
            color = GREEN if abs(e1) < 2.0 else RED
            c.create_line(x0, y0, x1, y1, fill=color, width=2, tags="trace")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
