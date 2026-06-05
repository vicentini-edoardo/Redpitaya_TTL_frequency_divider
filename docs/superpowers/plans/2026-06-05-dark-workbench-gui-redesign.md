# Dark Workbench GUI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the PySide6 GUI visual structure as a dark single-workbench control panel while preserving all existing Red Pitaya control functionality.

**Architecture:** Keep the existing `SshBackend`, `PulsePanel`, `HarmonicPanel`, and `MainWindow` behavior. Change only presentation helpers and Qt layout construction, adding small object names/labels so the redesigned workbench can be smoke-tested without connecting to hardware.

**Tech Stack:** Python 3, PySide6, `unittest`, existing `rp_math.py` helpers.

---

### Task 1: Add GUI Structure Smoke Test

**Files:**
- Create: `tests/test_gui_layout.py`

- [ ] **Step 1: Write the failing test**

Create an offscreen `QApplication`, instantiate `MainWindow`, and assert the redesigned workbench object names and copy exist.

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui_layout -v`

Expected: FAIL because `rpWorkbenchHeader`, `rpReadoutGrid`, and updated title text do not exist yet.

### Task 2: Redesign Style Tokens And Shared Components

**Files:**
- Modify: `redpitaya_combined_gui_qt.py`

- [ ] **Step 1: Update palette and widget style helpers**

Replace the old neon-leaning style values with the restrained dark workbench palette, button states, field states, tabs, log, badges, and monitor tile styling.

- [ ] **Step 2: Update `BigDisplay`**

Remove decorative rule lines, use a cleaner label/value/subtext stack, keep monospace numeric precision, and expose object names for readout smoke tests.

### Task 3: Rebuild Workbench Layout

**Files:**
- Modify: `redpitaya_combined_gui_qt.py`

- [ ] **Step 1: Update `MainWindow._build_ui` and connection header**

Create a horizontal workbench header with title, subtitle, connection controls, status, active mode, update action, and stable object names.

- [ ] **Step 2: Update `_NcoPanel._build_ui`**

Keep the 2x2 readout grid first, then create a compact control area with output mode, parameter grid, detail text, and right action column.

- [ ] **Step 3: Update shared trigger and log sections**

Keep all behavior, but restyle the DIO2 trigger and log rows to match the workbench system.

### Task 4: Verify And Polish

**Files:**
- Modify if needed: `redpitaya_combined_gui_qt.py`
- Test: `tests/test_gui_layout.py`, `tests/test_rp_math.py`

- [ ] **Step 1: Run targeted GUI smoke test**

Run: `QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui_layout -v`

- [ ] **Step 2: Run existing math tests**

Run: `python3 -m unittest discover -s tests`

- [ ] **Step 3: Run syntax check**

Run: `python3 -m py_compile redpitaya_combined_gui_qt.py`

- [ ] **Step 4: Inspect diff**

Run: `git diff -- redpitaya_combined_gui_qt.py tests/test_gui_layout.py`

Confirm the backend protocol, math helpers, and SSH command strings were not changed.
