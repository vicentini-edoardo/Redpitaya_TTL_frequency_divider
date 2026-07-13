# Graph Report - Redpitaya_TTL_frequency_divider  (2026-07-06)

## Corpus Check
- 23 files · ~39,423 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 507 nodes · 1015 edges · 43 communities (25 shown, 18 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 33 edges (avg confidence: 0.64)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `f8fcf85f`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_GUI PulseHarmonic Panels|GUI Pulse/Harmonic Panels]]
- [[_COMMUNITY_PicoSDK Verify CLI|PicoSDK Verify CLI]]
- [[_COMMUNITY_SSH Backend Job Queue|SSH Backend Job Queue]]
- [[_COMMUNITY_Dark Workbench Style Tokens|Dark Workbench Style Tokens]]
- [[_COMMUNITY_Pico4000a Scope Wrapper|Pico4000a Scope Wrapper]]
- [[_COMMUNITY_Workbench Layout Rebuild|Workbench Layout Rebuild]]
- [[_COMMUNITY_Main Window Lifecycle|Main Window Lifecycle]]
- [[_COMMUNITY_Edge-Lock Phase Offset Math|Edge-Lock Phase Offset Math]]
- [[_COMMUNITY_ApplyOscillator Math|Apply/Oscillator Math]]
- [[_COMMUNITY_Osc-Delay NCO Simulation|Osc-Delay NCO Simulation]]
- [[_COMMUNITY_rp_ctl.c Register IO|rp_ctl.c Register IO]]
- [[_COMMUNITY_PulseHarmonic Mode Overview|Pulse/Harmonic Mode Overview]]
- [[_COMMUNITY_Stroboscopic Illumination Sim|Stroboscopic Illumination Sim]]
- [[_COMMUNITY_FPGA Register Map & Precision|FPGA Register Map & Precision]]
- [[_COMMUNITY_GUI Layout Tests|GUI Layout Tests]]
- [[_COMMUNITY_FPGA RTL Top-Level|FPGA RTL Top-Level]]
- [[_COMMUNITY_Frequency-Match Precision Limits|Frequency-Match Precision Limits]]
- [[_COMMUNITY_Output Mode Buttons|Output Mode Buttons]]
- [[_COMMUNITY_Hardware Test Package Init|Hardware Test Package Init]]
- [[_COMMUNITY_paramiko Dependency Pin|paramiko Dependency Pin]]
- [[_COMMUNITY_AXI4-Lite Pulse Regs RTL|AXI4-Lite Pulse Regs RTL]]
- [[_COMMUNITY_pulse_gen RTL|pulse_gen RTL]]
- [[_COMMUNITY_Verify And Polish Task|Verify And Polish Task]]
- [[_COMMUNITY_Connection Strip Component|Connection Strip Component]]
- [[_COMMUNITY_Monitor Tiles Component|Monitor Tiles Component]]
- [[_COMMUNITY_FPGA Register Map (README)|FPGA Register Map (README)]]
- [[_COMMUNITY_Out Of Scope (no backendregisterrp_math.py changes)|Out Of Scope (no backend/register/rp_math.py changes)]]
- [[_COMMUNITY_Board-side Helper Binary (README)|Board-side Helper Binary (README)]]
- [[_COMMUNITY_Harmonic Generator Mode (README)|Harmonic Generator Mode (README)]]
- [[_COMMUNITY_Pulse  Freq-Shift Mode (README)|Pulse / Freq-Shift Mode (README)]]
- [[_COMMUNITY_Measurement Window Editable Field Design|Measurement Window Editable Field Design]]
- [[_COMMUNITY_test_rp_math.py|test_rp_math.py]]
- [[_COMMUNITY_HarmonicPanel|HarmonicPanel]]
- [[_COMMUNITY_harmonic_phase_offset_to_preload|harmonic_phase_offset_to_preload]]
- [[_COMMUNITY_phase_offset_to_preload|phase_offset_to_preload]]
- [[_COMMUNITY_._set_output_mode|._set_output_mode]]
- [[_COMMUNITY_hz_to_phase|hz_to_phase]]
- [[_COMMUNITY_rp_math.py|rp_math.py]]
- [[_COMMUNITY_PulsePanel|PulsePanel]]
- [[_COMMUNITY_measured_edges_to_phase_step|measured_edges_to_phase_step]]
- [[_COMMUNITY_Measurement Window Editable Field Implementation Plan|Measurement Window Editable Field Implementation Plan]]
- [[_COMMUNITY_duty_to_cycles|duty_to_cycles]]
- [[_COMMUNITY_suggest_window|suggest_window]]

## God Nodes (most connected - your core abstractions)
1. `SshBackend` - 45 edges
2. `_NcoPanel` - 39 edges
3. `MainWindow` - 28 edges
4. `TestWaveformAnalysis` - 23 edges
5. `AnalysisConfig` - 21 edges
6. `analyze_capture()` - 21 edges
7. `OscPanel` - 20 edges
8. `run_hardware_suite()` - 20 edges
9. `RedPitayaCommandBuilder` - 19 edges
10. `HarmonicPanel` - 17 edges

## Surprising Connections (you probably didn't know these)
- `Connection Panel (Host/Port/User/Key, Connect, Upload & Compile, status LED)` --conceptually_related_to--> `SshBackend`  [INFERRED]
  GUI.png → redpitaya_combined_gui_qt.py
- `Controls Panel (Freq shift, Width, Meas. window, Enable Output, Auto-Apply, Apply Now, Soft Reset)` --conceptually_related_to--> `PulsePanel`  [INFERRED]
  GUI.png → redpitaya_combined_gui_qt.py
- `Measurement Stat Tiles (Input Frequency, Pulse Duration, Output Frequency, Duty Cycle)` --conceptually_related_to--> `PulsePanel`  [INFERRED]
  GUI.png → redpitaya_combined_gui_qt.py
- `PySide6-Essentials>=6.6` --references--> `MainWindow`  [INFERRED]
  requirements.txt → redpitaya_combined_gui_qt.py
- `Controls Panel (Freq shift, Width, Meas. window, Enable Output, Auto-Apply, Apply Now, Soft Reset)` --shares_data_with--> `duty_to_cycles()`  [INFERRED]
  GUI.png → rp_math.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Dark Workbench GUI Redesign: plan tasks implementing the design spec** — docs_superpowers_plans_2026_06_05_dark_workbench_gui_redesign_task1_smoke_test, docs_superpowers_plans_2026_06_05_dark_workbench_gui_redesign_task2_style_tokens, docs_superpowers_plans_2026_06_05_dark_workbench_gui_redesign_task3_rebuild_layout, docs_superpowers_plans_2026_06_05_dark_workbench_gui_redesign_task4_verify_polish, docs_superpowers_specs_2026_06_05_dark_workbench_gui_redesign_design_goal [INFERRED 0.85]
- **Frequency-match precision hard limits and how DIO2 ratio escapes them** — docs_redpitaya_picosdk_hardware_tests_frequency_match_precision, docs_redpitaya_picosdk_hardware_tests_output_frequency_quantization, docs_redpitaya_picosdk_hardware_tests_clock_mismatch, docs_redpitaya_picosdk_hardware_tests_dio2_ratio_check [EXTRACTED 1.00]
- **Connect / Upload & Compile / Disconnected status form the SSH connection workflow** — gui_connection_panel, gui_log_panel, redpitaya_combined_gui_qt_sshbackend [INFERRED 0.80]
- **Freq shift + Width entry, Apply Now/Auto-Apply, and the freq-shift status readout form the pulse-mode parameter apply flow** — gui_controls_panel, gui_freq_shift_status_line, gui_measurement_display_panel [INFERRED 0.80]

## Communities (43 total, 18 thin omitted)

### Community 0 - "GUI Pulse/Harmonic Panels"
Cohesion: 0.17
Nodes (9): f_osc_from_params(), f_shift_from_f_osc(), osc_half_period_cycles(), osc_phase_preload(), NCO frequency shift derived from oscillation rate and phase amplitude., Oscillation frequency from NCO f_shift and phase amplitude., Clock ticks per half-oscillation (sweeps 2·P of phase at f_shift rate)., 48-bit accumulator preload so the first output pulse has delay = P0 − P. (+1 more)

### Community 1 - "PicoSDK Verify CLI"
Cohesion: 0.09
Nodes (40): ArgumentParser, Enum, Expectation, AnalysisConfig, analyze_capture(), _analyze_constant(), analyze_osc_delay(), build_arg_parser() (+32 more)

### Community 2 - "SSH Backend Job Queue"
Cohesion: 0.05
Nodes (13): Task 1: Add GUI Structure Smoke Test, Dark Workbench GUI Redesign Goal (same backend, new visuals), QObject, _Job, Single persistent paramiko SSH session shared by both panels.      self._mode (", Pulse-mode modulated write (enable=1, harmonic_mode=0 via helper).          When, Harmonic-mode modulated write (enable=1, harmonic_mode=1 via helper).          W, Set control register via the pulse helper (keeps harmonic_mode=0). (+5 more)

### Community 3 - "Dark Workbench Style Tokens"
Cohesion: 0.06
Nodes (40): Task 2: Redesign Style Tokens And Shared Components, Restrained Dark Product Palette (visual system), Path, QFont, QFrame, QGridLayout, QGroupBox, QIcon (+32 more)

### Community 4 - "Pico4000a Scope Wrapper"
Cohesion: 0.12
Nodes (11): Any, configure_test(), estimate_input_hz(), _nearest_range_key(), Pico4000aScope, Small block-capture wrapper around the PicoSDK ps4000a Python module., RedPitayaCommandBuilder, RedPitayaSSH (+3 more)

### Community 5 - "Workbench Layout Rebuild"
Cohesion: 0.50
Nodes (3): Task 1: Add regression coverage for the update helper, Task 2: Wire the helper into the Update button, Update Branch Selection Implementation Plan

### Community 7 - "Edge-Lock Phase Offset Math"
Cohesion: 0.17
Nodes (10): Architecture, Board-side helper (`rp_ctl.c`), Commands, Development Guidance, FPGA register map, GUI (`redpitaya_combined_gui_qt.py`), Math helpers (`rp_math.py`), Project Overview (+2 more)

### Community 8 - "Apply/Oscillator Math"
Cohesion: 0.11
Nodes (4): Task 3: Rebuild Workbench Layout, Option A Workbench Layout (header strip, mode tabs, shared tools), _NcoPanel, Shared UI and logic for the two NCO control tabs.      Both tabs poll the same s

### Community 9 - "Osc-Delay NCO Simulation"
Cohesion: 0.17
Nodes (16): check_edge_lock_shift(), check_limits(), fraction_to_acc(), hz_to_phase_step(), measured_step_base(), osc_half_period_cycles(), plot_results(), Oscillating Delay Mode — NCO simulation & verification.  Tick-accurate model of (+8 more)

### Community 10 - "rp_ctl.c Register IO"
Cohesion: 0.45
Nodes (11): off_t, detect_mode(), main(), print_json(), rd32(), rd48(), rd48u(), usage() (+3 more)

### Community 11 - "Pulse/Harmonic Mode Overview"
Cohesion: 0.50
Nodes (4): Generated Debug Bundle (summary.json + captures/*.csv), PicoSDK Hardware Verification Harness, hardware_tests/redpitaya_picosdk_verify.py (verification entry point), picosdk>=1.1 (PicoSDK Python wrapper)

### Community 12 - "Stroboscopic Illumination Sim"
Cohesion: 0.29
Nodes (6): illum_phase(), physical_signal(), Stroboscopic illumination — oscillating delay mode visualization.  Physical setu, phi_frac: fractional phase [0, 1) within one T_in period.     Resembles a nonlin, Fractional phase [P0-P, P0+P] of illumination at time t.     Triangle wave: P0-P, sampled_intensity()

### Community 13 - "FPGA Register Map & Precision"
Cohesion: 0.67
Nodes (3): Oscillating-Delay Measurement Analysis (per-edge phase + sinusoidal fit), Triangle-Wave vs Sinusoidal Fit Bias (8/pi^2 factor), scipy>=1.10

### Community 14 - "GUI Layout Tests"
Cohesion: 0.10
Nodes (4): _FakeBackend, TestDarkWorkbenchLayout, TestGitUpdateHelpers, TestMeasurementWindowField

### Community 15 - "FPGA RTL Top-Level"
Cohesion: 0.40
Nodes (4): axi4lite_pulse_regs, pulse_gen, system_wrapper, red_pitaya_top

### Community 16 - "Frequency-Match Precision Limits"
Cohesion: 0.50
Nodes (4): Scope vs Red Pitaya Clock Mismatch (tens of ppm), DIO2 Ratio Check (clock-independent f_out/f_DIO2 verification), Frequency-Match Precision and Its Hard Limits, Output Frequency Quantization Wall (~5 Hz at default window)

### Community 30 - "Measurement Window Editable Field Design"
Cohesion: 0.18
Nodes (10): Behavior, Data Flow, Error Handling, Goal, Measurement Window Editable Field Design, Recommended Approach, Risks, Scope (+2 more)

### Community 31 - "test_rp_math.py"
Cohesion: 0.19
Nodes (10): Connection Panel (Host/Port/User/Key, Connect, Upload & Compile, status LED), Controls Panel (Freq shift, Width, Meas. window, Enable Output, Auto-Apply, Apply Now, Soft Reset), Frequency Shift Status Line (requested/actual/register/resolution readout), Log Panel (scrolling status/debug output), Measurement Stat Tiles (Input Frequency, Pulse Duration, Output Frequency, Duty Cycle), Red Pitaya TTL Frequency Divider GUI Screenshot (Pulse Tab, Disconnected), fmt_dur(), fmt_freq() (+2 more)

### Community 33 - "harmonic_phase_offset_to_preload"
Cohesion: 0.27
Nodes (5): harmonic_phase_offset_to_preload(), harmonic_preload_to_phase_offset(), 48-bit accumulator preload for a constant edge-lock phase offset in     *harmoni, Inverse of :func:`harmonic_phase_offset_to_preload` → offset turns [0, 1)., TestHarmonicPhaseOffsetPreload

### Community 34 - "phase_offset_to_preload"
Cohesion: 0.29
Nodes (5): phase_offset_to_preload(), preload_to_phase_offset(), 48-bit accumulator preload for a constant edge-lock phase offset.      In edge-l, Inverse of :func:`phase_offset_to_preload`: preload word → offset turns.      Re, TestPhaseOffsetPreload

### Community 35 - "._set_output_mode"
Cohesion: 0.22
Nodes (3): _mode_btn_style(), Public entry point (e.g. the Ctrl+Return shortcut) → mode-specific write., Style for the three output-mode buttons (OFF / MODULATED / ON).

### Community 36 - "hz_to_phase"
Cohesion: 0.36
Nodes (3): hz_to_phase(), phase_to_hz(), TestPhaseConversion

### Community 37 - "rp_math.py"
Cohesion: 0.36
Nodes (3): trig_hz_to_phase_step(), trig_phase_step_to_hz(), TestTrigPhaseStep

### Community 39 - "measured_edges_to_phase_step"
Cohesion: 0.47
Nodes (3): measured_edges_to_phase_step(), phase_step_base from a true reciprocal measurement: edge_count rising     edges, TestInputMeasurementMath

### Community 40 - "Measurement Window Editable Field Implementation Plan"
Cohesion: 0.40
Nodes (4): Measurement Window Editable Field Implementation Plan, Task 1: Add failing GUI tests for the editable window field, Task 2: Implement the editable millisecond field in the shared NCO panel, Task 3: Run regression verification for the touched GUI surface

## Knowledge Gaps
- **48 isolated node(s):** `Task 1: Add regression coverage for the update helper`, `Task 2: Wire the helper into the Update button`, `Task 1: Add failing GUI tests for the editable window field`, `Task 2: Implement the editable millisecond field in the shared NCO panel`, `Task 3: Run regression verification for the touched GUI surface` (+43 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **18 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SshBackend` connect `SSH Backend Job Queue` to `Apply/Oscillator Math`, `Dark Workbench Style Tokens`, `test_rp_math.py`?**
  _High betweenness centrality (0.127) - this node is a cross-community bridge._
- **Why does `_NcoPanel` connect `Apply/Oscillator Math` to `HarmonicPanel`, `SSH Backend Job Queue`, `._set_output_mode`, `Dark Workbench Style Tokens`, `Main Window Lifecycle`, `PulsePanel`?**
  _High betweenness centrality (0.091) - this node is a cross-community bridge._
- **Why does `MainWindow` connect `Dark Workbench Style Tokens` to `Apply/Oscillator Math`, `SSH Backend Job Queue`, `Main Window Lifecycle`?**
  _High betweenness centrality (0.059) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `TestWaveformAnalysis` (e.g. with `AnalysisConfig` and `CheckStatus`) actually correct?**
  _`TestWaveformAnalysis` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `AnalysisConfig` (e.g. with `TestCommandBuilder` and `TestDebugBundle`) actually correct?**
  _`AnalysisConfig` has 3 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Single persistent paramiko SSH session shared by both panels.      self._mode ("`, `Pulse-mode modulated write (enable=1, harmonic_mode=0 via helper).          When`, `Harmonic-mode modulated write (enable=1, harmonic_mode=1 via helper).          W` to the rest of the system?**
  _97 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `PicoSDK Verify CLI` be split into smaller, more focused modules?**
  _Cohesion score 0.09180327868852459 - nodes in this community are weakly interconnected._