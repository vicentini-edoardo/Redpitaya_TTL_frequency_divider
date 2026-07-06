# Measurement Window Editable Field Design

## Goal

Replace the fixed "Meas. window" combo box in the Qt GUI with an editable field where the user types a whole-number value in milliseconds.

## Scope

This change is limited to the desktop GUI.

- Replace the existing preset dropdown with a text entry control for integer milliseconds.
- Keep the current backend path that writes the measurement window through the existing board command.
- Preserve the existing polling behavior that follows the active measurement window.

Out of scope:

- FPGA changes
- Board helper changes in `rp_ctl.c`
- Support for fractional milliseconds
- New preset lists or suggestion logic beyond what is needed to keep the UI working

## Recommended Approach

Use a `QLineEdit` with a `QIntValidator`, paired with a small `ms` suffix label.

Why this approach:

- It matches the requested "editable field" behavior better than a spin box.
- It is a small GUI-only change.
- It maps directly to the existing backend command, which already accepts microseconds and clamps to a minimum of `1000`.

## Behavior

- The field displays the current measurement window in whole milliseconds.
- The user may type only whole-number values.
- The value is committed only when the user presses Enter or the field loses focus.
- On commit, the GUI converts milliseconds to microseconds and sends the existing window command.
- Values below `1` are clamped to `1` before sending, matching the FPGA minimum of `1000 us`.
- If the input is empty or otherwise not commit-ready, the field reverts to the last valid value on focus loss.

## UI Details

- Replace the combo box in the "Meas. window" row with:
  - one `QLineEdit`
  - one small `QLabel` showing `ms`
- Keep the existing styling language used for the surrounding controls.
- Keep the existing window-suggestion label, but adapt it to compare the typed millisecond value against the recommended window derived from `suggest_window`.

## Data Flow

1. User types a whole-number millisecond value.
2. Enter or focus loss triggers a commit handler.
3. The handler validates/clamps the integer millisecond value.
4. The GUI converts `ms -> us`.
5. The existing backend `set_window()` path sends the value to the board.
6. Poll timing continues to follow the active window as it does today.
7. Backend status refresh updates the field text if the board reports a different clamped value.

## Error Handling

- Invalid typed values are prevented at the widget level with `QIntValidator`.
- Empty text on commit restores the previous valid value instead of sending anything.
- Backend writes continue to use the existing error-reporting path.

## Testing

- Add a GUI test covering commit-on-Enter behavior and backend window write in microseconds.
- Add a GUI test covering focus-loss commit behavior.
- Add a GUI test covering sub-`1 ms` input clamping to `1 ms`.
- Add a GUI test covering empty-input reversion to the previous valid value.

## Risks

- Focus-loss handling can accidentally trigger duplicate writes if implemented in both a signal and a custom focus hook. Use one commit path.
- The old combo-box synchronization code should be removed or adapted cleanly so the field stays in sync with polled hardware state.
