# Graph Report - Redpitaya_TTL_frequency_divider  (2026-07-13)

## Corpus Check
- 23 files · ~39,458 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 543 nodes · 1103 edges · 33 communities (21 shown, 12 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 29 edges (avg confidence: 0.61)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `b0b643bc`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_GUI PulseHarmonic Panels|GUI Pulse/Harmonic Panels]]
- [[_COMMUNITY_PicoSDK Verify CLI|PicoSDK Verify CLI]]
- [[_COMMUNITY_SSH Backend Job Queue|SSH Backend Job Queue]]
- [[_COMMUNITY_Dark Workbench Style Tokens|Dark Workbench Style Tokens]]
- [[_COMMUNITY_redpitaya_picosdk_verify.py|redpitaya_picosdk_verify.py]]
- [[_COMMUNITY_run_hardware_suite|run_hardware_suite]]
- [[_COMMUNITY_OscPanel|OscPanel]]
- [[_COMMUNITY_Edge-Lock Phase Offset Math|Edge-Lock Phase Offset Math]]
- [[_COMMUNITY_ApplyOscillator Math|Apply/Oscillator Math]]
- [[_COMMUNITY_osc_delay_sim.py|osc_delay_sim.py]]
- [[_COMMUNITY_rp_ctl.c Register IO|rp_ctl.c Register IO]]
- [[_COMMUNITY_PulseHarmonic Mode Overview|Pulse/Harmonic Mode Overview]]
- [[_COMMUNITY_harmonic_phase_offset_to_preload|harmonic_phase_offset_to_preload]]
- [[_COMMUNITY_FPGA Register Map & Precision|FPGA Register Map & Precision]]
- [[_COMMUNITY_GUI Layout Tests|GUI Layout Tests]]
- [[_COMMUNITY_FPGA RTL Top-Level|FPGA RTL Top-Level]]
- [[_COMMUNITY_Measurement Window Editable Field Design|Measurement Window Editable Field Design]]
- [[_COMMUNITY_Output Mode Buttons|Output Mode Buttons]]
- [[_COMMUNITY_strobo_sim.py|strobo_sim.py]]
- [[_COMMUNITY_paramiko Dependency Pin|paramiko Dependency Pin]]
- [[_COMMUNITY_AXI4-Lite Pulse Regs RTL|AXI4-Lite Pulse Regs RTL]]
- [[_COMMUNITY_pulse_gen RTL|pulse_gen RTL]]
- [[_COMMUNITY_Measurement Window Editable Field Implementation Plan|Measurement Window Editable Field Implementation Plan]]
- [[_COMMUNITY_Connection Strip Component|Connection Strip Component]]
- [[_COMMUNITY_Monitor Tiles Component|Monitor Tiles Component]]
- [[_COMMUNITY_FPGA Register Map (README)|FPGA Register Map (README)]]
- [[_COMMUNITY_Out Of Scope (no backendregisterrp_math.py changes)|Out Of Scope (no backend/register/rp_math.py changes)]]
- [[_COMMUNITY_Update Branch Selection Implementation Plan|Update Branch Selection Implementation Plan]]
- [[_COMMUNITY_Harmonic Generator Mode (README)|Harmonic Generator Mode (README)]]
- [[_COMMUNITY_Pulse  Freq-Shift Mode (README)|Pulse / Freq-Shift Mode (README)]]
- [[_COMMUNITY___init__.py|__init__.py]]
- [[_COMMUNITY_hz_to_phase|hz_to_phase]]
- [[_COMMUNITY_rp_math.py|rp_math.py]]

## God Nodes (most connected - your core abstractions)
1. `SshBackend` - 44 edges
2. `_NcoPanel` - 38 edges
3. `MainWindow` - 27 edges
4. `TestWaveformAnalysis` - 23 edges
5. `AnalysisConfig` - 21 edges
6. `analyze_capture()` - 21 edges
7. `run_hardware_suite()` - 20 edges
8. `OscPanel` - 20 edges
9. `hz_to_phase()` - 20 edges
10. `RedPitayaCommandBuilder` - 19 edges

## Surprising Connections (you probably didn't know these)
- `Connection Panel (Host/Port/User/Key, Connect, Upload & Compile, status LED)` --conceptually_related_to--> `SshBackend`  [INFERRED]
  GUI.png → redpitaya_combined_gui_qt.py
- `PySide6-Essentials>=6.6` --references--> `MainWindow`  [INFERRED]
  requirements.txt → redpitaya_combined_gui_qt.py
- `Frequency Shift Status Line (requested/actual/register/resolution readout)` --shares_data_with--> `fmt_signed_freq()`  [INFERRED]
  GUI.png → rp_math.py
- `Controls Panel (Freq shift, Width, Meas. window, Enable Output, Auto-Apply, Apply Now, Soft Reset)` --conceptually_related_to--> `PulsePanel`  [INFERRED]
  GUI.png → redpitaya_combined_gui_qt.py
- `Measurement Stat Tiles (Input Frequency, Pulse Duration, Output Frequency, Duty Cycle)` --conceptually_related_to--> `PulsePanel`  [INFERRED]
  GUI.png → redpitaya_combined_gui_qt.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Connect / Upload & Compile / Disconnected status form the SSH connection workflow** — gui_connection_panel, gui_log_panel, redpitaya_combined_gui_qt_sshbackend [INFERRED 0.80]
- **Freq shift + Width entry, Apply Now/Auto-Apply, and the freq-shift status readout form the pulse-mode parameter apply flow** — gui_controls_panel, gui_freq_shift_status_line, gui_measurement_display_panel [INFERRED 0.80]

## Communities (33 total, 12 thin omitted)

### Community 0 - "GUI Pulse/Harmonic Panels"
Cohesion: 0.33
Nodes (5): Dark Workbench GUI Redesign Implementation Plan, Task 1: Add GUI Structure Smoke Test, Task 2: Redesign Style Tokens And Shared Components, Task 3: Rebuild Workbench Layout, Task 4: Verify And Polish

### Community 2 - "SSH Backend Job Queue"
Cohesion: 0.07
Nodes (10): QObject, Single persistent paramiko SSH session shared by both panels.      self._mode (", Pulse-mode modulated write (enable=1, harmonic_mode=0 via helper).          When, Harmonic-mode modulated write (enable=1, harmonic_mode=1 via helper).          W, Set control register via the pulse helper (keeps harmonic_mode=0)., Set control register via the harmonic helper (keeps harmonic_mode=1)., Write DIO2 48-bit NCO step. phase_step=0 disables., Set osc registers then write to enable (osc_mode + enable bits). (+2 more)

### Community 3 - "Dark Workbench Style Tokens"
Cohesion: 0.12
Nodes (22): QFont, QFrame, QGridLayout, QGroupBox, QIcon, QLabel, QLineEdit, QWidget (+14 more)

### Community 4 - "redpitaya_picosdk_verify.py"
Cohesion: 0.09
Nodes (41): ArgumentParser, Enum, Expectation, AnalysisConfig, analyze_capture(), _analyze_constant(), analyze_osc_delay(), build_arg_parser() (+33 more)

### Community 5 - "run_hardware_suite"
Cohesion: 0.11
Nodes (11): Any, configure_test(), estimate_input_hz(), _nearest_range_key(), Pico4000aScope, Small block-capture wrapper around the PicoSDK ps4000a Python module., RedPitayaCommandBuilder, RedPitayaSSH (+3 more)

### Community 6 - "OscPanel"
Cohesion: 0.09
Nodes (12): OscPanel, Public entry point (e.g. the Ctrl+Return shortcut) → mode-specific write., Oscillating delay: stroboscopic phase-scan via alternating NCO sign., f_osc_from_params(), f_shift_from_f_osc(), osc_half_period_cycles(), osc_phase_preload(), NCO frequency shift derived from oscillation rate and phase amplitude. (+4 more)

### Community 7 - "Edge-Lock Phase Offset Math"
Cohesion: 0.17
Nodes (10): Architecture, Board-side helper (`rp_ctl.c`), Commands, Development Guidance, FPGA register map, GUI (`redpitaya_combined_gui_qt.py`), Math helpers (`rp_math.py`), Project Overview (+2 more)

### Community 8 - "Apply/Oscillator Math"
Cohesion: 0.07
Nodes (8): HarmonicPanel, _mode_btn_style(), _NcoPanel, Harmonic generator mode (harmonic_mode=1): f_out = N × f_in + f_shift., Style for the three output-mode buttons (OFF / MODULATED / ON)., Shared UI and logic for the two NCO control tabs.      Both tabs poll the same s, harmonic_preload_to_phase_offset(), Inverse of :func:`harmonic_phase_offset_to_preload` → offset turns [0, 1).

### Community 9 - "osc_delay_sim.py"
Cohesion: 0.17
Nodes (16): check_edge_lock_shift(), check_limits(), fraction_to_acc(), hz_to_phase_step(), measured_step_base(), osc_half_period_cycles(), plot_results(), Oscillating Delay Mode — NCO simulation & verification.  Tick-accurate model of (+8 more)

### Community 10 - "rp_ctl.c Register IO"
Cohesion: 0.45
Nodes (11): off_t, detect_mode(), main(), print_json(), rd32(), rd48(), rd48u(), usage() (+3 more)

### Community 12 - "harmonic_phase_offset_to_preload"
Cohesion: 0.17
Nodes (6): harmonic_phase_offset_to_preload(), phase_offset_to_preload(), 48-bit accumulator preload for a constant edge-lock phase offset.      In edge-l, 48-bit accumulator preload for a constant edge-lock phase offset in     *harmoni, TestHarmonicPhaseOffsetPreload, TestPhaseOffsetPreload

### Community 14 - "GUI Layout Tests"
Cohesion: 0.10
Nodes (4): _FakeBackend, TestDarkWorkbenchLayout, TestGitUpdateHelpers, TestMeasurementWindowField

### Community 15 - "FPGA RTL Top-Level"
Cohesion: 0.40
Nodes (4): axi4lite_pulse_regs, pulse_gen, system_wrapper, red_pitaya_top

### Community 16 - "Measurement Window Editable Field Design"
Cohesion: 0.18
Nodes (10): Behavior, Data Flow, Error Handling, Goal, Measurement Window Editable Field Design, Recommended Approach, Risks, Scope (+2 more)

### Community 18 - "strobo_sim.py"
Cohesion: 0.29
Nodes (6): illum_phase(), physical_signal(), Stroboscopic illumination — oscillating delay mode visualization.  Physical setu, phi_frac: fractional phase [0, 1) within one T_in period.     Resembles a nonlin, Fractional phase [P0-P, P0+P] of illumination at time t.     Triangle wave: P0-P, sampled_intensity()

### Community 22 - "Measurement Window Editable Field Implementation Plan"
Cohesion: 0.40
Nodes (4): Measurement Window Editable Field Implementation Plan, Task 1: Add failing GUI tests for the editable window field, Task 2: Implement the editable millisecond field in the shared NCO panel, Task 3: Run regression verification for the touched GUI surface

### Community 25 - "FPGA Register Map (README)"
Cohesion: 0.06
Nodes (33): Current limitation, Frequency-match precision and its hard limits, Install dependencies, Interpreting common failures, osc_delay metrics in summary.json, Oscillating-delay measurement, Output bundle, Prepare the Red Pitaya (+25 more)

### Community 26 - "Out Of Scope (no backend/register/rp_math.py changes)"
Cohesion: 0.15
Nodes (12): Approved Direction, Components, Dark Workbench GUI Redesign, Error Handling And State, Functional Scope, Goal, Layout, Out Of Scope (+4 more)

### Community 27 - "Update Branch Selection Implementation Plan"
Cohesion: 0.50
Nodes (3): Task 1: Add regression coverage for the update helper, Task 2: Wire the helper into the Update button, Update Branch Selection Implementation Plan

### Community 36 - "hz_to_phase"
Cohesion: 0.06
Nodes (27): Connection Panel (Host/Port/User/Key, Connect, Upload & Compile, status LED), Controls Panel (Freq shift, Width, Meas. window, Enable Output, Auto-Apply, Apply Now, Soft Reset), Frequency Shift Status Line (requested/actual/register/resolution readout), Log Panel (scrolling status/debug output), Measurement Stat Tiles (Input Frequency, Pulse Duration, Output Frequency, Duty Cycle), Red Pitaya TTL Frequency Divider GUI Screenshot (Pulse Tab, Disconnected), PulsePanel, Pulse / frequency-shift mode (harmonic_mode=0): f_out = f_in + f_shift. (+19 more)

### Community 37 - "rp_math.py"
Cohesion: 0.08
Nodes (17): QMainWindow, _cleanup_legacy_repo_state(), _default_state_file(), _Job, _list_remote_branches(), main(), MainWindow, _parse_remote_branches() (+9 more)

## Knowledge Gaps
- **81 isolated node(s):** `axi4lite_pulse_regs`, `pulse_gen`, `system_wrapper`, `pulse_gen`, `axi4lite_pulse_regs` (+76 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **12 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SshBackend` connect `SSH Backend Job Queue` to `Apply/Oscillator Math`, `Dark Workbench Style Tokens`, `hz_to_phase`, `rp_math.py`?**
  _High betweenness centrality (0.102) - this node is a cross-community bridge._
- **Why does `_NcoPanel` connect `Apply/Oscillator Math` to `SSH Backend Job Queue`, `Dark Workbench Style Tokens`, `hz_to_phase`, `rp_math.py`, `OscPanel`, `harmonic_phase_offset_to_preload`?**
  _High betweenness centrality (0.062) - this node is a cross-community bridge._
- **Why does `phase_to_hz()` connect `hz_to_phase` to `Apply/Oscillator Math`, `rp_math.py`, `redpitaya_picosdk_verify.py`, `run_hardware_suite`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `TestWaveformAnalysis` (e.g. with `AnalysisConfig` and `CheckStatus`) actually correct?**
  _`TestWaveformAnalysis` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `AnalysisConfig` (e.g. with `TestCommandBuilder` and `TestDebugBundle`) actually correct?**
  _`AnalysisConfig` has 3 INFERRED edges - model-reasoned connections that need verification._
- **What connects `axi4lite_pulse_regs`, `pulse_gen`, `system_wrapper` to the rest of the system?**
  _126 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `SSH Backend Job Queue` be split into smaller, more focused modules?**
  _Cohesion score 0.07372549019607844 - nodes in this community are weakly interconnected._