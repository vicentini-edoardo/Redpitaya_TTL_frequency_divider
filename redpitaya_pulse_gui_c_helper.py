#!/usr/bin/env python3
"""
redpitaya_pulse_gui_c_helper.py — Desktop GUI for the Red Pitaya pulse generator.

Communicates with the board over SSH using a small C helper binary (rp_pulse_ctl)
that reads/writes FPGA registers via /dev/mem. All SSH commands are non-interactive
and use the full binary paths to work around the limited PATH of non-login shells.

Run with:  python3 redpitaya_pulse_gui_c_helper.py
"""
import json
import shlex
import shutil
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont

CLOCK_HZ = 125_000_000          # Red Pitaya FPGA clock frequency
BASE_ADDR = 0x40600000           # Default AXI base address of the FPGA core
REMOTE_BIN      = "/root/rp_pulse_ctl"
REMOTE_FPGAUTIL = "/opt/redpitaya/bin/fpgautil"   # Full path required for non-login SSH
REMOTE_BITFILE  = "/root/red_pitaya_top.bit.bin"

DIV_MIN   = 1
DIV_MAX   = 32
WIDTH_MIN = 1    # minimum hardware cycle count
DELAY_MIN = 1    # minimum hardware cycle count

# ── Palette ───────────────────────────────────────────────────────────────────
CLR_BG       = "#1e1e2e"
CLR_SURFACE  = "#2a2a3d"
CLR_ACCENT   = "#89b4fa"
CLR_SUCCESS  = "#a6e3a1"
CLR_WARN     = "#f38ba8"
CLR_TEXT     = "#cdd6f4"
CLR_MUTED    = "#6c7086"
CLR_ENTRY_BG = "#313244"
CLR_BORDER   = "#45475a"


def _best_font(families, size, weight=""):
    available = set(tkfont.families())
    for f in families:
        if f in available:
            return (f, size, weight) if weight else (f, size)
    return (families[-1], size, weight) if weight else (families[-1], size)


def fmt_freq_hz(freq_hz: float) -> str:
    if freq_hz >= 1e6:
        return f"{freq_hz/1e6:.6g} MHz"
    if freq_hz >= 1e3:
        return f"{freq_hz/1e3:.6g} kHz"
    return f"{freq_hz:.6g} Hz"


def fmt_time_s(value_s: float) -> str:
    if value_s >= 1:
        return f"{value_s:.6g} s"
    if value_s >= 1e-3:
        return f"{value_s*1e3:.6g} ms"
    if value_s >= 1e-6:
        return f"{value_s*1e6:.6g} us"
    return f"{value_s*1e9:.6g} ns"


def frac_to_cycles(frac: float, period_cycles: int) -> int:
    """Width fraction [0, 1] → hardware cycles, clamped to [WIDTH_MIN, period_cycles]."""
    return max(WIDTH_MIN, min(period_cycles, round(frac * period_cycles)))


def cycles_to_frac(cycles: int, period_cycles: int) -> float:
    """Hardware cycles → width fraction [0, 1]."""
    return cycles / period_cycles if period_cycles > 0 else 0.0


def deg_to_cycles(deg: float, period_cycles: int) -> int:
    """Delay phase [0°, 180°] → hardware cycles, clamped to [DELAY_MIN, period//2]."""
    max_delay = max(DELAY_MIN, period_cycles // 2)
    return max(DELAY_MIN, min(max_delay, round((deg / 360.0) * period_cycles)))


def cycles_to_deg(cycles: int, period_cycles: int) -> float:
    """Hardware cycles → delay phase degrees."""
    return (cycles / period_cycles) * 360.0 if period_cycles > 0 else 0.0


class RemoteCtl:
    def __init__(self):
        self.host = ""
        self.user = ""
        self.port = 22

    def connect(self, host: str, user: str, port: int):
        if not shutil.which("ssh"):
            raise RuntimeError("OpenSSH client not found on this PC.")
        self.host = host
        self.user = user
        self.port = port

    def run(self, cmd: str):
        ssh_cmd = ["ssh", "-p", str(self.port), f"{self.user}@{self.host}", cmd]
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "SSH command failed.")
        return proc.stdout.strip()

    def helper(self, base_addr: int, command: str, *args: int):
        remote_cmd = " ".join(
            [shlex.quote(REMOTE_BIN), shlex.quote(hex(base_addr)), shlex.quote(command)] +
            [shlex.quote(str(a)) for a in args]
        )
        return json.loads(self.run(remote_cmd))

    def upload_bitfile(self, local_path: str):
        if not shutil.which("scp"):
            raise RuntimeError("scp not found on this PC.")
        scp_cmd = ["scp", "-P", str(self.port), local_path,
                   f"{self.user}@{self.host}:{REMOTE_BITFILE}"]
        proc = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"scp failed: {proc.stderr.strip() or proc.stdout.strip()}")
        self.run(f"{REMOTE_FPGAUTIL} -b {REMOTE_BITFILE}")

    def upload_and_compile(self, local_src: str, remote_src: str = "/root/rp_pulse_ctl.c"):
        if not shutil.which("scp"):
            raise RuntimeError("scp not found on this PC.")
        scp_cmd = ["scp", "-P", str(self.port), local_src,
                   f"{self.user}@{self.host}:{remote_src}"]
        proc = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"scp failed: {proc.stderr.strip() or proc.stdout.strip()}")
        compile_cmd = f"gcc -O2 -o {shlex.quote(REMOTE_BIN)} {shlex.quote(remote_src)}"
        return self.run(compile_cmd)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Red Pitaya Pulse Control")
        self.root.geometry("900x620")
        self.root.configure(bg=CLR_BG)

        self.remote = RemoteCtl()
        self.connected = False
        self.base_addr = BASE_ADDR

        # Connection vars
        self.host_var = tk.StringVar(value="rp-f06a51.local")
        self.port_var = tk.StringVar(value="22")
        self.user_var = tk.StringVar(value="root")
        self.base_var = tk.StringVar(value="0x40600000")

        self.enable_var = tk.BooleanVar(value=True)
        self.auto_apply_var = tk.BooleanVar(value=False)

        # Divider
        self.divider_var = tk.IntVar(value=1)
        self.divider_entry_var = tk.StringVar(value="1")

        # Width as duty-cycle fraction [0, 1]
        self.width_frac_var = tk.DoubleVar(value=0.5)
        self.width_frac_entry_var = tk.StringVar(value="0.500")
        self.width_ns_var = tk.StringVar(value="")

        # Delay as phase in degrees [0, 180]
        self.delay_deg_var = tk.DoubleVar(value=0.0)
        self.delay_deg_entry_var = tk.StringVar(value="0.0")
        self.delay_ns_var = tk.StringVar(value="")

        # Internal cycle tracking — updated from hardware filt_period
        self._period_cycles = 1
        self._force_period_update = False
        self._auto_apply_job = None
        self._poll_job = None

        # Advanced panel state
        self._adv_visible = False
        self._adv_frame = None
        self._adv_toggle_btn = None

        # Scale widget references (set in _build)
        self.divider_scale = None
        self.width_scale = None
        self.delay_scale = None

        self.updating_widgets = False

        self.status_text = tk.StringVar(value="Disconnected.")
        self.info_text = tk.StringVar(value="Connect to read input frequency from hardware.")
        self.readback_text = tk.StringVar(value="No register readback yet.")
        self.freq_warning_text = tk.StringVar(value="")

        self._build()

    # ── Styles ────────────────────────────────────────────────────────────────

    def _setup_styles(self):
        FONT_MAIN  = _best_font(["Inter", "Segoe UI", "Helvetica", "TkDefaultFont"], 10)
        FONT_LABEL = _best_font(["Inter", "Segoe UI", "Helvetica", "TkDefaultFont"], 10, "bold")
        FONT_SMALL = _best_font(["Inter", "Segoe UI", "Helvetica", "TkDefaultFont"], 9)
        FONT_MONO  = _best_font(["JetBrains Mono", "Menlo", "Consolas", "Courier New"], 9)

        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass

        s.configure("TFrame",         background=CLR_BG)
        s.configure("Surface.TFrame", background=CLR_SURFACE)

        s.configure("TLabelframe",
                    background=CLR_SURFACE, bordercolor=CLR_BORDER,
                    relief="flat", borderwidth=1)
        s.configure("TLabelframe.Label",
                    background=CLR_SURFACE, foreground=CLR_ACCENT, font=FONT_LABEL)

        s.configure("TLabel",
                    background=CLR_SURFACE, foreground=CLR_TEXT, font=FONT_MAIN)
        s.configure("Muted.TLabel",
                    background=CLR_SURFACE, foreground=CLR_MUTED, font=FONT_SMALL)
        s.configure("Mono.TLabel",
                    background=CLR_SURFACE, foreground=CLR_TEXT, font=FONT_MONO)
        s.configure("Status.TLabel",
                    background=CLR_SURFACE, foreground=CLR_ACCENT, font=FONT_SMALL)
        s.configure("Info.TLabel",
                    background=CLR_SURFACE, foreground=CLR_TEXT, font=FONT_SMALL)
        s.configure("Warning.TLabel",
                    background=CLR_SURFACE, foreground=CLR_WARN, font=FONT_LABEL)

        s.configure("TEntry",
                    fieldbackground=CLR_ENTRY_BG, foreground=CLR_TEXT,
                    bordercolor=CLR_BORDER, insertcolor=CLR_TEXT,
                    selectbackground=CLR_ACCENT, selectforeground=CLR_BG)
        s.map("TEntry", bordercolor=[("focus", CLR_ACCENT)])

        s.configure("TButton",
                    background=CLR_SURFACE, foreground=CLR_TEXT,
                    bordercolor=CLR_BORDER, font=FONT_MAIN,
                    relief="flat", padding=(8, 4))
        s.map("TButton",
              background=[("active", CLR_BORDER), ("pressed", CLR_ACCENT)],
              foreground=[("pressed", CLR_BG)])

        s.configure("Accent.TButton",
                    background=CLR_ACCENT, foreground=CLR_BG,
                    bordercolor=CLR_ACCENT, font=FONT_LABEL,
                    relief="flat", padding=(10, 5))
        s.map("Accent.TButton",
              background=[("active", "#74a8e8"), ("pressed", "#5a90d0")],
              foreground=[("active", CLR_BG)])

        s.configure("TCheckbutton",
                    background=CLR_SURFACE, foreground=CLR_TEXT, font=FONT_MAIN)
        s.map("TCheckbutton", background=[("active", CLR_SURFACE)])

        s.configure("TScale",
                    troughcolor=CLR_ENTRY_BG, background=CLR_ACCENT,
                    bordercolor=CLR_BORDER)

        s.configure("TSeparator", background=CLR_BORDER)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        self._setup_styles()

        outer = ttk.Frame(self.root, padding=14, style="TFrame")
        outer.pack(fill="both", expand=True)

        self._build_connection(outer)
        self._build_controls(outer)
        self._build_readback(outer)

    def _build_connection(self, outer):
        conn = ttk.LabelFrame(outer, text="Connection", padding=10)
        conn.pack(fill="x", pady=(0, 10))

        ttk.Label(conn, text="Host").grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Entry(conn, textvariable=self.host_var, width=20).grid(row=0, column=1, sticky="w", padx=(0, 10))

        ttk.Button(conn, text="Connect",      command=self.connect).grid(row=0, column=2, padx=3)
        ttk.Button(conn, text="Read back",    command=self.read_back).grid(row=0, column=3, padx=3)
        ttk.Button(conn, text="Soft reset",   command=self.soft_reset).grid(row=0, column=4, padx=3)
        ttk.Button(conn, text="Upload & compile", command=self.upload_and_compile).grid(row=0, column=5, padx=3)
        ttk.Button(conn, text="Upload bitfile",   command=self.upload_bitfile).grid(row=1, column=2, columnspan=2, padx=3, pady=(6,0), sticky="w")
        ttk.Checkbutton(conn, text="Auto apply", variable=self.auto_apply_var).grid(row=0, column=6, padx=(10, 0))

        self._adv_toggle_btn = ttk.Button(conn, text="▼ Advanced", command=self._toggle_advanced)
        self._adv_toggle_btn.grid(row=0, column=7, padx=(10, 0), sticky="e")
        conn.grid_columnconfigure(7, weight=1)

        ttk.Label(conn, textvariable=self.status_text, style="Status.TLabel").grid(
            row=1, column=0, columnspan=8, sticky="w", pady=(8, 0))

        ttk.Label(conn, textvariable=self.info_text, style="Info.TLabel", justify="left").grid(
            row=2, column=0, columnspan=7, sticky="w", pady=(4, 0))
        ttk.Button(conn, text="Force freq update", command=self._force_freq_update).grid(
            row=2, column=7, sticky="e", pady=(4, 0))

        ttk.Label(conn, textvariable=self.freq_warning_text, style="Warning.TLabel").grid(
            row=3, column=0, columnspan=8, sticky="w", pady=(2, 0))

        # Advanced sub-frame (hidden by default)
        self._adv_frame = ttk.Frame(conn, style="Surface.TFrame", padding=(0, 8, 0, 0))
        self._adv_frame.grid(row=4, column=0, columnspan=8, sticky="ew")
        self._adv_frame.grid_remove()

        sep = ttk.Separator(self._adv_frame, orient="horizontal")
        sep.grid(row=0, column=0, columnspan=6, sticky="ew", pady=(0, 8))

        ttk.Label(self._adv_frame, text="Port").grid(row=1, column=0, sticky="w", padx=(0, 4))
        ttk.Entry(self._adv_frame, textvariable=self.port_var, width=8).grid(row=1, column=1, sticky="w", padx=(0, 16))

        ttk.Label(self._adv_frame, text="User").grid(row=1, column=2, sticky="w", padx=(0, 4))
        ttk.Entry(self._adv_frame, textvariable=self.user_var, width=12).grid(row=1, column=3, sticky="w", padx=(0, 16))

        ttk.Label(self._adv_frame, text="Base address").grid(row=1, column=4, sticky="w", padx=(0, 4))
        ttk.Entry(self._adv_frame, textvariable=self.base_var, width=16).grid(row=1, column=5, sticky="w")

    def _toggle_advanced(self):
        if self._adv_visible:
            self._adv_frame.grid_remove()
            self._adv_toggle_btn.configure(text="▼ Advanced")
        else:
            self._adv_frame.grid()
            self._adv_toggle_btn.configure(text="▲ Advanced")
        self._adv_visible = not self._adv_visible

    def _build_controls(self, outer):
        ctrl = ttk.LabelFrame(outer, text="Controls", padding=10)
        ctrl.pack(fill="both", expand=True, pady=(0, 10))

        self._add_param_row(ctrl, 0,
                            label="Divider",
                            float_var=None, int_var=self.divider_var,
                            entry_var=self.divider_entry_var,
                            minv=DIV_MIN, maxv=DIV_MAX,
                            callback=self.on_divider_change,
                            ns_var=None,
                            scale_attr="divider_scale")

        self._add_param_row(ctrl, 1,
                            label="Width (duty 0–1)",
                            float_var=self.width_frac_var, int_var=None,
                            entry_var=self.width_frac_entry_var,
                            minv=0.0, maxv=1.0,
                            callback=self.on_width_change,
                            ns_var=self.width_ns_var,
                            scale_attr="width_scale")

        self._add_param_row(ctrl, 2,
                            label="Delay (phase 0–180°)",
                            float_var=self.delay_deg_var, int_var=None,
                            entry_var=self.delay_deg_entry_var,
                            minv=0.0, maxv=180.0,
                            callback=self.on_delay_change,
                            ns_var=self.delay_ns_var,
                            scale_attr="delay_scale")

        btn_frame = ttk.Frame(ctrl, style="Surface.TFrame")
        btn_frame.grid(row=3, column=0, columnspan=4, sticky="w", pady=(14, 0))

        ttk.Checkbutton(btn_frame, text="Enable output", variable=self.enable_var,
                        command=self.maybe_auto_apply).pack(side="left", padx=(0, 12))
        ttk.Button(btn_frame, text="Apply now", command=self.apply_now,
                   style="Accent.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(btn_frame, text="Read registers", command=self.read_back).pack(side="left")

    def _add_param_row(self, parent, row, label, float_var, int_var,
                       entry_var, minv, maxv, callback, ns_var, scale_attr):
        ttk.Label(parent, text=label, width=20, anchor="w").grid(
            row=row, column=0, sticky="w", pady=(6, 0), padx=(0, 8))

        var = float_var if float_var is not None else int_var
        scale = ttk.Scale(parent, from_=minv, to=maxv, orient="horizontal",
                          variable=var,
                          command=lambda value, cb=callback: cb(value))
        scale.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=(6, 0))
        parent.grid_columnconfigure(1, weight=1)

        entry = ttk.Entry(parent, textvariable=entry_var, width=8)
        entry.grid(row=row, column=2, sticky="w", pady=(6, 0), padx=(0, 8))
        entry.bind("<Return>",   lambda e, cb=callback: cb(None))
        entry.bind("<FocusOut>", lambda e, cb=callback: cb(None))

        if ns_var is not None:
            ttk.Label(parent, textvariable=ns_var, style="Muted.TLabel", width=12).grid(
                row=row, column=3, sticky="w", pady=(6, 0))

        setattr(self, scale_attr, scale)
        scale.set(var.get())

    def _build_readback(self, outer):
        rb = ttk.LabelFrame(outer, text="Readback", padding=10)
        rb.pack(fill="x")
        ttk.Label(rb, textvariable=self.readback_text,
                  style="Mono.TLabel", justify="left").pack(anchor="w")

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self):
        try:
            host = self.host_var.get().strip()
            user = self.user_var.get().strip()
            port = int(self.port_var.get().strip())
            self.base_addr = int(self.base_var.get().replace("_", ""), 0)
            self.remote.connect(host, user, port)
            self.connected = True
            self.status_text.set("Loading FPGA bitstream…")
            self.root.update_idletasks()
            self.remote.run(f"{REMOTE_FPGAUTIL} -b {REMOTE_BITFILE}")
            self.status_text.set(f"Connected to {user}@{host}:{port}.")
            self.read_back()
            self._start_poll()
        except Exception as exc:
            self.connected = False
            messagebox.showerror("Connection error", str(exc))
            self.status_text.set("Connection failed.")

    # ── Info text (divider change only) ───────────────────────────────────────

    def _start_poll(self, interval_ms=2000):
        # Polls the board every 2 s to keep status/frequency display current.
        self._stop_poll()
        self._poll_tick(interval_ms)

    def _stop_poll(self):
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None

    def _poll_tick(self, interval_ms):
        if not self.connected:
            return
        try:
            data = self.remote.helper(self.base_addr, "read")
            self._update_readback(data)
        except Exception:
            pass
        self._poll_job = self.root.after(interval_ms, self._poll_tick, interval_ms)

    def upload_bitfile(self):
        if not self.connected:
            messagebox.showerror("Not connected", "Connect to the Red Pitaya first.")
            return
        import os
        local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "red_pitaya_top.bit.bin")
        if not os.path.isfile(local_path):
            messagebox.showerror("File not found", f"Cannot find:\n{local_path}")
            return
        self.status_text.set("Uploading bitfile…")
        self.root.update_idletasks()
        try:
            self.remote.upload_bitfile(local_path)
            self.status_text.set("Bitfile uploaded and FPGA reloaded.")
            self.read_back()
        except Exception as exc:
            messagebox.showerror("Upload bitfile failed", str(exc))
            self.status_text.set("Bitfile upload failed.")

    def upload_and_compile(self):
        if not self.connected:
            messagebox.showerror("Not connected", "Connect to the Red Pitaya first.")
            return
        import os
        local_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rp_pulse_ctl.c")
        if not os.path.isfile(local_src):
            messagebox.showerror("File not found", f"Cannot find:\n{local_src}")
            return
        self.status_text.set("Uploading rp_pulse_ctl.c…")
        self.root.update_idletasks()
        try:
            self.remote.upload_and_compile(local_src)
            self.status_text.set("Upload & compile successful.")
        except Exception as exc:
            messagebox.showerror("Upload/compile failed", str(exc))
            self.status_text.set("Upload/compile failed.")

    def _force_freq_update(self):
        self._force_period_update = True
        self.read_back()

    def _update_info_text(self):
        if self._period_cycles <= 1:
            return
        divider    = max(DIV_MIN, min(DIV_MAX, self.divider_var.get()))
        input_hz   = CLOCK_HZ / self._period_cycles
        divided_hz = input_hz / divider
        input_period_s = self._period_cycles / CLOCK_HZ
        self.info_text.set(
            f"Input: {fmt_freq_hz(input_hz)}  |  Divider: ÷{divider}  |  "
            f"Divided: {fmt_freq_hz(divided_hz)}\n"
            f"Width/Delay ref: input period {fmt_time_s(input_period_s)}  ({self._period_cycles} cycles)"
        )

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_divider_change(self, value):
        if self.updating_widgets:
            return
        if value is None:
            try:
                new_val = int(self.divider_entry_var.get().strip())
            except ValueError:
                new_val = self.divider_var.get()
        else:
            new_val = int(round(float(value)))
        new_val = max(DIV_MIN, min(DIV_MAX, new_val))
        self.updating_widgets = True
        self.divider_var.set(new_val)
        self.divider_entry_var.set(str(new_val))
        self.divider_scale.set(new_val)
        self.updating_widgets = False
        self._update_info_text()
        self.maybe_auto_apply()

    def on_width_change(self, value):
        if self.updating_widgets:
            return
        if value is None:
            try:
                new_val = float(self.width_frac_entry_var.get().strip())
            except ValueError:
                new_val = self.width_frac_var.get()
        else:
            new_val = float(value)
        new_val = max(0.0, min(1.0, new_val))
        self.updating_widgets = True
        self.width_frac_var.set(new_val)
        self.width_frac_entry_var.set(f"{new_val:.3f}")
        self.width_scale.set(new_val)
        w_cyc = frac_to_cycles(new_val, self._period_cycles)
        self.width_ns_var.set(fmt_time_s(w_cyc / CLOCK_HZ))
        self.updating_widgets = False
        self.maybe_auto_apply()

    def on_delay_change(self, value):
        if self.updating_widgets:
            return
        if value is None:
            try:
                new_val = float(self.delay_deg_entry_var.get().strip())
            except ValueError:
                new_val = self.delay_deg_var.get()
        else:
            new_val = float(value)
        new_val = max(0.0, min(180.0, new_val))
        self.updating_widgets = True
        self.delay_deg_var.set(new_val)
        self.delay_deg_entry_var.set(f"{new_val:.1f}")
        self.delay_scale.set(new_val)
        d_cyc = deg_to_cycles(new_val, self._period_cycles)
        self.delay_ns_var.set(fmt_time_s(d_cyc / CLOCK_HZ))
        self.updating_widgets = False
        self.maybe_auto_apply()

    # ── Hardware ops ──────────────────────────────────────────────────────────

    def maybe_auto_apply(self):
        # Debounced: cancels any pending call and restarts the 300 ms timer,
        # so rapid slider drags produce only one SSH write after the user stops.
        if not self.auto_apply_var.get():
            return
        if self._auto_apply_job is not None:
            self.root.after_cancel(self._auto_apply_job)
        self._auto_apply_job = self.root.after(300, self._do_auto_apply)

    def _do_auto_apply(self):
        self._auto_apply_job = None
        self.apply_now()

    def apply_now(self):
        if not self.connected:
            return
        try:
            divider = max(DIV_MIN, min(DIV_MAX, self.divider_var.get()))
            frac = max(0.0, min(1.0, self.width_frac_var.get()))
            deg  = max(0.0, min(180.0, self.delay_deg_var.get()))
            width_cycles = frac_to_cycles(frac, self._period_cycles)
            delay_cycles = deg_to_cycles(deg, self._period_cycles)

            enable = 1 if self.enable_var.get() else 0
            data = self.remote.helper(self.base_addr, "write",
                                      divider, width_cycles, delay_cycles, enable)
            self._update_readback(data)
            self.status_text.set(
                f"Applied — width {width_cycles} cyc, delay {delay_cycles} cyc.")
        except Exception as exc:
            messagebox.showerror("Apply failed", str(exc))
            self.status_text.set("Apply failed.")

    def read_back(self):
        if not self.connected:
            return
        try:
            data = self.remote.helper(self.base_addr, "read")
            self._update_readback(data)
            self.status_text.set("Readback updated.")
        except Exception as exc:
            messagebox.showerror("Readback failed", str(exc))
            self.status_text.set("Readback failed.")

    def soft_reset(self):
        if not self.connected:
            return
        try:
            data = self.remote.helper(self.base_addr, "soft_reset")
            self._update_readback(data)
            self.status_text.set("Soft reset pulse sent.")
        except Exception as exc:
            messagebox.showerror("Soft reset failed", str(exc))
            self.status_text.set("Soft reset failed.")

    def _update_readback(self, data):
        control     = int(data.get("control",     0))
        divider     = int(data.get("divider",     0))
        width       = int(data.get("width",       0))
        delay       = int(data.get("delay",       0))
        status      = int(data.get("status",      0))
        raw_period  = int(data.get("raw_period",  0))
        filt_period = int(data.get("filt_period", 0))

        busy         = (status >> 0) & 0x1
        period_valid = (status >> 1) & 0x1
        timeout_flag = (status >> 2) & 0x1
        enable       = control & 0x1

        # Update internal period reference from filtered hardware measurement.
        # Only apply if change exceeds 5% or a force update was requested.
        if period_valid and filt_period > 0:
            change = abs(filt_period - self._period_cycles) / max(1, self._period_cycles)
            if self._force_period_update or self._period_cycles <= 1 or change > 0.05:
                self._force_period_update = False
                self._period_cycles = filt_period
                self._update_info_text()
                self.width_ns_var.set(fmt_time_s(
                    frac_to_cycles(self.width_frac_var.get(), filt_period) / CLOCK_HZ))
                self.delay_ns_var.set(fmt_time_s(
                    deg_to_cycles(self.delay_deg_var.get(), filt_period) / CLOCK_HZ))

        raw_freq  = CLOCK_HZ / raw_period  if raw_period  > 0 else 0.0
        filt_freq = CLOCK_HZ / filt_period if filt_period > 0 else 0.0

        width_frac = cycles_to_frac(width, self._period_cycles)
        delay_deg  = cycles_to_deg(delay,  self._period_cycles)

        if not period_valid or timeout_flag:
            self.freq_warning_text.set("⚠  No input frequency detected")
        else:
            self.freq_warning_text.set("")

        status_str = f"busy={busy}  valid={period_valid}  timeout={timeout_flag}"

        self.readback_text.set(
            f"control  = 0x{control:08X}    enable = {enable}\n"
            f"divider  = {divider}\n"
            f"width    = {width:6d} cycles  ({fmt_time_s(width / CLOCK_HZ):>10})  →  {width_frac:.3f} duty\n"
            f"delay    = {delay:6d} cycles  ({fmt_time_s(delay / CLOCK_HZ):>10})  →  {delay_deg:.1f}°\n"
            f"status   = 0x{status:08X}    {status_str}\n"
            f"raw  f   = {fmt_freq_hz(raw_freq):>12}  ({raw_period} cycles)\n"
            f"filt f   = {fmt_freq_hz(filt_freq):>12}  ({filt_period} cycles)"
        )


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
