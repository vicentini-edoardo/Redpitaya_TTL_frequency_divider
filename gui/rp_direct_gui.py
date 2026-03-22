#!/usr/bin/env python3
"""
rp_direct_gui.py — Red Pitaya TTL Duty-Cycle Controller

Runs DIRECTLY on the Red Pitaya board (requires the 'rp' Python module).
Reads a TTL square wave on IN1, measures its frequency, and generates a
TTL-compatible square wave on OUT1 at the same frequency with a
user-selectable duty cycle.

Usage (on the board):
    python3 rp_direct_gui.py

Requirements:
    - Python 3.8+, standard library only (tkinter, threading, time, math)
    - 'rp' module from /opt/redpitaya/lib/python/ (board-only)
    - X11 display (run locally or via SSH -X / VNC)
"""

import tkinter as tk
import threading
import time
import math

# ── Try importing the rp module (board-only) ─────────────────────────────────
try:
    import rp
    RP_AVAILABLE = True
except ImportError:
    RP_AVAILABLE = False

# ── ADC / measurement constants ──────────────────────────────────────────────
DECIMATION   = rp.RP_DEC_1024 if RP_AVAILABLE else 1024
# 125 MHz / 1024 = ~122 kSPS — good for 10 Hz–50 kHz signals
SAMPLE_RATE  = 125_000_000 / 1024
BUF_SIZE     = 16384
LOOP_SLEEP_S = 0.05      # 50 ms between acquisitions
MEDIAN_WIN   = 7         # frequency median filter window

# ── Theme ─────────────────────────────────────────────────────────────────────
BG        = "#1a1a1a"
BG_CARD   = "#222222"
BG_FIELD  = "#2c2c2c"
BG_TOPBAR = "#141414"
SEP       = "#3a3a3a"
FG        = "#e2e2e2"
FG_DIM    = "#666666"
FG_MID    = "#999999"
ACCENT    = "#4fc3f7"
GREEN     = "#4caf50"
ORANGE    = "#ff9800"
RED_C     = "#f44336"
CHART_BG  = "#0d0d0d"

F_LABEL  = ("Segoe UI",     9)
F_LABEL_B= ("Segoe UI",     9, "bold")
F_SECTION= ("Segoe UI",     9, "bold")
F_MONO   = ("Courier New", 10)
F_BIGVAL = ("Courier New", 20, "bold")
F_UNIT   = ("Segoe UI",    11)
F_RDLBL  = ("Segoe UI",     9)
F_RDVAL  = ("Courier New", 12, "bold")
F_STEP   = ("Segoe UI",     8, "bold")
F_BTN    = ("Segoe UI",     9)

OUTER_PAD = 10
WIN_W     = 540
CHART_H   = 100
CHART_DUR = 10.0   # seconds of frequency history shown


# ── Utility ───────────────────────────────────────────────────────────────────

def _sep_line(parent, **kw):
    return tk.Frame(parent, height=1, bg=SEP, **kw)


def _section_hdr(parent, text):
    return tk.Label(parent, text=text.upper(), bg=BG_CARD,
                    fg=ACCENT, font=F_SECTION, anchor="w")


def _step_btn(parent, text, command):
    return tk.Button(
        parent, text=text, command=command,
        bg=BG_FIELD, fg=FG, activebackground=SEP, activeforeground=FG,
        relief="flat", font=F_STEP, width=5, height=1,
        cursor="hand2", bd=0, highlightthickness=1,
        highlightbackground=BG_FIELD,
    )


def _readout_row(parent, label, row):
    tk.Label(parent, text=label, bg=BG_CARD, fg=FG_DIM,
             font=F_RDLBL, anchor="w").grid(
        row=row, column=0, sticky="w", padx=(12, 6), pady=3)
    val = tk.Label(parent, text="—", bg=BG_CARD, fg=FG,
                   font=F_RDVAL, anchor="w", width=18)
    val.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=3)
    return val


# ── Frequency measurement (pure Python, mirrors the C logic) ─────────────────

class MedianFilter:
    def __init__(self, n):
        self._win = [0.0] * n
        self._idx = 0
        self._full = False
        self._n = n

    def update(self, val):
        self._win[self._idx] = val
        self._idx = (self._idx + 1) % self._n
        if not self._full and self._idx == 0:
            self._full = True
        n = self._n if self._full else self._idx
        return sorted(self._win[:n])[n // 2]


def measure_freq(buf, sample_rate):
    """
    Measure frequency from a flat list/array of voltage samples.
    Returns (frequency_hz, edge_count).
    Uses adaptive hysteresis (30 % of amplitude) and sub-sample interpolation.
    """
    vmin = min(buf)
    vmax = max(buf)
    vmid = (vmax + vmin) * 0.5
    hyst = (vmax - vmin) * 0.3
    if hyst < 0.05:
        return 0.0, 0

    hi = vmid + hyst
    lo = vmid - hyst

    edge_count = 0
    first_edge = -1.0
    last_edge  = -1.0
    above = buf[0] > vmid

    for i in range(1, len(buf)):
        if not above and buf[i] > hi:
            frac = 0.0 if buf[i] == buf[i - 1] else \
                   (vmid - buf[i - 1]) / (buf[i] - buf[i - 1])
            edge = (i - 1) + frac
            if first_edge < 0:
                first_edge = edge
            last_edge = edge
            edge_count += 1
            above = True
        elif above and buf[i] < lo:
            above = False

    if edge_count < 2:
        return 0.0, edge_count

    spc = (last_edge - first_edge) / (edge_count - 1)
    return sample_rate / spc, edge_count


# ── Hardware worker (background thread) ──────────────────────────────────────

class HWWorker:
    """
    Runs in a daemon thread.
    Acquires ADC buffers from IN1, measures frequency, drives OUT1.
    Thread-safe state is exposed via simple Python attributes guarded by a Lock.
    """

    def __init__(self):
        self._lock      = threading.Lock()
        self._duty      = 0.5       # 0.0–1.0
        self._running   = False
        self._enabled   = False     # output on/off

        # Read-back state (written by worker thread, read by GUI thread)
        self.freq        = 0.0
        self.edge_count  = 0
        self.locked      = False
        self.uptime_s    = 0
        self.error_msg   = ""

        self._mf         = MedianFilter(MEDIAN_WIN)
        self._out_active = False    # True once generator was initialised

    # ── Setters (called from GUI thread) ─────────────────────────────────────

    def set_duty(self, duty: float):
        with self._lock:
            self._duty = max(0.01, min(0.99, duty))
            if RP_AVAILABLE and self._out_active:
                try:
                    rp.rp_GenDutyCycle(rp.RP_CH_1, float(self._duty))
                except Exception:
                    pass

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._enabled = enabled
            if RP_AVAILABLE and self._out_active:
                try:
                    if enabled:
                        rp.rp_GenOutEnable(rp.RP_CH_1)
                    else:
                        rp.rp_GenOutDisable(rp.RP_CH_1)
                except Exception:
                    pass

    # ── Main loop ─────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def stop(self):
        self._running = False

    def _run(self):
        if not RP_AVAILABLE:
            # Simulation mode: generate fake data so the GUI looks alive
            self._sim_run()
            return

        try:
            rp.rp_Init()
        except Exception as e:
            self.error_msg = f"rp_Init failed: {e}"
            return

        # Configure acquisition
        rp.rp_AcqReset()
        rp.rp_AcqSetDecimation(DECIMATION)
        rp.rp_AcqSetTriggerDelay(0)

        # Configure generator (PWM waveform on CH1)
        rp.rp_GenReset()
        rp.rp_GenWaveform(rp.RP_CH_1, rp.RP_WAVEFORM_PWM)
        rp.rp_GenAmp(rp.RP_CH_1, 1.0)
        rp.rp_GenOffset(rp.RP_CH_1, 0.0)
        rp.rp_GenPhase(rp.RP_CH_1, 0.0)
        rp.rp_GenFreq(rp.RP_CH_1, 1000.0)    # placeholder until first measurement
        with self._lock:
            rp.rp_GenDutyCycle(rp.RP_CH_1, float(self._duty))
            if self._enabled:
                rp.rp_GenOutEnable(rp.RP_CH_1)
        self._out_active = True

        t_start = time.monotonic()

        while self._running:
            # Acquire one buffer
            try:
                rp.rp_AcqStart()
                rp.rp_AcqSetTriggerSrc(rp.RP_TRIG_SRC_NOW)

                # Wait for trigger (up to 40 × 1 ms = 40 ms)
                state = rp.RP_TRIG_STATE_WAITING
                for _ in range(40):
                    time.sleep(0.001)
                    _, state = rp.rp_AcqGetTriggerState()
                    if state == rp.RP_TRIG_STATE_TRIGGERED:
                        break

                _, buf = rp.rp_AcqGetOldestDataV(rp.RP_CH_1, BUF_SIZE)
                rp.rp_AcqStop()
            except Exception as e:
                self.error_msg = str(e)
                time.sleep(0.5)
                continue

            # Measure frequency
            freq_raw, ec = measure_freq(buf, SAMPLE_RATE)
            locked = (ec >= 2 and freq_raw > 0.0)

            if locked:
                freq_filt = self._mf.update(freq_raw)
                # Apply to generator
                try:
                    rp.rp_GenFreq(rp.RP_CH_1, float(freq_filt))
                except Exception:
                    pass
            else:
                freq_filt = self.freq   # keep last good value

            with self._lock:
                duty_now = self._duty

            # Update public state (no lock needed — Python GIL covers these
            # simple attribute writes; GUI reads are non-critical)
            self.freq       = freq_filt if locked else 0.0
            self.edge_count = ec
            self.locked     = locked
            self.uptime_s   = int(time.monotonic() - t_start)

            time.sleep(LOOP_SLEEP_S)

        # Cleanup
        try:
            rp.rp_GenOutDisable(rp.RP_CH_1)
            rp.rp_AcqStop()
            rp.rp_Release()
        except Exception:
            pass

    def _sim_run(self):
        """Demo mode — fake a ~8 kHz signal so the GUI can be tested on a PC."""
        import random
        t_start = time.monotonic()
        base = 8000.0
        while self._running:
            jitter = random.gauss(0, 2.0)
            self.freq       = base + jitter
            self.edge_count = 20
            self.locked     = True
            self.uptime_s   = int(time.monotonic() - t_start)
            time.sleep(0.1)


# ── Main GUI ──────────────────────────────────────────────────────────────────

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Red Pitaya — TTL Duty Cycle Controller")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._hw      = HWWorker()
        self._running = True

        # Frequency history for the chart: list of (monotonic_time, freq_hz)
        self._freq_hist = []

        self._build_ui()
        self._hw.start()

        # Enable output by default
        self._output_enabled = True
        self._hw.set_enabled(True)

        self._tick()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        if not RP_AVAILABLE:
            warn = tk.Frame(self, bg="#3a1a00")
            warn.pack(fill="x")
            tk.Label(warn,
                     text="  ⚠  'rp' module not found — running in simulation mode  ",
                     bg="#3a1a00", fg=ORANGE, font=F_LABEL_B).pack(pady=4)

        self._build_duty_card()
        tk.Frame(self, height=8, bg=BG).pack()
        self._build_readouts()
        self._build_chart()

    # ── Duty cycle card ───────────────────────────────────────────────────────

    def _build_duty_card(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="x", padx=OUTER_PAD, pady=(OUTER_PAD, 0))

        card = tk.Frame(outer, bg=BG_CARD)
        card.pack(fill="x")

        # Header
        hdr = tk.Frame(card, bg=BG_CARD)
        hdr.pack(fill="x", padx=12, pady=(6, 2))
        _section_hdr(hdr, "Output Duty Cycle").pack(side="left")
        tk.Label(hdr, text="percent", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="right")

        _sep_line(card).pack(fill="x")

        # Big value display
        disp = tk.Frame(card, bg=BG_CARD)
        disp.pack(pady=(6, 0))
        self._duty_var    = tk.DoubleVar(value=50.0)
        self._duty_big    = tk.Label(disp, text=" 50.0", bg=BG_CARD, fg=FG,
                                     font=F_BIGVAL, width=7, anchor="e")
        self._duty_big.pack(side="left")
        tk.Label(disp, text="%", bg=BG_CARD, fg=FG_MID,
                 font=F_UNIT).pack(side="left", anchor="s", pady=(0, 6))

        # Slider
        sf = tk.Frame(card, bg=BG_CARD)
        sf.pack(fill="x", padx=12, pady=(6, 0))
        self._duty_slider = tk.Scale(
            sf, from_=1, to=99, resolution=0.1,
            orient="horizontal", variable=self._duty_var,
            bg=BG_CARD, fg=FG_DIM, troughcolor=BG_FIELD,
            highlightthickness=0, activebackground=ACCENT,
            showvalue=0, sliderlength=18, width=8,
            command=self._on_duty_slider,
        )
        self._duty_slider.pack(fill="x")

        rng = tk.Frame(card, bg=BG_CARD)
        rng.pack(fill="x", padx=14)
        tk.Label(rng, text="1%",  bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="left")
        tk.Label(rng, text="99%", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="right")

        # Step buttons
        steps = tk.Frame(card, bg=BG_CARD)
        steps.pack(pady=(4, 0))
        for delta, label in [(-10, "−10%"), (-5, "−5%"), (-1, "−1%"),
                              (+1,  "+1%"), (+5,  "+5%"), (+10, "+10%")]:
            _step_btn(steps, label,
                      lambda d=delta: self._step_duty(d)).pack(
                side="left", padx=2)

        # Direct-entry row
        entry_row = tk.Frame(card, bg=BG_CARD)
        entry_row.pack(pady=(4, 8))
        tk.Label(entry_row, text="Go to:", bg=BG_CARD, fg=FG_DIM,
                 font=F_LABEL).pack(side="left", padx=(0, 6))
        self._duty_entry = tk.StringVar()
        e = tk.Entry(entry_row, textvariable=self._duty_entry, width=9,
                     bg=BG_FIELD, fg=FG, insertbackground=FG,
                     relief="flat", font=F_MONO, bd=4)
        e.pack(side="left")
        e.bind("<Return>", lambda _: self._apply_duty_entry())
        tk.Label(entry_row, text="%", bg=BG_CARD, fg=FG_MID,
                 font=F_LABEL).pack(side="left", padx=(2, 8))
        tk.Button(
            entry_row, text="Set", command=self._apply_duty_entry,
            bg=BG_FIELD, fg=FG, activebackground=SEP, activeforeground=FG,
            relief="flat", font=F_BTN, width=5, cursor="hand2",
            bd=0, highlightthickness=1, highlightbackground=BG_FIELD,
        ).pack(side="left", padx=(0, 12))

        # Output enable/disable toggle
        self._out_btn = tk.Button(
            entry_row, text="Output: ON",
            command=self._toggle_output,
            bg="#1a5a1a", fg="white",
            activebackground="#2a6a2a", activeforeground="white",
            relief="groove", font=F_BTN, width=12, cursor="hand2",
            bd=2, highlightthickness=1, highlightbackground="#2a6a2a",
        )
        self._out_btn.pack(side="left")

    # ── Readouts panel ────────────────────────────────────────────────────────

    def _build_readouts(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="x", padx=OUTER_PAD)

        card = tk.Frame(outer, bg=BG_CARD)
        card.pack(fill="x")

        _section_hdr(card, "Input Signal (IN1)").pack(
            anchor="w", padx=12, pady=(6, 4))
        _sep_line(card).pack(fill="x")

        grid = tk.Frame(card, bg=BG_CARD)
        grid.pack(fill="x", pady=4)

        self._freq_lbl  = _readout_row(grid, "Frequency",   0)
        self._lock_lbl  = _readout_row(grid, "Lock Status", 1)
        self._duty_lbl  = _readout_row(grid, "Out Duty",    2)
        self._up_lbl    = _readout_row(grid, "Uptime",      3)

        if not RP_AVAILABLE:
            _readout_row(grid, "Mode", 4).config(
                text="SIMULATION", fg=ORANGE)

    # ── Frequency history chart ───────────────────────────────────────────────

    def _build_chart(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="x", padx=OUTER_PAD, pady=(8, OUTER_PAD))

        hdr_frame = tk.Frame(outer, bg=BG_CARD)
        hdr_frame.pack(fill="x")
        _section_hdr(hdr_frame, "Input Frequency History").pack(
            side="left", padx=12, pady=(8, 4))
        tk.Label(hdr_frame, text=f"last {CHART_DUR:.0f} s",
                 bg=BG_CARD, fg=FG_DIM, font=F_LABEL).pack(
            side="right", padx=12, pady=(8, 4))
        _sep_line(hdr_frame).pack(fill="x")

        cf = tk.Frame(outer, bg=BG_CARD)
        cf.pack(fill="x")
        self._canvas = tk.Canvas(
            cf, width=WIN_W, height=CHART_H,
            bg=CHART_BG, highlightthickness=0,
        )
        self._canvas.pack(padx=0, pady=(4, 8))

    # ── Control event handlers ────────────────────────────────────────────────

    def _on_duty_slider(self, _val=None):
        pct = self._duty_var.get()
        self._duty_big.config(text=f"{pct:5.1f}")
        self._hw.set_duty(pct / 100.0)

    def _step_duty(self, delta: float):
        new = max(1.0, min(99.0, round(self._duty_var.get() + delta, 1)))
        self._duty_var.set(new)
        self._on_duty_slider()

    def _apply_duty_entry(self):
        try:
            pct = float(self._duty_entry.get())
            pct = max(1.0, min(99.0, round(pct, 1)))
            self._duty_var.set(pct)
            self._on_duty_slider()
            self._duty_entry.set("")
        except ValueError:
            self._duty_entry.set("")

    def _toggle_output(self):
        self._output_enabled = not self._output_enabled
        self._hw.set_enabled(self._output_enabled)
        if self._output_enabled:
            self._out_btn.config(text="Output: ON",  fg="white",
                                 bg="#1a5a1a", activebackground="#2a6a2a",
                                 highlightbackground="#2a6a2a")
        else:
            self._out_btn.config(text="Output: OFF", fg="white",
                                 bg="#5a1a1a", activebackground="#6a2a2a",
                                 highlightbackground="#6a2a2a")

    # ── 100 ms UI tick ────────────────────────────────────────────────────────

    def _tick(self):
        if not self._running:
            return

        freq    = self._hw.freq
        locked  = self._hw.locked
        uptime  = self._hw.uptime_s
        duty_pct = self._duty_var.get()

        # Readouts
        if locked:
            self._freq_lbl.config(text=f"{freq:,.2f} Hz")
            self._lock_lbl.config(text="SIGNAL OK", fg=GREEN)
        else:
            self._freq_lbl.config(text="—")
            self._lock_lbl.config(text="NO SIGNAL", fg=RED_C)

        self._duty_lbl.config(text=f"{duty_pct:.1f}%")
        mins, secs = divmod(uptime, 60)
        self._up_lbl.config(text=f"{mins:02d}:{secs:02d}")

        # Chart history
        now = time.monotonic()
        if locked and freq > 0:
            self._freq_hist.append((now, freq))
        # Prune old entries
        cutoff = now - CHART_DUR
        self._freq_hist = [(t, f) for t, f in self._freq_hist if t >= cutoff]

        self._redraw_chart()

        self.after(100, self._tick)

    def _redraw_chart(self):
        c = self._canvas
        c.delete("all")

        data = self._freq_hist
        CL, CR, CT, CB = 58, 12, 10, 20
        cw = WIN_W
        pw = cw - CL - CR
        ph = CHART_H - CT - CB
        x0 = CL
        x1 = cw - CR
        y0 = CT
        y1 = CT + ph

        # Y-axis range: centre on median, ±5 % or minimum ±1 Hz
        if len(data) >= 2:
            freqs = [f for _, f in data]
            f_mid = sorted(freqs)[len(freqs) // 2]
            f_span = max(f_mid * 0.01, 2.0)    # at least 2 Hz range
            f_lo = f_mid - f_span
            f_hi = f_mid + f_span
        elif data:
            f_mid = data[-1][1]
            f_span = max(f_mid * 0.01, 2.0)
            f_lo = f_mid - f_span
            f_hi = f_mid + f_span
        else:
            f_lo, f_hi, f_mid = 0.0, 1.0, 0.5

        def to_xy(t, f):
            now = time.monotonic()
            x = x0 + (t - (now - CHART_DUR)) / CHART_DUR * pw
            y = y1 - (f - f_lo) / (f_hi - f_lo) * ph
            y = max(y0, min(y1, y))
            return x, y

        # Grid lines (top, mid, bottom)
        for label, f in [(f"{f_hi:.1f}", f_hi),
                          (f"{f_mid:.1f}", f_mid),
                          (f"{f_lo:.1f}", f_lo)]:
            _, gy = to_xy(0, f)
            dash = () if f == f_mid else (4, 4)
            c.create_line(x0, gy, x1, gy,
                          fill="#333333", dash=dash, width=1)
            c.create_text(x0 - 4, gy, text=label,
                          fill=FG_DIM, font=("Courier New", 8), anchor="e")

        # Border
        c.create_rectangle(x0, y0, x1, y1, outline=SEP, width=1)

        # Time labels
        now = time.monotonic()
        c.create_text(x0, y1 + 4, text=f"−{CHART_DUR:.0f}s",
                      fill=FG_DIM, font=("Courier New", 8), anchor="nw")
        c.create_text(x1, y1 + 4, text="now",
                      fill=FG_DIM, font=("Courier New", 8), anchor="ne")

        # Trace
        if len(data) >= 2:
            pts = []
            for t, f in data:
                x, y = to_xy(t, f)
                pts.extend([x, y])
            c.create_line(*pts, fill=ACCENT, width=2, smooth=False)

            # Last value annotation
            _, last_f = data[-1]
            lx, ly = to_xy(data[-1][0], last_f)
            c.create_text(x1 - 2, ly,
                          text=f"{last_f:.1f} Hz",
                          fill=ACCENT, font=("Courier New", 8, "bold"),
                          anchor="e")
        elif not data:
            c.create_text((x0 + x1) // 2, (y0 + y1) // 2,
                          text="Waiting for signal…",
                          fill=FG_DIM, font=("Segoe UI", 10))

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _on_close(self):
        self._running = False
        self._hw.stop()
        time.sleep(0.15)   # let worker finish one last iteration
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
