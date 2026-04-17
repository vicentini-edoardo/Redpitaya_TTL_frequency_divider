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
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont

CLOCK_HZ   = 125_000_000          # Red Pitaya FPGA clock frequency
BASE_ADDR  = 0x40600000           # Default AXI base address of the FPGA core
NCO_WIDTH  = 48                   # Phase accumulator width (W)
REMOTE_BIN      = "/root/rp_pulse_ctl"
REMOTE_FPGAUTIL = "/opt/redpitaya/bin/fpgautil"   # Full path required for non-login SSH
REMOTE_BITFILE  = "/root/red_pitaya_top.bit.bin"

DIV_MIN   = 1
DIV_MAX   = 32
WIDTH_MIN = 1    # minimum hardware cycle count
DELAY_MIN = 1    # minimum hardware cycle count
MOD_FREQ_MIN_HZ = 0.0
MOD_FREQ_MAX_HZ = 25_000.0
MOD_AMP_MIN = 0.0
MOD_AMP_MAX = 1.0
MOD_AMP_Q15_MAX = 32767

CONTROL_PULSE_ENABLE     = 0x1
CONTROL_SOFT_RESET       = 0x2
CONTROL_PHASE_MOD_ENABLE = 0x4

# ── Cyberpunk Palette ─────────────────────────────────────────────────────────
CLR_BG         = "#050a0f"   # Near-black with blue tint
CLR_SURFACE    = "#0d1117"   # Panel background
CLR_BORDER     = "#00f5ff"   # Neon cyan — glow color for brackets
CLR_BORDER_DIM = "#006b72"   # Dimmed cyan — glow effect on waveform
CLR_ACCENT     = "#00f5ff"   # Neon cyan — active elements
CLR_ACCENT2    = "#b000ff"   # Electric violet — duty cycle tile
CLR_SUCCESS    = "#00ff9f"   # Neon green — connected/output freq
CLR_WARN       = "#ff2d55"   # Hot pink — warnings/disconnected
CLR_TEXT       = "#e0f0ff"   # Cool white — primary text
CLR_MUTED      = "#8aa6c1"   # Brighter steel blue-grey — secondary labels/INPUT wave
CLR_ENTRY_BG   = "#0a1520"   # Entry field background
CLR_GRID       = "#183445"   # Brighter grid lines for dark background
CLR_STAT_BG    = "#060d14"   # Big stats tile background


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


def clamp_mod_freq_hz(freq_hz: float) -> float:
    return max(MOD_FREQ_MIN_HZ, min(MOD_FREQ_MAX_HZ, freq_hz))


def mod_freq_to_word(freq_hz: float) -> int:
    return int(clamp_mod_freq_hz(freq_hz) * (2**32) / CLOCK_HZ)


def clamp_mod_amp(value: float) -> float:
    return max(MOD_AMP_MIN, min(MOD_AMP_MAX, value))


def mod_amp_to_q15(value: float) -> int:
    return int(round(clamp_mod_amp(value) * MOD_AMP_Q15_MAX))


def q15_to_mod_amp(value: int) -> float:
    return clamp_mod_amp(value / MOD_AMP_Q15_MAX) if MOD_AMP_Q15_MAX > 0 else 0.0


# ── NCO phase-step / frequency conversion ─────────────────────────────────────

def hz_to_phase_step_offset(freq_hz: float) -> int:
    """Convert a signed frequency offset in Hz to a signed 48-bit phase-step word."""
    raw = round(freq_hz * (2 ** NCO_WIDTH) / CLOCK_HZ)
    # Clamp to signed 48-bit range
    limit = 2 ** (NCO_WIDTH - 1)
    return max(-limit, min(limit - 1, raw))


def phase_step_offset_to_hz(word: int) -> float:
    """Convert a signed 48-bit phase-step offset word back to Hz."""
    return word * CLOCK_HZ / (2 ** NCO_WIDTH)


def fmt_freq_offset_hz(freq_hz: float) -> str:
    """Format a (potentially sub-Hz) frequency offset with appropriate precision."""
    abs_f = abs(freq_hz)
    sign  = "-" if freq_hz < 0 else "+"
    if abs_f >= 1e6:
        return f"{sign}{abs_f/1e6:.6g} MHz"
    if abs_f >= 1e3:
        return f"{sign}{abs_f/1e3:.6g} kHz"
    if abs_f >= 1.0:
        return f"{sign}{abs_f:.6g} Hz"
    if abs_f >= 1e-3:
        return f"{sign}{abs_f*1e3:.6g} mHz"
    return f"{sign}{abs_f*1e6:.6g} µHz"


class _Tooltip:
    """Simple hover tooltip for any tkinter widget."""
    def __init__(self, widget, text):
        self._widget = widget
        self._text   = text
        self._tw     = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self._tw:
            return
        x = self._widget.winfo_rootx() + self._widget.winfo_width() // 2
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tw = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self._text, bg="#2e2e3e", fg=CLR_TEXT,
                 relief="flat", borderwidth=1, font=("", 9),
                 padx=6, pady=3).pack()

    def _hide(self, _event=None):
        if self._tw:
            self._tw.destroy()
            self._tw = None


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
        self.root.geometry("960x780")
        self.root.minsize(800, 680)
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
        self.phase_mod_enable_var = tk.BooleanVar(value=False)
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

        # NCO frequency offset in Hz (maps to phase_step_offset)
        self.freq_offset_hz_var = tk.DoubleVar(value=0.0)
        self.freq_offset_hz_entry_var = tk.StringVar(value="0.000000")
        self.freq_offset_word_var = tk.StringVar(value="")

        self.mod_freq_var = tk.DoubleVar(value=10.0)
        self.mod_freq_entry_var = tk.StringVar(value="10")
        self.mod_freq_word_var = tk.StringVar(value="")
        self.mod_amp_var = tk.DoubleVar(value=0.7)
        self.mod_amp_entry_var = tk.StringVar(value="0.70")
        self.mod_amp_word_var = tk.StringVar(value="")

        # Live stats display vars
        self.stat_input_freq_var  = tk.StringVar(value="—")
        self.stat_output_freq_var = tk.StringVar(value="—")
        self.stat_duty_var        = tk.StringVar(value="—")
        self.stat_phase_var       = tk.StringVar(value="—")

        # Internal cycle tracking — updated from hardware filt_period
        self._period_cycles = 1
        self._period_valid  = False
        self._period_stable = False
        self._timeout_flag  = False
        self._phase_freq_clamped = False
        self._mod_freq_dirty = False
        self._mod_amp_dirty = False
        self._force_period_update = False
        self._auto_apply_job = None
        self._poll_job = None
        self._apply_state_lock = threading.Lock()
        self._apply_thread = None
        self._pending_apply_state = None

        # Advanced panel state
        self._adv_visible = False
        self._adv_frame = None
        self._adv_toggle_btn = None

        # Scale widget references (set in _build)
        self.divider_scale = None
        self.width_scale = None
        self.delay_scale = None
        self.mod_freq_scale = None
        self.mod_amp_scale = None

        self.updating_widgets = False

        self.status_text = tk.StringVar(value="Disconnected.")
        self.info_text = tk.StringVar(value="Connect to read input frequency from hardware.")
        self.readback_text = tk.StringVar(value="No register readback yet.")
        self.freq_warning_text = tk.StringVar(value="")
        self._conn_dot = None   # tk.Label used as a colored status indicator

        self._build()
        self.on_freq_offset_change(self.freq_offset_hz_var.get())
        self.on_mod_freq_change(self.mod_freq_var.get())
        self.on_mod_amp_change(self.mod_amp_var.get())
        self._update_modulation_controls()
        self.root.bind("<Control-Return>", lambda e: self.apply_now())

    # ── Styles ────────────────────────────────────────────────────────────────

    def _setup_styles(self):
        # Set up fonts first (needed for cyber frame labels)
        self._font_label       = _best_font(["JetBrains Mono", "Menlo", "Consolas", "Courier New"], 9, "bold")
        self._font_stat_header = _best_font(["JetBrains Mono", "Menlo", "Consolas", "Courier New"], 8)
        self._font_stat_value  = _best_font(["JetBrains Mono", "Menlo", "Consolas", "Courier New"], 20, "bold")

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

    # ── Cyberpunk Frame Helper ─────────────────────────────────────────────────

    def _make_cyber_frame(self, parent, title, padding=12):
        """Create a frame with corner bracket decorations."""
        outer = tk.Frame(parent, bg=CLR_SURFACE, bd=0)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)

        c = tk.Canvas(outer, bg=CLR_SURFACE, highlightthickness=0)
        c.place(x=0, y=0, relwidth=1, relheight=1)
        c.bind("<Configure>", lambda e, cv=c: self._redraw_bracket(cv))

        tk.Label(
            outer,
            text=title,
            bg=CLR_SURFACE,
            fg=CLR_ACCENT,
            font=self._font_label,
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(2, 0))

        inner = tk.Frame(outer, bg=CLR_SURFACE, bd=0)
        inner.grid(row=1, column=0, sticky="nsew", padx=padding, pady=(8, padding))
        return outer, inner

    def _redraw_bracket(self, canvas):
        """Draw L-corner brackets on a canvas."""
        canvas.delete("bracket")
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 4 or h < 4:
            return
        sz, th = 16, 2
        for x1, y1, x2, y2 in [
            (0, 0, sz, 0), (0, 0, 0, sz),              # top-left
            (w-sz, 0, w, 0), (w, 0, w, sz),            # top-right
            (0, h-sz, 0, h), (0, h, sz, h),            # bottom-left
            (w, h-sz, w, h), (w-sz, h, w, h),          # bottom-right
        ]:
            canvas.create_line(x1, y1, x2, y2,
                               fill=CLR_BORDER, width=th, tags="bracket")

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        self._setup_styles()

        outer = tk.Frame(self.root, bg=CLR_BG)
        outer.pack(fill="both", expand=True, padx=12, pady=10)

        self._build_connection(outer)
        self._build_stats(outer)
        self._build_controls(outer)
        self._build_waveform(outer)
        self._build_readback(outer)

    def _build_connection(self, outer):
        conn_outer, conn = self._make_cyber_frame(outer, "CONNECTION")
        conn_outer.pack(fill="x", pady=(0, 10))

        # Row 0: Main connection row
        ttk.Label(conn, text="Host", background=CLR_SURFACE, foreground=CLR_TEXT).grid(
            row=0, column=0, sticky="w", padx=(0, 4), pady=(6, 0))
        ttk.Entry(conn, textvariable=self.host_var, width=22).grid(
            row=0, column=1, sticky="w", padx=(0, 10), pady=(6, 0))

        btn_connect = ttk.Button(conn, text="Connect", command=self.connect, style="Accent.TButton")
        btn_connect.grid(row=0, column=2, padx=3, pady=(6, 0))
        _Tooltip(btn_connect, "SSH-connect and load FPGA bitstream")

        status_frame = tk.Frame(conn, bg=CLR_SURFACE)
        status_frame.grid(row=0, column=3, sticky="ew", pady=(6, 0), padx=(20, 0))
        conn.grid_columnconfigure(3, weight=1)
        self._conn_dot = tk.Label(status_frame, text="●", fg=CLR_WARN,
                                  bg=CLR_SURFACE, font=("", 12))
        self._conn_dot.pack(side="left", padx=(0, 6))
        ttk.Label(status_frame, textvariable=self.status_text,
                  background=CLR_SURFACE, foreground=CLR_ACCENT, font=("", 8)).pack(side="left")

        self._adv_toggle_btn = tk.Button(conn, text="▼ ADVANCED", command=self._toggle_advanced,
                                         bg=CLR_SURFACE, fg=CLR_BORDER_DIM, relief="flat",
                                         font=("", 9), padx=8, pady=4)
        self._adv_toggle_btn.grid(row=0, column=4, sticky="e", pady=(6, 0))

        # Advanced sub-frame (hidden by default)
        self._adv_frame = tk.Frame(conn, bg=CLR_SURFACE)
        self._adv_frame.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        self._adv_frame.grid_remove()

        sep = ttk.Separator(self._adv_frame, orient="horizontal")
        sep.grid(row=0, column=0, columnspan=8, sticky="ew", pady=(0, 8))

        # Port / User / Base addr
        ttk.Label(self._adv_frame, text="Port", background=CLR_SURFACE, foreground=CLR_TEXT).grid(
            row=1, column=0, sticky="w", padx=(0, 4))
        ttk.Entry(self._adv_frame, textvariable=self.port_var, width=8).grid(
            row=1, column=1, sticky="w", padx=(0, 16))

        ttk.Label(self._adv_frame, text="User", background=CLR_SURFACE, foreground=CLR_TEXT).grid(
            row=1, column=2, sticky="w", padx=(0, 4))
        ttk.Entry(self._adv_frame, textvariable=self.user_var, width=12).grid(
            row=1, column=3, sticky="w", padx=(0, 16))

        ttk.Label(self._adv_frame, text="Base address", background=CLR_SURFACE, foreground=CLR_TEXT).grid(
            row=1, column=4, sticky="w", padx=(0, 4))
        ttk.Entry(self._adv_frame, textvariable=self.base_var, width=16).grid(
            row=1, column=5, sticky="w")

        # Advanced buttons: Read back, Soft reset, Upload & compile, Upload bitfile, Force freq update
        btn_rb = ttk.Button(self._adv_frame, text="Read back", command=self.read_back)
        btn_rb.grid(row=2, column=0, padx=3, pady=(8, 0), sticky="w")
        _Tooltip(btn_rb, "Read current register values from hardware")

        btn_sr = ttk.Button(self._adv_frame, text="Soft reset", command=self.soft_reset)
        btn_sr.grid(row=2, column=1, padx=3, pady=(8, 0), sticky="w")
        _Tooltip(btn_sr, "Send a soft-reset pulse to the FPGA core")

        btn_uc = ttk.Button(self._adv_frame, text="Upload & compile", command=self.upload_and_compile)
        btn_uc.grid(row=2, column=2, padx=3, pady=(8, 0), sticky="w")
        _Tooltip(btn_uc, "SCP rp_pulse_ctl.c to the board and compile it")

        btn_ub = ttk.Button(self._adv_frame, text="Upload bitfile", command=self.upload_bitfile)
        btn_ub.grid(row=2, column=3, padx=3, pady=(8, 0), sticky="w")
        _Tooltip(btn_ub, "Upload red_pitaya_top.bit.bin and reprogram the FPGA")

        btn_ffu = ttk.Button(self._adv_frame, text="Force freq update", command=self._force_freq_update)
        btn_ffu.grid(row=2, column=4, padx=3, pady=(8, 0), sticky="w")

        # Info and warning text
        ttk.Label(self._adv_frame, textvariable=self.info_text,
                  background=CLR_SURFACE, foreground=CLR_TEXT, font=("", 8),
                  justify="left").grid(row=3, column=0, columnspan=5, sticky="ew", pady=(8, 0))

        ttk.Label(self._adv_frame, textvariable=self.freq_warning_text,
                  background=CLR_SURFACE, foreground=CLR_WARN, font=("", 8, "bold")).grid(
                  row=4, column=0, columnspan=5, sticky="ew", pady=(2, 0))

    def _toggle_advanced(self):
        if self._adv_visible:
            self._adv_frame.grid_remove()
            self._adv_toggle_btn.configure(text="▼ ADVANCED")
        else:
            self._adv_frame.grid()
            self._adv_toggle_btn.configure(text="▲ ADVANCED")
        self._adv_visible = not self._adv_visible

    def _build_stats(self, outer):
        stats_outer, stats_inner = self._make_cyber_frame(outer, "LIVE STATS")
        stats_outer.pack(fill="x", pady=(0, 10))
        stats_inner.configure(height=80)

        for col, (header, var, color) in enumerate([
            ("INPUT FREQ",  self.stat_input_freq_var,  CLR_ACCENT),
            ("OUTPUT FREQ", self.stat_output_freq_var, CLR_SUCCESS),
            ("DUTY CYCLE",  self.stat_duty_var,        CLR_ACCENT2),
            ("PHASE SHIFT", self.stat_phase_var,       CLR_TEXT),
        ]):
            tile = tk.Frame(stats_inner, bg=CLR_STAT_BG, padx=16, pady=8)
            tile.grid(row=0, column=col, sticky="nsew", padx=(0, 4 if col < 3 else 0))
            stats_inner.grid_columnconfigure(col, weight=1)
            tk.Label(tile, text=header, bg=CLR_STAT_BG,
                     fg=CLR_MUTED, font=self._font_stat_header).pack(anchor="w")
            tk.Label(tile, textvariable=var, bg=CLR_STAT_BG,
                     fg=color, font=self._font_stat_value).pack(anchor="w")

    def _build_controls(self, outer):
        ctrl_outer, ctrl = self._make_cyber_frame(outer, "CONTROLS")
        ctrl_outer.pack(fill="both", expand=True, pady=(0, 10))

        self._add_param_row(ctrl, 0,
                            label="Divider",
                            float_var=None, int_var=self.divider_var,
                            entry_var=self.divider_entry_var,
                            minv=DIV_MIN, maxv=DIV_MAX,
                            callback=self.on_divider_change,
                            ns_var=None,
                            scale_attr="divider_scale")

        self._add_param_row(ctrl, 1,
                            label="Width (duty cycle)",
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

        self._add_freq_offset_row(ctrl, 3)

        self._add_param_row(ctrl, 4,
                            label="Modulation frequency (Hz)",
                            float_var=self.mod_freq_var, int_var=None,
                            entry_var=self.mod_freq_entry_var,
                            minv=MOD_FREQ_MIN_HZ, maxv=MOD_FREQ_MAX_HZ,
                            callback=self.on_mod_freq_change,
                            ns_var=self.mod_freq_word_var,
                            scale_attr="mod_freq_scale")

        self._add_param_row(ctrl, 5,
                            label="Modulation amplitude (sweep / T)",
                            float_var=self.mod_amp_var, int_var=None,
                            entry_var=self.mod_amp_entry_var,
                            minv=MOD_AMP_MIN, maxv=MOD_AMP_MAX,
                            callback=self.on_mod_amp_change,
                            ns_var=self.mod_amp_word_var,
                            scale_attr="mod_amp_scale")

        btn_frame = tk.Frame(ctrl, bg=CLR_SURFACE)
        btn_frame.grid(row=6, column=0, columnspan=4, sticky="w", pady=(14, 0))

        ttk.Checkbutton(btn_frame, text="Enable output", variable=self.enable_var,
                        command=self.maybe_auto_apply).pack(side="left", padx=(0, 12))
        self.phase_mod_check = ttk.Checkbutton(
            btn_frame,
            text="Enable phase modulation",
            variable=self.phase_mod_enable_var,
            command=self.on_phase_mod_toggle,
        )
        self.phase_mod_check.pack(side="left", padx=(0, 12))
        ttk.Checkbutton(btn_frame, text="Auto apply", variable=self.auto_apply_var).pack(
            side="left", padx=(0, 12))
        btn_apply = ttk.Button(btn_frame, text="Apply now (Ctrl+↵)",
                               command=self.apply_now, style="Accent.TButton")
        btn_apply.pack(side="left", padx=(0, 6))
        _Tooltip(btn_apply, "Write divider/width/delay/modulation to hardware (Ctrl+Enter)")

    def _add_param_row(self, parent, row, label, float_var, int_var,
                       entry_var, minv, maxv, callback, ns_var, scale_attr):
        ttk.Label(parent, text=label, width=22, anchor="w",
                  background=CLR_SURFACE, foreground=CLR_TEXT).grid(
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
            ttk.Label(parent, textvariable=ns_var, style="Muted.TLabel", width=18).grid(
                row=row, column=3, sticky="w", pady=(6, 0))

        setattr(self, scale_attr, scale)
        setattr(self, f"{scale_attr}_entry", entry)
        scale.set(var.get())

    def _add_freq_offset_row(self, parent, row):
        """Frequency offset row: label + wide entry + word readback. No slider."""
        ttk.Label(parent, text="Freq offset (Hz)", width=22, anchor="w",
                  background=CLR_SURFACE, foreground=CLR_TEXT).grid(
            row=row, column=0, sticky="w", pady=(6, 0), padx=(0, 8))

        # Entry spans the slider column (col 1) so it gets the extra space.
        # width=20 fits "-12345678.901234" without truncation.
        entry = ttk.Entry(parent, textvariable=self.freq_offset_hz_entry_var, width=20)
        entry.grid(row=row, column=1, sticky="w", pady=(6, 0), padx=(0, 8))
        entry.bind("<Return>",   lambda e: self.on_freq_offset_change(None))
        entry.bind("<FocusOut>", lambda e: self.on_freq_offset_change(None))
        self.freq_offset_entry = entry

        ttk.Label(parent, textvariable=self.freq_offset_word_var,
                  style="Muted.TLabel", width=22).grid(
            row=row, column=2, columnspan=2, sticky="w", pady=(6, 0))

    def _build_waveform(self, outer):
        wf_outer, wf_inner = self._make_cyber_frame(outer, "WAVEFORM PREVIEW")
        wf_outer.pack(fill="x", pady=(0, 10))
        self._wf_canvas = tk.Canvas(wf_inner, height=195, bg=CLR_BG, highlightthickness=0)
        self._wf_canvas.pack(fill="x", expand=True)
        self._wf_canvas.bind("<Configure>", lambda e: self._draw_waveform())

    def _draw_waveform(self):
        if not hasattr(self, '_wf_canvas'):
            return  # Canvas not yet created
        c = self._wf_canvas
        c.delete("all")
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 20 or ch < 20:
            return

        margin_l = 72
        margin_r = 10
        tw = cw - margin_l - margin_r  # track width in pixels

        # Two horizontal tracks (bigger, more prominent)
        y_in_hi  = 14;  y_in_lo  = 48    # INPUT: 34px tall
        y_out_hi = 80;  y_out_lo = 140   # OUTPUT: 60px tall

        def _label(text, y_hi, y_lo, color, bold=False):
            font_spec = ("", 9, "bold") if bold else ("", 9)
            c.create_text(margin_l - 4, (y_hi + y_lo) // 2,
                          text=text, anchor="e", fill=color, font=font_spec)

        # Grid lines at track boundaries
        for y in (y_in_hi, y_in_lo, y_out_hi, y_out_lo):
            c.create_line(margin_l, y, margin_l + tw, y,
                          fill=CLR_GRID, dash=(4, 4), width=1)

        _label("INPUT",  y_in_hi,  y_in_lo,  CLR_MUTED, bold=True)
        _label("OUTPUT", y_out_hi, y_out_lo, CLR_ACCENT, bold=True)

        divider = max(1, self.divider_var.get())
        frac    = max(0.001, min(0.999, self.width_frac_var.get()))
        deg     = max(0.0,   min(180.0, self.delay_deg_var.get()))
        delay_frac = deg / 360.0

        # Keep a fixed time window of 32 input-clock periods so divider
        # changes are visible on the slower output waveform.
        n_in = 32
        in_pw = tw / n_in
        x = float(margin_l)
        for _ in range(n_in):
            mid = x + in_pw / 2
            pts = [x, y_in_lo, x, y_in_hi, mid, y_in_hi,
                   mid, y_in_lo, x + in_pw, y_in_lo]
            for i in range(0, len(pts) - 2, 2):
                c.create_line(pts[i], pts[i+1], pts[i+2], pts[i+3],
                              fill=CLR_MUTED, width=1)
            x += in_pw

        # ── Output: same time window, divided period/duty/phase applied ───
        out_pw = in_pw * divider
        n_out = max(1, int(n_in / divider))
        x = float(margin_l)
        for _ in range(n_out):
            d_px = out_pw * delay_frac
            h_px = out_pw * frac
            # low → delay → rising → high → falling → low
            pts = [
                x,             y_out_lo,
                x + d_px,      y_out_lo,
                x + d_px,      y_out_hi,
                x + d_px + h_px, y_out_hi,
                x + d_px + h_px, y_out_lo,
                x + out_pw,    y_out_lo,
            ]
            # Glow effect: draw thicker line first in dimmed color
            for i in range(0, len(pts) - 2, 2):
                c.create_line(pts[i], pts[i+1], pts[i+2], pts[i+3],
                              fill=CLR_BORDER_DIM, width=4)
            # Main line
            for i in range(0, len(pts) - 2, 2):
                c.create_line(pts[i], pts[i+1], pts[i+2], pts[i+3],
                              fill=CLR_ACCENT, width=2)
            x += out_pw

        # ── Annotations ────────────────────────────────────────────────────
        sep_y = ch - 18
        c.create_line(margin_l, sep_y, margin_l + tw, sep_y,
                      fill=CLR_GRID, width=1)

        in_ref_duty = frac * 100
        effective_phase_mod = self._effective_phase_mod_enabled()
        c.create_text(margin_l + tw / 2, ch - 8,
                      text=(
                          f"÷{divider}  |  duty {in_ref_duty:.1f}% of input period  |  "
                          f"phase mod {fmt_freq_hz(clamp_mod_freq_hz(self.mod_freq_var.get()))} sweep 0..T"
                          if effective_phase_mod
                          else f"÷{divider}  |  duty {in_ref_duty:.1f}% of input period  |  delay {deg:.1f}°"
                      ),
                      fill=CLR_TEXT, font=("", 9))

    def _build_readback(self, outer):
        rb_outer, rb_inner = self._make_cyber_frame(outer, "REGISTER READBACK")
        rb_outer.pack(fill="x")
        ttk.Label(rb_inner, textvariable=self.readback_text,
                  background=CLR_SURFACE, foreground=CLR_TEXT,
                  font=_best_font(["JetBrains Mono", "Menlo", "Consolas", "Courier New"], 8),
                  justify="left").pack(anchor="w")

    # ── Connection ────────────────────────────────────────────────────────────

    def _set_connected(self, connected: bool):
        self.connected = connected
        if self._conn_dot:
            self._conn_dot.config(fg=CLR_SUCCESS if connected else CLR_WARN)
        host = self.host_var.get().strip()
        self.root.title(
            f"Red Pitaya Pulse Control — {host}" if connected
            else "Red Pitaya Pulse Control"
        )

    def connect(self):
        try:
            host = self.host_var.get().strip()
            user = self.user_var.get().strip()
            port = int(self.port_var.get().strip())
            self.base_addr = int(self.base_var.get().replace("_", ""), 0)
            self.remote.connect(host, user, port)
            self._set_connected(True)
            self.status_text.set("Loading FPGA bitstream…")
            self.root.update_idletasks()
            self.remote.run(f"{REMOTE_FPGAUTIL} -b {REMOTE_BITFILE}")
            self.status_text.set(f"Connected to {user}@{host}:{port}.")
            self.read_back()
            self._start_poll()
        except Exception as exc:
            self._set_connected(False)
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

    def _effective_phase_mod_enabled(self):
        # Matches FPGA behaviour: modulation only activates after period_stable
        # (8 consecutive valid periods), not merely period_valid.
        return self.phase_mod_enable_var.get() and self._period_stable and not self._timeout_flag

    def _phase_mod_requested(self):
        return self.phase_mod_enable_var.get()

    def _update_modulation_controls(self):
        delay_state = "disabled" if self._effective_phase_mod_enabled() else "normal"
        self.delay_scale.configure(state=delay_state)
        self.delay_scale_entry.configure(state=delay_state)
        self.mod_freq_scale.configure(state="normal" if self._phase_mod_requested() else "disabled")
        self.mod_freq_scale_entry.configure(state="normal" if self._phase_mod_requested() else "disabled")
        self.mod_amp_scale.configure(state="normal" if self._phase_mod_requested() else "disabled")
        self.mod_amp_scale_entry.configure(state="normal" if self._phase_mod_requested() else "disabled")
        self.phase_mod_check.configure(state="normal")

    def _update_info_text(self):
        if self._period_cycles <= 1:
            return
        divider    = max(DIV_MIN, min(DIV_MAX, self.divider_var.get()))
        input_hz   = CLOCK_HZ / self._period_cycles
        divided_hz = input_hz / divider
        input_period_s = self._period_cycles / CLOCK_HZ
        mode_text = (
            f"Phase modulation: {fmt_freq_hz(clamp_mod_freq_hz(self.mod_freq_var.get()))}, delay sweep 0..T"
            if self._effective_phase_mod_enabled()
            else "Static delay mode"
        )
        self.info_text.set(
            f"Input: {fmt_freq_hz(input_hz)}  |  Divider: ÷{divider}  |  "
            f"Divided: {fmt_freq_hz(divided_hz)}\n"
            f"Width/Delay ref: input period {fmt_time_s(input_period_s)}  ({self._period_cycles} cycles)  |  {mode_text}"
        )

    def _capture_apply_state(self):
        divider = max(DIV_MIN, min(DIV_MAX, self.divider_var.get()))
        frac = max(0.0, min(1.0, self.width_frac_var.get()))
        deg = max(0.0, min(180.0, self.delay_deg_var.get()))
        mod_freq_hz = clamp_mod_freq_hz(self.mod_freq_var.get())
        mod_amp = clamp_mod_amp(self.mod_amp_var.get())
        control_word = 0
        if self.enable_var.get():
            control_word |= CONTROL_PULSE_ENABLE
        if self._phase_mod_requested():
            control_word |= CONTROL_PHASE_MOD_ENABLE
        return {
            "divider": divider,
            "width_cycles": frac_to_cycles(frac, self._period_cycles),
            "delay_cycles": deg_to_cycles(deg, self._period_cycles),
            "phase_freq_word": mod_freq_to_word(mod_freq_hz),
            "phase_amp_q15": mod_amp_to_q15(mod_amp),
            "phase_step_offset": hz_to_phase_step_offset(self.freq_offset_hz_var.get()),
            "control_word": control_word,
        }

    def _queue_apply(self, source):
        if not self.connected:
            return

        state = self._capture_apply_state()
        should_start = False
        with self._apply_state_lock:
            self._pending_apply_state = state
            if self._apply_thread is None or not self._apply_thread.is_alive():
                self._apply_thread = threading.Thread(target=self._apply_worker, daemon=True)
                should_start = True

        if should_start:
            self.status_text.set("Auto-applying…" if source == "auto" else "Applying…")
            self._apply_thread.start()
        else:
            self.status_text.set("Apply queued…")

    def _apply_worker(self):
        while True:
            with self._apply_state_lock:
                state = self._pending_apply_state
                self._pending_apply_state = None
                if state is None:
                    self._apply_thread = None
                    return

            try:
                data = self.remote.helper(
                    self.base_addr,
                    "write",
                    state["divider"],
                    state["width_cycles"],
                    state["delay_cycles"],
                    state["phase_freq_word"],
                    state["phase_amp_q15"],
                    state["control_word"],
                )
            except Exception as exc:
                self.root.after(0, self._handle_apply_error, str(exc))
                continue

            self.root.after(0, self._finish_apply, state, data)

    def _finish_apply(self, state, data):
        self._update_readback(data)
        if int(data.get("phase_freq", -1)) == state["phase_freq_word"]:
            self._mod_freq_dirty = False
        if int(data.get("phase_amp_q15", -1)) == state["phase_amp_q15"]:
            self._mod_amp_dirty = False
        mode_text = "phase mod ON" if (state["control_word"] & CONTROL_PHASE_MOD_ENABLE) else "phase mod OFF"
        self.status_text.set(
            f"Applied — width {state['width_cycles']} cyc, delay {state['delay_cycles']} cyc, {mode_text}."
        )

    def _handle_apply_error(self, message):
        messagebox.showerror("Apply failed", message)
        self.status_text.set("Apply failed.")

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
        # Update stat duty cycle
        div = max(1, new_val)
        self.stat_duty_var.set(f"{self.width_frac_var.get() * 100:.1f} %")
        self._draw_waveform()
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
        self.width_ns_var.set(f"{new_val*100:.1f}%  {fmt_time_s(w_cyc / CLOCK_HZ)}")
        # Update stat duty cycle
        self.stat_duty_var.set(f"{new_val * 100:.1f} %")
        self.updating_widgets = False
        self._draw_waveform()
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
        # Update stat phase
        self.stat_phase_var.set(f"{new_val:.1f} °")
        self.updating_widgets = False
        self._draw_waveform()
        self.maybe_auto_apply()

    def on_freq_offset_change(self, value):
        if self.updating_widgets:
            return
        try:
            new_val = float(self.freq_offset_hz_entry_var.get().strip())
        except ValueError:
            new_val = self.freq_offset_hz_var.get()
        self.updating_widgets = True
        self.freq_offset_hz_var.set(new_val)
        # Show 6 decimal places so sub-mHz values are visible
        self.freq_offset_hz_entry_var.set(f"{new_val:.6f}")
        word = hz_to_phase_step_offset(new_val)
        self.freq_offset_word_var.set(
            f"step offset {word:+d}  ({fmt_freq_offset_hz(new_val)})")
        self.updating_widgets = False
        self.maybe_auto_apply()

    def on_mod_freq_change(self, value):
        if self.updating_widgets:
            return
        if value is None:
            try:
                new_val = float(self.mod_freq_entry_var.get().strip())
            except ValueError:
                new_val = self.mod_freq_var.get()
        else:
            new_val = float(value)
        clamped = clamp_mod_freq_hz(new_val)
        self._phase_freq_clamped = abs(clamped - new_val) > 1e-9
        self._mod_freq_dirty = True
        self.updating_widgets = True
        self.mod_freq_var.set(clamped)
        self.mod_freq_entry_var.set(f"{clamped:.0f}")
        self.mod_freq_scale.set(clamped)
        self.mod_freq_word_var.set(f"DDS 0x{mod_freq_to_word(clamped):08X}")
        self.updating_widgets = False
        self._update_info_text()
        self._draw_waveform()
        self.maybe_auto_apply()

    def on_mod_amp_change(self, value):
        if self.updating_widgets:
            return
        if value is None:
            try:
                new_val = float(self.mod_amp_entry_var.get().strip())
            except ValueError:
                new_val = self.mod_amp_var.get()
        else:
            new_val = float(value)
        clamped = clamp_mod_amp(new_val)
        self._mod_amp_dirty = True
        self.updating_widgets = True
        self.mod_amp_var.set(clamped)
        self.mod_amp_entry_var.set(f"{clamped:.2f}")
        self.mod_amp_scale.set(clamped)
        self.mod_amp_word_var.set(f"Q15 {mod_amp_to_q15(clamped)}")
        self.updating_widgets = False
        self._update_info_text()
        self._draw_waveform()
        self.maybe_auto_apply()

    def on_phase_mod_toggle(self):
        self._update_modulation_controls()
        if self._effective_phase_mod_enabled():
            self.delay_ns_var.set("ignored while phase modulation is active")
        else:
            self.delay_ns_var.set(fmt_time_s(
                deg_to_cycles(self.delay_deg_var.get(), self._period_cycles) / CLOCK_HZ))
        self._update_info_text()
        self._draw_waveform()
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
        self._queue_apply(source="auto")

    def apply_now(self):
        if not self.connected:
            return
        if self._auto_apply_job is not None:
            self.root.after_cancel(self._auto_apply_job)
            self._auto_apply_job = None
        self._queue_apply(source="manual")

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
        raw_period  = int(data.get("period", data.get("raw_period", 0)))
        filt_period = int(data.get("period_avg", data.get("filt_period", 0)))
        phase_freq  = int(data.get("phase_freq", 0))
        phase_amp_q15 = int(data.get("phase_amp_q15", 0))
        # NCO registers (signed 48-bit, may not be present on older firmware)
        phase_step_offset_raw = int(data.get("phase_step_offset", 0))
        phase_step_base_raw   = int(data.get("phase_step_base",   0))
        phase_step_raw        = int(data.get("phase_step",        0))
        # Sign-extend 48-bit values
        if phase_step_offset_raw >= 2**(NCO_WIDTH - 1):
            phase_step_offset_raw -= 2**NCO_WIDTH
        if phase_step_base_raw >= 2**(NCO_WIDTH - 1):
            phase_step_base_raw -= 2**NCO_WIDTH
        if phase_step_raw >= 2**(NCO_WIDTH - 1):
            phase_step_raw -= 2**NCO_WIDTH

        busy           = (status >> 0) & 0x1
        period_valid   = (status >> 1) & 0x1
        timeout_flag   = (status >> 2) & 0x1
        period_stable  = int(data.get("period_stable", (status >> 3) & 0x1))
        enable         = control & CONTROL_PULSE_ENABLE
        phase_mod_enable = (control & CONTROL_PHASE_MOD_ENABLE) != 0
        self._period_valid  = bool(period_valid)
        self._timeout_flag  = bool(timeout_flag)
        self._period_stable = bool(period_stable)

        # Update internal period reference from filtered hardware measurement.
        # Only apply if change exceeds 5% or a force update was requested.
        if period_valid and filt_period > 0:
            change = abs(filt_period - self._period_cycles) / max(1, self._period_cycles)
            if self._force_period_update or self._period_cycles <= 1 or change > 0.05:
                self._force_period_update = False
                self._period_cycles = filt_period
                self._update_info_text()
                frac = self.width_frac_var.get()
                w_cyc = frac_to_cycles(frac, filt_period)
                self.width_ns_var.set(f"{frac*100:.1f}%  {fmt_time_s(w_cyc / CLOCK_HZ)}")
                if self._effective_phase_mod_enabled():
                    self.delay_ns_var.set("ignored while phase modulation is active")
                else:
                    self.delay_ns_var.set(fmt_time_s(
                        deg_to_cycles(self.delay_deg_var.get(), filt_period) / CLOCK_HZ))

        raw_freq  = CLOCK_HZ / raw_period  if raw_period  > 0 else 0.0
        filt_freq = CLOCK_HZ / filt_period if filt_period > 0 else 0.0
        mod_freq_hz = phase_freq * CLOCK_HZ / (2**32)
        mod_amp = q15_to_mod_amp(phase_amp_q15)

        # Update live stats from hardware data
        divider_hw = max(1, divider)
        out_freq   = filt_freq / divider_hw if filt_freq > 0 else 0.0
        self.stat_input_freq_var.set(fmt_freq_hz(filt_freq) if filt_period > 0 else "—")
        self.stat_output_freq_var.set(fmt_freq_hz(out_freq)  if filt_period > 0 else "—")
        frac = self.width_frac_var.get()
        self.stat_duty_var.set(f"{frac * 100:.1f} %")
        if phase_mod_enable and period_stable and not timeout_flag:
            self.stat_phase_var.set(fmt_freq_hz(mod_freq_hz))
        else:
            self.stat_phase_var.set(f"{self.delay_deg_var.get():.1f} °")

        width_frac = cycles_to_frac(width, self._period_cycles)
        delay_deg  = cycles_to_deg(delay,  self._period_cycles)

        blocked_phase_mod = self.phase_mod_enable_var.get() and (not period_stable or timeout_flag)
        self.enable_var.set(bool(enable))
        clamped_mod_freq_hz = clamp_mod_freq_hz(mod_freq_hz)
        should_sync_mod_freq = (phase_mod_enable or phase_freq != 0) and not self._mod_freq_dirty
        if should_sync_mod_freq and abs(self.mod_freq_var.get() - clamped_mod_freq_hz) > 0.5:
            self.mod_freq_var.set(clamped_mod_freq_hz)
            self.mod_freq_entry_var.set(f"{clamped_mod_freq_hz:.0f}")
            self.mod_freq_scale.set(clamped_mod_freq_hz)
        self.mod_freq_word_var.set(f"DDS 0x{mod_freq_to_word(self.mod_freq_var.get()):08X}")
        clamped_mod_amp = clamp_mod_amp(mod_amp)
        should_sync_mod_amp = (phase_mod_enable or phase_amp_q15 != 0) and not self._mod_amp_dirty
        if should_sync_mod_amp and abs(self.mod_amp_var.get() - clamped_mod_amp) > 0.01:
            self.mod_amp_var.set(clamped_mod_amp)
            self.mod_amp_entry_var.set(f"{clamped_mod_amp:.2f}")
            self.mod_amp_scale.set(clamped_mod_amp)
        self.mod_amp_word_var.set(f"Q15 {mod_amp_to_q15(self.mod_amp_var.get())}")
        self._update_modulation_controls()
        self._update_info_text()
        self._draw_waveform()

        warnings = []
        if blocked_phase_mod:
            if not period_stable:
                warnings.append("Phase modulation waiting for period_stable (8 consecutive valid periods). Static delay active.")
            else:
                warnings.append("Phase modulation blocked by trigger timeout. Static delay active.")
        if not period_valid:
            warnings.append("No valid trigger period.")
        if timeout_flag:
            warnings.append("Trigger timeout detected on STATUS.bit2.")
        if self._phase_freq_clamped:
            warnings.append("Modulation frequency was clamped to 25 kHz.")
        self.freq_warning_text.set("  ".join(f"⚠  {text}" for text in warnings))

        status_str = (f"busy={busy}  valid={period_valid}  "
                      f"stable={period_stable}  timeout={timeout_flag}")

        # Sync freq offset entry from hardware if present (non-zero or explicitly sent)
        if phase_step_offset_raw != 0:
            hw_offset_hz = phase_step_offset_to_hz(phase_step_offset_raw)
            if abs(hw_offset_hz - self.freq_offset_hz_var.get()) > 1e-7:
                self.freq_offset_hz_var.set(hw_offset_hz)
                self.freq_offset_hz_entry_var.set(f"{hw_offset_hz:.6f}")
                word = hz_to_phase_step_offset(hw_offset_hz)
                self.freq_offset_word_var.set(
                    f"step offset {word:+d}  ({fmt_freq_offset_hz(hw_offset_hz)})")

        offset_hz   = phase_step_offset_to_hz(phase_step_offset_raw)
        base_hz     = phase_step_offset_to_hz(phase_step_base_raw)
        live_hz     = phase_step_offset_to_hz(phase_step_raw)

        self.readback_text.set(
            f"control  = 0x{control:08X}    enable = {1 if enable else 0}  phase_mod = {1 if phase_mod_enable else 0}\n"
            f"divider  = {divider}\n"
            f"width    = {width:6d} cycles  ({fmt_time_s(width / CLOCK_HZ):>10})  →  {width_frac:.3f} duty\n"
            f"delay    = {delay:6d} cycles  ({fmt_time_s(delay / CLOCK_HZ):>10})  →  {delay_deg:.1f}°\n"
            f"phase_fr = 0x{phase_freq:08X}  ({fmt_freq_hz(mod_freq_hz):>12})\n"
            f"phase_amp= {phase_amp_q15:6d}  ({mod_amp:.2f}T sweep)\n"
            f"step_off = {phase_step_offset_raw:+d}  ({offset_hz:+.6f} Hz)\n"
            f"step_base= {phase_step_base_raw}  ({base_hz:.6f} Hz)\n"
            f"step_live= {phase_step_raw}  ({live_hz:.6f} Hz)\n"
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
