#!/usr/bin/env python3
"""
scope_gui.py — Red Pitaya PLL Oscilloscope View

Plots the raw IN1 ADC samples (cyan) and the reconstructed OUT1 square wave
(green) to verify phase locking visually.

Requirements: Python 3.8+, standard library only.
Usage: python3 gui/scope_gui.py
"""

import tkinter as tk
import socket
import threading
import json
import time
import math

# ── Theme ────────────────────────────────────────────────────────────────────
BG        = "#1a1a1a"
BG_CARD   = "#222222"
BG_TOPBAR = "#141414"
BG_FIELD  = "#2c2c2c"
SEP       = "#3a3a3a"
FG        = "#e2e2e2"
FG_DIM    = "#666666"
CYAN      = "#4fc3f7"    # IN1 trace
GREEN_C   = "#4caf50"    # OUT1 trace
RED_C     = "#f44336"
ORANGE    = "#ff9800"
CHART_BG  = "#0d0d0d"

F_LABEL = ("Segoe UI",     9)
F_MONO  = ("Courier New", 10)
F_BTN   = ("Segoe UI",     9)
F_INFO  = ("Courier New", 11, "bold")

# ── Canvas dimensions ────────────────────────────────────────────────────────
CW   = 760    # canvas width
CH   = 260    # canvas height
CL   = 46     # left margin  (voltage axis labels)
CR   = 12     # right margin
CT   = 14     # top margin
CB   = 28     # bottom margin (time axis labels)

TCP_TIMEOUT      = 2.0
SCOPE_INTERVAL_S = 0.4    # request new scope data every 400 ms


class ScopeApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Red Pitaya — Scope View")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._sock       = None
        self._conn_lock  = threading.Lock()
        self._running    = True
        self._connected  = False

        self._last_scope  = None   # parsed SCOPE dict
        self._last_status = None   # parsed STATUS dict

        self._build_ui()
        threading.Thread(target=self._tcp_loop, daemon=True).start()
        self._tick()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        bar = tk.Frame(self, bg=BG_TOPBAR)
        bar.pack(fill="x")

        inner = tk.Frame(bar, bg=BG_TOPBAR)
        inner.pack(side="left", padx=10, pady=8)

        tk.Label(inner, text="Board IP", bg=BG_TOPBAR, fg=FG_DIM,
                 font=F_LABEL).grid(row=0, column=0, sticky="w")
        tk.Label(inner, text="Port", bg=BG_TOPBAR, fg=FG_DIM,
                 font=F_LABEL).grid(row=0, column=1, sticky="w", padx=(12, 0))

        self._ip_var   = tk.StringVar(value="rp-f06a51.local")
        self._port_var = tk.StringVar(value="5555")

        tk.Entry(inner, textvariable=self._ip_var, width=17,
                 bg=BG_FIELD, fg=FG, insertbackground=FG,
                 relief="flat", font=F_MONO, bd=4).grid(row=1, column=0)
        tk.Entry(inner, textvariable=self._port_var, width=6,
                 bg=BG_FIELD, fg=FG, insertbackground=FG,
                 relief="flat", font=F_MONO, bd=4).grid(
            row=1, column=1, padx=(12, 0))

        self._conn_btn = tk.Button(
            bar, text="Connect", command=self._toggle_connect,
            bg="#1a4a8a", fg="white", relief="flat", font=F_BTN,
            width=10, cursor="hand2", bd=0, highlightthickness=0,
            activebackground="#2a5aaa", activeforeground="white")
        self._conn_btn.pack(side="left", padx=(16, 0))

        self._status_lbl = tk.Label(bar, text="  ●  DISCONNECTED  ",
                                    bg=BG_TOPBAR, fg=RED_C, font=F_LABEL)
        self._status_lbl.pack(side="left", padx=12)

        # Legend (right side)
        leg = tk.Frame(bar, bg=BG_TOPBAR)
        leg.pack(side="right", padx=12)
        tk.Label(leg, text="━  IN1 (measured)",
                 bg=BG_TOPBAR, fg=CYAN, font=F_LABEL).pack(anchor="e")
        tk.Label(leg, text="━  OUT1 (reconstructed)",
                 bg=BG_TOPBAR, fg=GREEN_C, font=F_LABEL).pack(anchor="e")

        # Canvas
        cf = tk.Frame(self, bg=BG_CARD)
        cf.pack(fill="x", padx=10, pady=(8, 0))

        self._canvas = tk.Canvas(cf, width=CW, height=CH,
                                 bg=CHART_BG, highlightthickness=0)
        self._canvas.pack(pady=4)
        self._draw_static()

        # Info bar
        info = tk.Frame(self, bg=BG_CARD)
        info.pack(fill="x", padx=10, pady=(0, 10))

        self._freq_lbl  = tk.Label(info, text="freq:  —",
                                   bg=BG_CARD, fg=FG_DIM, font=F_INFO)
        self._phase_lbl = tk.Label(info, text="phase: —",
                                   bg=BG_CARD, fg=FG_DIM, font=F_INFO)
        self._duty_lbl  = tk.Label(info, text="duty:  —",
                                   bg=BG_CARD, fg=FG_DIM, font=F_INFO)
        self._lock_lbl  = tk.Label(info, text="lock:  —",
                                   bg=BG_CARD, fg=FG_DIM, font=F_INFO)
        for lbl in (self._freq_lbl, self._phase_lbl,
                    self._duty_lbl, self._lock_lbl):
            lbl.pack(side="left", padx=(12, 16), pady=6)

    def _draw_static(self):
        """Draw fixed axes — voltage grid lines and labels."""
        c = self._canvas
        c.delete("static")

        pw = CW - CL - CR
        ph = CH - CT - CB
        x0, x1 = CL, CL + pw
        cy = CT + ph // 2

        # Voltage grid: +1, 0, -1
        for v, label in [(1.0, "+1 V"), (0.0, "0 V"), (-1.0, "−1 V")]:
            y = cy - int(v * ph / 2.4)
            dash = () if v == 0.0 else (4, 4)
            c.create_line(x0, y, x1, y, fill="#2a2a2a", dash=dash, tags="static")
            c.create_text(x0 - 4, y, text=label, fill=FG_DIM,
                          font=("Courier New", 8), anchor="e", tags="static")

        # Border
        c.create_rectangle(x0, CT, x1, CT + ph,
                            outline=SEP, width=1, tags="static")

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _redraw(self):
        c = self._canvas
        c.delete("trace")

        scope = self._last_scope
        if scope is None or len(scope.get("v", [])) < 2:
            c.create_text(CW // 2, CH // 2,
                          text="Waiting for scope data…", fill=FG_DIM,
                          font=("Segoe UI", 11), tags="trace")
            return

        samples = scope["v"]
        dt_us   = scope["dt_us"]
        freq    = scope["freq"]
        phase   = scope["phase"]
        duty    = scope["duty"]
        n       = len(samples)

        pw = CW - CL - CR
        ph = CH - CT - CB
        x0 = CL
        cy = CT + ph / 2
        total_us = n * dt_us

        def to_x(i):
            return x0 + i / (n - 1) * pw

        def v_to_y(v):
            y = cy - v * (ph / 2.4)
            return max(CT, min(CT + ph, y))

        # ── IN1 trace (cyan, raw ADC) ──────────────────────────────────────────
        pts = []
        for i, v in enumerate(samples):
            pts.extend([to_x(i), v_to_y(v)])
        if len(pts) >= 4:
            c.create_line(*pts, fill=CYAN, width=1, tags="trace", smooth=False)

        # ── OUT1 trace (green, reconstructed square wave) ──────────────────────
        if freq > 0:
            T_us           = 1e6 / freq
            phase_off_us   = phase / 360.0 * T_us
            pts_out = []
            prev_v = None
            for i in range(n):
                t           = i * dt_us + phase_off_us
                t_in_cycle  = math.fmod(t, T_us)
                if t_in_cycle < 0:
                    t_in_cycle += T_us
                v = 0.9 if t_in_cycle < duty * T_us else -0.9
                x = to_x(i)
                y = v_to_y(v)
                # Insert vertical edge when polarity flips
                if prev_v is not None and prev_v != v:
                    pts_out.extend([x, v_to_y(prev_v), x, y])
                else:
                    pts_out.extend([x, y])
                prev_v = v
            if len(pts_out) >= 4:
                c.create_line(*pts_out, fill=GREEN_C, width=2,
                              tags="trace", smooth=False)

        # ── Time axis labels ───────────────────────────────────────────────────
        bot = CT + ph + 6
        c.create_text(x0,       bot, text="0",
                      fill=FG_DIM, font=("Courier New", 8),
                      anchor="n", tags="trace")
        c.create_text(x0 + pw // 2, bot,
                      text=f"{total_us / 2:.0f} µs",
                      fill=FG_DIM, font=("Courier New", 8),
                      anchor="n", tags="trace")
        c.create_text(x0 + pw, bot,
                      text=f"{total_us:.0f} µs",
                      fill=FG_DIM, font=("Courier New", 8),
                      anchor="n", tags="trace")

        # ── Phase offset annotation ────────────────────────────────────────────
        if freq > 0 and phase != 0:
            T_us = 1e6 / freq
            off_us = abs(phase / 360.0 * T_us)
            sign = "+" if phase >= 0 else "−"
            c.create_text(x0 + pw - 4, CT + 4,
                          text=f"Δφ = {sign}{abs(phase):.1f}°  ({off_us:.1f} µs)",
                          fill=ORANGE, font=("Courier New", 9, "bold"),
                          anchor="ne", tags="trace")

    # ── Info bar ──────────────────────────────────────────────────────────────

    def _update_info(self, d):
        freq   = d.get("freq",          0.0)
        phase  = d.get("phase_applied", 0.0)
        duty   = d.get("duty",          0.5) * 100.0
        locked = d.get("locked",        False)

        self._freq_lbl.config(text=f"freq:  {freq:.2f} Hz")
        self._phase_lbl.config(text=f"phase: {phase:+.1f}°")
        self._duty_lbl.config(text=f"duty:  {duty:.1f}%")
        if locked:
            self._lock_lbl.config(text="lock:  LOCKED",    fg=GREEN_C)
        else:
            self._lock_lbl.config(text="lock:  NO SIGNAL", fg=RED_C)

    # ── Connection ────────────────────────────────────────────────────────────

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
        self._status_lbl.config(text="  ●  CONNECTING…  ", fg=ORANGE)
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
            self.after(0, lambda e=exc: self._status_lbl.config(
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

    # ── TCP loop (daemon thread) ──────────────────────────────────────────────

    def _tcp_loop(self):
        buf = ""
        last_req = 0.0
        while self._running:
            with self._conn_lock:
                s = self._sock
            if s is None:
                time.sleep(0.05)
                continue

            # Periodically request a fresh scope snapshot
            now = time.monotonic()
            if now - last_req >= SCOPE_INTERVAL_S:
                self._send_raw("GET_SCOPE")
                last_req = now

            try:
                s.settimeout(0.5)
                chunk = s.recv(65536).decode(errors="replace")
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
        if line.startswith("SCOPE "):
            try:
                data = json.loads(line[6:])
                self._last_scope = data
            except json.JSONDecodeError:
                pass
        elif line.startswith("STATUS "):
            try:
                data = json.loads(line[7:])
                self._last_status = data
                self.after(0, lambda d=data: self._update_info(d))
            except json.JSONDecodeError:
                pass

    # ── 200 ms UI tick ────────────────────────────────────────────────────────

    def _tick(self):
        if self._connected:
            self._status_lbl.config(text="  ●  CONNECTED  ", fg=GREEN_C)
            self._conn_btn.config(text="Disconnect",
                                  bg="#6a1a1a", activebackground="#8a2a2a")
        else:
            if "CONNECTING" not in self._status_lbl.cget("text"):
                self._status_lbl.config(text="  ●  DISCONNECTED  ", fg=RED_C)
            self._conn_btn.config(text="Connect",
                                  bg="#1a4a8a", activebackground="#2a5aaa")
        self._redraw()
        self.after(200, self._tick)


def main():
    app = ScopeApp()
    app.mainloop()


if __name__ == "__main__":
    main()
