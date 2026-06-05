# Dark Workbench GUI Redesign

## Goal

Redesign the PySide6 Red Pitaya control GUI from zero visually while keeping the same backend behavior and user-facing functionality.

## Approved Direction

Use the Option A workbench structure with a restrained dark theme. The interface remains a single-window desktop control panel with a horizontal connection strip, two mode tabs, live readouts first, editable controls second, shared DIO2 trigger controls, and a compact log.

## Users And Context

Small lab groups use the application during bench work to control a Red Pitaya TTL frequency generator over SSH. They need fast confidence in connection state, active mode, measured input, computed output, and output override state. The GUI should feel like shared lab software rather than a terminal or marketing dashboard.

## Functional Scope

Preserve these existing functions:

- SSH connect and disconnect with host, port, username, optional key, and default base address.
- One persistent `SshBackend` session with priority queue behavior unchanged.
- Pulse / Freq-Shift mode with frequency shift, pulse width, measurement window, auto-apply, apply now, soft reset, upload and compile.
- Harmonic Generator mode with frequency shift, harmonic N, measurement window, auto-apply, apply now, soft reset, upload and compile.
- Live FPGA polling and status sync for input frequency, output frequency, period stability, pulse duration, duty cycle, harmonic N, NCO status, output override mode, and DIO2 trigger status.
- Output mode choices: Laser off, Modulated, Laser on.
- Independent DIO2 trigger frequency setting.
- Shared log with timestamped messages.
- Git pull update button and status text.
- Ctrl+Return applies the active tab.

No backend protocol, register math, SSH command, polling cadence, or board helper behavior should change.

## Layout

The window uses a vertical workbench layout:

1. Header strip:
   - Product title and short hardware subtitle.
   - Connection fields and key picker.
   - Connect/Disconnect button.
   - Connection status and active mode badge.
   - Update button and short update status.

2. Mode workspace:
   - Existing tabs remain the primary mode switch.
   - Each tab starts with a 2x2 monitor grid.
   - Controls sit below the monitor grid in one grouped work area.
   - Primary actions sit in a compact right-side action column within the controls area.

3. Shared tools:
   - DIO2 trigger output row.
   - Compact log row.

## Visual System

Use a restrained dark product palette:

- Background: near-black blue neutral.
- Panels: slightly lighter layered dark neutrals.
- Borders: visible but quiet cool gray.
- Text: high-contrast off-white for values, muted blue-gray for labels.
- Info/selection: blue.
- Success/stable/apply: green.
- Warning/acquiring/reset/suggestion: amber.
- Error/disconnected/off: red.

Use color only for status, current selection, and actions. Avoid decorative gradients, glass effects, oversized cards, and neon styling.

## Components

Monitor tiles:

- Clean rectangular readout tiles with a label, large value, and compact subtext.
- Values use a mono font for numeric precision.
- Tiles do not use decorative rule lines.
- Stable or active readings can take semantic color.

Connection strip:

- Uses smaller fields with clear labels.
- Status is both text and color.
- Active mode appears as a badge.

Output mode:

- Keep three distinct buttons.
- Active mode has filled/tinted state.
- Disabled controls remain readable when Laser off or Laser on disables modulation settings.

Controls:

- Keep standard Qt spin boxes, combo boxes, checkboxes, and buttons.
- Align labels and fields in a compact grid.
- Keep the detailed shift/register text, but visually subordinate it to main controls.

Actions:

- Apply Now is the primary action.
- Soft Reset is warning-coded.
- Upload & Compile is info-coded.

Log:

- Keep monospace, compact, scrollable.
- Log should not dominate the window.

## Responsive And Window Behavior

The current desktop target remains the priority. The redesigned window should be usable at the existing minimum size or slightly smaller if practical. Text must not be clipped in buttons, badges, field labels, tabs, or readout tiles.

## Error Handling And State

Preserve existing signal handling. Disconnected, connecting, connected, error, acquiring, no-input, and stable states should remain explicit through text plus semantic color. Poll failures continue to log without tearing down the session.

## Testing

Run the existing math tests to confirm behavior remains untouched:

```bash
python3 -m unittest discover -s tests
```

Perform a syntax/import check for the GUI module:

```bash
python3 -m py_compile redpitaya_combined_gui_qt.py
```

If PySide6 is available, instantiate the window offscreen or run a short smoke check that creates `QApplication` and `MainWindow` without showing a blocking UI.

## Out Of Scope

- New hardware controls.
- Backend protocol changes.
- Register map changes.
- Rewriting `rp_math.py`.
- Replacing PySide6.
- Adding web UI, icons, or external design dependencies.
