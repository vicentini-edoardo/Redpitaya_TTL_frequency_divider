"""
Stroboscopic illumination — oscillating delay mode visualization.

Physical setup:
  - Periodic sample process at f_in (e.g., cantilever, phonon, SNOM tip oscillation)
  - Pulsed laser synchronized to f_in, 1% duty cycle
  - Oscillating delay shifts the laser-sample phase: P0 ± P at rate f_osc
  - Detector integrates many pulses → reconstructs time-domain waveform shape

Usage:
    python3 strobo_sim.py
    python3 strobo_sim.py --P0 0.25 --P 0.15 --f_osc 5000
"""

import argparse
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.colors import Normalize
from matplotlib import cm

# ── CLI args ───────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--f_in",  type=float, default=250_000, help="Process frequency (Hz)")
parser.add_argument("--f_osc", type=float, default=10_000,  help="Oscillation frequency (Hz)")
parser.add_argument("--duty",  type=float, default=0.01,    help="Illumination duty cycle [0-1]")
parser.add_argument("--P0",    type=float, default=0.0,     help="Centre phase offset [fraction of T_in]")
parser.add_argument("--P",     type=float, default=0.20,    help="Phase amplitude [fraction of T_in]")
args = parser.parse_args()

f_in   = args.f_in
f_osc  = args.f_osc
duty   = args.duty
P0     = args.P0
P      = args.P

# ── Derived NCO parameters ─────────────────────────────────────────────────────
CLK_HZ  = 124_999_999
T_in    = 1 / f_in
T_osc   = 1 / f_osc
f_shift = 4 * f_osc * P         # NCO frequency offset
delta   = f_shift / f_in        # phase advance per f_in period
ratio   = f_in / f_osc          # f_in periods per f_osc period

print(f"{'─'*55}")
print(f"f_in    = {f_in/1e3:.1f} kHz   T_in  = {T_in*1e6:.2f} µs")
print(f"f_osc   = {f_osc/1e3:.1f} kHz   T_osc = {T_osc*1e6:.0f} µs")
print(f"f_shift = {f_shift:.0f} Hz")
print(f"Phase range   : {(P0-P)*100:.1f}% → {(P0+P)*100:.1f}% of T_in")
print(f"Phase step/T_in: {delta*100:.3f}%  ({ratio:.1f} pulses per oscillation)")
print(f"{'─'*55}")

# ── Physical signal: complex multi-harmonic (not a pure sine) ──────────────────
def physical_signal(phi_frac):
    """
    phi_frac: fractional phase [0, 1) within one T_in period.
    Resembles a nonlinear optical response with fast rise and slow decay.
    """
    phi = 2 * np.pi * phi_frac
    return (0.55 * np.sin(phi)
          + 0.28 * np.sin(2*phi + 1.05)
          + 0.13 * np.sin(3*phi - 0.55)
          + 0.07 * np.cos(4*phi + 0.30)
          + 0.03 * np.sin(7*phi - 1.10))

# ── Triangle-wave phase oscillation ───────────────────────────────────────────
def illum_phase(t):
    """
    Fractional phase [P0-P, P0+P] of illumination at time t.
    Triangle wave: P0-P at t=0, P0+P at t=T_osc/2, P0-P at t=T_osc.
    """
    t_norm = (t * f_osc) % 1.0
    tri    = 1.0 - 2.0 * np.abs(2.0 * t_norm - 1.0)   # -1 → +1 → -1
    return P0 + P * tri

# ── Sampled intensity: integrate signal over illumination pulse width ──────────
def sampled_intensity(phi_frac, n_pts=30):
    s = np.linspace(0, duty, n_pts)
    return np.mean(physical_signal((phi_frac + s) % 1.0))

# ── Generate data ──────────────────────────────────────────────────────────────

# 3 original periods (continuous waveform)
t_orig    = np.linspace(0, 3*T_in, 5000)
sig_orig  = physical_signal((t_orig * f_in) % 1.0)

# Illumination pulses during 3 original periods
pulse_t3 = np.array([0.0, T_in, 2*T_in])
pulse_phi3 = illum_phase(pulse_t3)        # fractional phase [P0-P, P0+P]
# Actual time marker within each T_in period (the sample is captured here)
pulse_t_mark = np.array([
    n * T_in + (phi % 1.0) * T_in
    for n, phi in enumerate(pulse_phi3)
])
pulse_sig3 = np.array([sampled_intensity(phi) for phi in pulse_phi3])

# 3 stroboscopic periods (one sample per f_in pulse)
n_strobo      = round(3 * ratio)           # 75 pulses
t_strobo      = np.arange(n_strobo) * T_in
phi_strobo    = illum_phase(t_strobo)
sig_strobo    = np.array([sampled_intensity(p) for p in phi_strobo])

# Reference waveform over the phase range [P0-P, P0+P] for overlay
phi_ref  = np.linspace(P0 - P, P0 + P, 500)
sig_ref  = physical_signal(phi_ref % 1.0)

# ── Colour map: phase → colour ─────────────────────────────────────────────────
cmap  = cm.plasma
norm  = Normalize(vmin=P0 - P, vmax=P0 + P)

# ── Figure ─────────────────────────────────────────────────────────────────────
fig = plt.figure(figsize=(15, 10), facecolor='#0f0f0f')
fig.suptitle(
    f"Stroboscopic illumination — oscillating delay mode\n"
    f"f_in = {f_in/1e3:.0f} kHz    f_osc = {f_osc/1e3:.0f} kHz    "
    f"duty = {duty*100:.0f}%    P0 = {P0*100:.0f}%    P = ±{P*100:.0f}% of T_in",
    color='white', fontsize=12, y=0.98
)

gs = gridspec.GridSpec(2, 2, figure=fig,
                       hspace=0.42, wspace=0.35,
                       left=0.07, right=0.95, top=0.91, bottom=0.08)

dark_bg   = '#1a1a2e'
dark_grid = '#2a2a3e'
txt_col   = '#e0e0e0'

def style_ax(ax, title='', xlabel='', ylabel=''):
    ax.set_facecolor(dark_bg)
    ax.tick_params(colors=txt_col, labelsize=9)
    ax.xaxis.label.set_color(txt_col)
    ax.yaxis.label.set_color(txt_col)
    ax.title.set_color(txt_col)
    ax.set_title(title, fontsize=10, pad=6)
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel(ylabel, fontsize=9)
    for sp in ax.spines.values():
        sp.set_color('#444466')
    ax.grid(True, color=dark_grid, alpha=0.8, lw=0.5)

# ── Panel A: Original signal × 3 periods ──────────────────────────────────────
ax_A = fig.add_subplot(gs[0, :])   # full top row
style_ax(ax_A,
         title=f'Original sample process (3 periods of f_in = {f_in/1e3:.0f} kHz)',
         xlabel='Time (µs)', ylabel='Signal amplitude (a.u.)')

ax_A.plot(t_orig*1e6, sig_orig, color='#4a9ede', lw=1.2, label='Physical process signal', zorder=2)

# Mark illumination pulses
pulse_width_vis = 0.15e-6  # wider than actual (40 ns) for visibility
for i, (tm, tmark, phi, sv) in enumerate(zip(pulse_t3, pulse_t_mark, pulse_phi3, pulse_sig3)):
    color = cmap(norm(phi))
    # Highlight the sampled instant within each period
    ax_A.axvspan((tmark - pulse_width_vis/2)*1e6, (tmark + pulse_width_vis/2)*1e6,
                 alpha=0.7, color=color, zorder=3)
    ax_A.plot(tmark*1e6, sv, 'o', color=color, ms=9, zorder=6,
              markeredgecolor='white', markeredgewidth=0.8)
    ax_A.annotate(
        f'pulse {i}\nφ = {phi*100:+.1f}%',
        xy=(tmark*1e6, sv),
        xytext=(tmark*1e6 + 0.1, sv + 0.12),
        fontsize=8, color=color,
        arrowprops=dict(arrowstyle='->', color=color, lw=0.8)
    )

ax_A.legend(loc='upper right', fontsize=8,
            facecolor='#1a1a2e', edgecolor='#444466', labelcolor=txt_col)
ax_A.set_xlim(-0.3, 3*T_in*1e6 + 0.3)

# Period separators
for k in [1, 2]:
    ax_A.axvline(k*T_in*1e6, color='#666688', lw=0.8, ls=':', alpha=0.7)

note = (f"3 consecutive laser pulses sample phases:\n"
        f"{pulse_phi3[0]*100:.1f}% → {pulse_phi3[1]*100:.1f}% → {pulse_phi3[2]*100:.1f}% of T_in\n"
        f"(step = {delta*100:.2f}% per period = Δ)")
ax_A.text(0.01, 0.04, note, transform=ax_A.transAxes,
          fontsize=8, color='#aaaacc', va='bottom',
          bbox=dict(boxstyle='round', facecolor='#0f0f1f', alpha=0.7, edgecolor='#444466'))

# ── Panel B (bottom-left): Stroboscopic time trace × 3 f_osc periods ──────────
ax_B = fig.add_subplot(gs[1, 0])
style_ax(ax_B,
         title=f'Stroboscopic trace × 3 oscillation periods',
         xlabel='Time (µs)', ylabel='Detected intensity (a.u.)')

t_ms_strobo = t_strobo * 1e6   # µs
sc = ax_B.scatter(t_ms_strobo, sig_strobo,
                  c=phi_strobo, cmap='plasma', norm=norm,
                  s=20, zorder=3, edgecolors='none', alpha=0.9)

# Oscillation period markers
for k in range(4):
    ax_B.axvline(k * T_osc * 1e6, color='#ff9944', lw=0.8, ls=':', alpha=0.7)

ax_B.text(0.01, 0.95, f'f_osc = {f_osc/1e3:.0f} kHz  ({ratio:.0f} pulses/period)',
          transform=ax_B.transAxes, fontsize=8, color='#ff9944', va='top')

# Right axis: phase evolution
ax_Br = ax_B.twinx()
ax_Br.plot(t_ms_strobo, phi_strobo * 100, color='#666688', lw=0.8, ls='--', alpha=0.6)
ax_Br.set_ylabel('Illumination phase (% T_in)', color='#8888aa', fontsize=8)
ax_Br.tick_params(axis='y', labelcolor='#888888', labelsize=8)
ax_Br.set_ylim((P0 - P - 0.05)*100, (P0 + P + 0.05)*100)
ax_Br.axhline((P0 + P)*100, color='#666688', lw=0.5, ls=':')
ax_Br.axhline((P0 - P)*100, color='#666688', lw=0.5, ls=':')
ax_Br.set_facecolor(dark_bg)
for sp in ax_Br.spines.values():
    sp.set_color('#444466')

# ── Panel C (bottom-right): Recovered waveform vs phase ───────────────────────
ax_C = fig.add_subplot(gs[1, 1])
style_ax(ax_C,
         title='Recovered waveform (phase scan)',
         xlabel='Illumination phase (% of T_in)', ylabel='Detected intensity (a.u.)')

# Original reference (full period, gray)
phi_full = np.linspace(0, 1, 500)
sig_full = physical_signal(phi_full)
ax_C.plot(phi_full*100, sig_full, color='#444466', lw=1.5, ls='--',
          label='Original (full period)', zorder=1)

# Scanned range overlay
ax_C.axvspan((P0-P)*100, (P0+P)*100, alpha=0.08, color='white', zorder=0)

# Recovered: each sample plotted at its UNWRAPPED phase (P0-P to P0+P)
sc2 = ax_C.scatter(phi_strobo*100, sig_strobo,
                   c=t_ms_strobo, cmap='viridis',
                   s=18, zorder=3, edgecolors='none', alpha=0.85,
                   label='Stroboscopic samples')

# Reference waveform over scanned range (unwrapped phase, % of T_in)
ax_C.plot(phi_ref*100, sig_ref, color='#ff6644', lw=1.5, ls='-',
          label='True signal in scan range', zorder=2, alpha=0.8)

ax_C.legend(loc='upper right', fontsize=7,
            facecolor='#1a1a2e', edgecolor='#444466', labelcolor=txt_col)
ax_C.set_xlim((P0-P)*100 - 3, (P0+P)*100 + 3)

# Colourbar for panel B (phase colour)
cbar = fig.colorbar(sc, ax=ax_B, pad=0.01, fraction=0.04)
cbar.set_label('Illumination phase (frac. of T_in)', color=txt_col, fontsize=8)
cbar.ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x*100:.0f}%'))
cbar.ax.tick_params(colors=txt_col, labelsize=7)

# Colourbar for panel C (time ordering)
cbar2 = fig.colorbar(sc2, ax=ax_C, pad=0.01, fraction=0.04)
cbar2.set_label('Time (µs)', color=txt_col, fontsize=8)
cbar2.ax.tick_params(colors=txt_col, labelsize=7)

plt.savefig('/tmp/strobo_sim.png', dpi=150, facecolor='#0f0f0f')
print("Saved → /tmp/strobo_sim.png")
plt.show()
