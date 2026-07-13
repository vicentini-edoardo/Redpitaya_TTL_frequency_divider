# Graph Report - Redpitaya_TTL_frequency_divider  (2026-07-13)

## Corpus Check
- 16 files · ~21,272 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 298 nodes · 514 edges · 25 communities (13 shown, 12 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 10 edges (avg confidence: 0.78)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `e6f3afd1`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_GUI PulseHarmonic Panels|GUI Pulse/Harmonic Panels]]
- [[_COMMUNITY_PicoSDK Verify CLI|PicoSDK Verify CLI]]
- [[_COMMUNITY_SSH Backend Job Queue|SSH Backend Job Queue]]
- [[_COMMUNITY_Dark Workbench Style Tokens|Dark Workbench Style Tokens]]
- [[_COMMUNITY_redpitaya_picosdk_verify.py|redpitaya_picosdk_verify.py]]
- [[_COMMUNITY_PulsePanel|PulsePanel]]
- [[_COMMUNITY_Edge-Lock Phase Offset Math|Edge-Lock Phase Offset Math]]
- [[_COMMUNITY_ApplyOscillator Math|Apply/Oscillator Math]]
- [[_COMMUNITY_rp_ctl.c Register IO|rp_ctl.c Register IO]]
- [[_COMMUNITY_PulseHarmonic Mode Overview|Pulse/Harmonic Mode Overview]]
- [[_COMMUNITY_FPGA Register Map & Precision|FPGA Register Map & Precision]]
- [[_COMMUNITY_GUI Layout Tests|GUI Layout Tests]]
- [[_COMMUNITY_FPGA RTL Top-Level|FPGA RTL Top-Level]]
- [[_COMMUNITY_Output Mode Buttons|Output Mode Buttons]]
- [[_COMMUNITY_paramiko Dependency Pin|paramiko Dependency Pin]]
- [[_COMMUNITY_AXI4-Lite Pulse Regs RTL|AXI4-Lite Pulse Regs RTL]]
- [[_COMMUNITY_pulse_gen RTL|pulse_gen RTL]]
- [[_COMMUNITY_Connection Strip Component|Connection Strip Component]]
- [[_COMMUNITY_Monitor Tiles Component|Monitor Tiles Component]]
- [[_COMMUNITY_FPGA Register Map (README)|FPGA Register Map (README)]]
- [[_COMMUNITY_Out Of Scope (no backendregisterrp_math.py changes)|Out Of Scope (no backend/register/rp_math.py changes)]]
- [[_COMMUNITY_Harmonic Generator Mode (README)|Harmonic Generator Mode (README)]]
- [[_COMMUNITY_Pulse  Freq-Shift Mode (README)|Pulse / Freq-Shift Mode (README)]]
- [[_COMMUNITY_hz_to_phase|hz_to_phase]]
- [[_COMMUNITY_rp_math.py|rp_math.py]]

## God Nodes (most connected - your core abstractions)
1. `SshBackend` - 38 edges
2. `_NcoPanel` - 33 edges
3. `MainWindow` - 26 edges
4. `PulsePanel` - 16 edges
5. `HarmonicPanel` - 15 edges
6. `Red Pitaya TTL Frequency Generator` - 13 edges
7. `Dark Workbench GUI Redesign` - 12 edges
8. `_mono_font()` - 11 edges
9. `BigDisplay` - 10 edges
10. `main()` - 8 edges

## Surprising Connections (you probably didn't know these)
- `Connection Panel (Host/Port/User/Key, Connect, Upload & Compile, status LED)` --conceptually_related_to--> `SshBackend`  [INFERRED]
  GUI.png → redpitaya_combined_gui_qt.py
- `Controls Panel (Freq shift, Width, Meas. window, Enable Output, Auto-Apply, Apply Now, Soft Reset)` --conceptually_related_to--> `PulsePanel`  [INFERRED]
  GUI.png → redpitaya_combined_gui_qt.py
- `Measurement Stat Tiles (Input Frequency, Pulse Duration, Output Frequency, Duty Cycle)` --conceptually_related_to--> `PulsePanel`  [INFERRED]
  GUI.png → redpitaya_combined_gui_qt.py
- `PySide6-Essentials>=6.6` --references--> `MainWindow`  [INFERRED]
  requirements.txt → redpitaya_combined_gui_qt.py
- `Frequency Shift Status Line (requested/actual/register/resolution readout)` --shares_data_with--> `fmt_signed_freq()`  [INFERRED]
  GUI.png → rp_math.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Connect / Upload & Compile / Disconnected status form the SSH connection workflow** — gui_connection_panel, gui_log_panel, redpitaya_combined_gui_qt_sshbackend [INFERRED 0.80]
- **Freq shift + Width entry, Apply Now/Auto-Apply, and the freq-shift status readout form the pulse-mode parameter apply flow** — gui_controls_panel, gui_freq_shift_status_line, gui_measurement_display_panel [INFERRED 0.80]

## Communities (25 total, 12 thin omitted)

### Community 0 - "GUI Pulse/Harmonic Panels"
Cohesion: 0.33
Nodes (5): Dark Workbench GUI Redesign Implementation Plan, Task 1: Add GUI Structure Smoke Test, Task 2: Redesign Style Tokens And Shared Components, Task 3: Rebuild Workbench Layout, Task 4: Verify And Polish

### Community 2 - "SSH Backend Job Queue"
Cohesion: 0.07
Nodes (9): QObject, _Job, Single persistent paramiko SSH session shared by both panels.      self._mode (", Pulse-mode modulated write (enable=1, harmonic_mode=0 via helper)., Harmonic-mode modulated write (enable=1, harmonic_mode=1 via helper)., Set control register via the pulse helper (keeps harmonic_mode=0)., Set control register via the harmonic helper (keeps harmonic_mode=1)., Write DIO2 48-bit NCO step. phase_step=0 disables. (+1 more)

### Community 3 - "Dark Workbench Style Tokens"
Cohesion: 0.12
Nodes (21): Path, QFont, QFrame, QGridLayout, QGroupBox, QLabel, QWidget, BigDisplay (+13 more)

### Community 4 - "redpitaya_picosdk_verify.py"
Cohesion: 0.29
Nodes (6): Error Handling, Goal, Out Of Scope, Scope, Select Git Update Branch, Testing

### Community 5 - "PulsePanel"
Cohesion: 0.33
Nodes (5): File Structure, Select Git Update Branch Implementation Plan, Self-Review, Task 1: Specify the selectable branch behavior with tests, Task 2: Add the smallest branch-aware updater

### Community 7 - "Edge-Lock Phase Offset Math"
Cohesion: 0.17
Nodes (10): Architecture, Board-side helper (`rp_ctl.c`), Commands, Development Guidance, FPGA register map, GUI (`redpitaya_combined_gui_qt.py`), Math helpers (`rp_math.py`), Project Overview (+2 more)

### Community 8 - "Apply/Oscillator Math"
Cohesion: 0.06
Nodes (9): HarmonicPanel, _mode_btn_style(), _NcoPanel, PulsePanel, Public entry point (e.g. the Ctrl+Return shortcut) → mode-specific write., Pulse / frequency-shift mode (harmonic_mode=0): f_out = f_in + f_shift., Harmonic generator mode (harmonic_mode=1): f_out = N × f_in + f_shift., Style for the three output-mode buttons (OFF / MODULATED / ON). (+1 more)

### Community 10 - "rp_ctl.c Register IO"
Cohesion: 0.45
Nodes (11): off_t, detect_mode(), main(), print_json(), rd32(), rd48(), rd48u(), usage() (+3 more)

### Community 15 - "FPGA RTL Top-Level"
Cohesion: 0.40
Nodes (4): axi4lite_pulse_regs, pulse_gen, system_wrapper, red_pitaya_top

### Community 25 - "FPGA Register Map (README)"
Cohesion: 0.12
Nodes (16): Board-side helper, Connecting, control register bits, FPGA register map, GUI, Hardware assumptions, Installation, License (+8 more)

### Community 26 - "Out Of Scope (no backend/register/rp_math.py changes)"
Cohesion: 0.15
Nodes (12): Approved Direction, Components, Dark Workbench GUI Redesign, Error Handling And State, Functional Scope, Goal, Layout, Out Of Scope (+4 more)

### Community 36 - "hz_to_phase"
Cohesion: 0.09
Nodes (22): Connection Panel (Host/Port/User/Key, Connect, Upload & Compile, status LED), Controls Panel (Freq shift, Width, Meas. window, Enable Output, Auto-Apply, Apply Now, Soft Reset), Frequency Shift Status Line (requested/actual/register/resolution readout), Log Panel (scrolling status/debug output), Measurement Stat Tiles (Input Frequency, Pulse Duration, Output Frequency, Duty Cycle), Red Pitaya TTL Frequency Divider GUI Screenshot (Pulse Tab, Disconnected), duty_to_cycles(), fmt_dur() (+14 more)

### Community 37 - "rp_math.py"
Cohesion: 0.13
Nodes (6): QMainWindow, MainWindow, Switch the poll helper when the user changes tabs., Track the active measurement window (clamped) as the poll period., Switch poll target between pulse and harmonic helper., PySide6-Essentials>=6.6

## Knowledge Gaps
- **61 isolated node(s):** `Task 1: Specify the selectable branch behavior with tests`, `Task 2: Add the smallest branch-aware updater`, `Self-Review`, `Goal`, `Scope` (+56 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **12 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SshBackend` connect `SSH Backend Job Queue` to `Apply/Oscillator Math`, `Dark Workbench Style Tokens`, `hz_to_phase`, `rp_math.py`?**
  _High betweenness centrality (0.143) - this node is a cross-community bridge._
- **Why does `_NcoPanel` connect `Apply/Oscillator Math` to `SSH Backend Job Queue`, `Dark Workbench Style Tokens`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Why does `MainWindow` connect `rp_math.py` to `Apply/Oscillator Math`, `Dark Workbench Style Tokens`?**
  _High betweenness centrality (0.078) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `PulsePanel` (e.g. with `Controls Panel (Freq shift, Width, Meas. window, Enable Output, Auto-Apply, Apply Now, Soft Reset)` and `Measurement Stat Tiles (Input Frequency, Pulse Duration, Output Frequency, Duty Cycle)`) actually correct?**
  _`PulsePanel` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Task 1: Specify the selectable branch behavior with tests`, `Task 2: Add the smallest branch-aware updater`, `Self-Review` to the rest of the system?**
  _77 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `SSH Backend Job Queue` be split into smaller, more focused modules?**
  _Cohesion score 0.07493061979648474 - nodes in this community are weakly interconnected._
- **Should `Dark Workbench Style Tokens` be split into smaller, more focused modules?**
  _Cohesion score 0.11605937921727395 - nodes in this community are weakly interconnected._