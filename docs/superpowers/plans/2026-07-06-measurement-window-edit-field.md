# Measurement Window Editable Field Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the preset measurement-window dropdown with an integer millisecond text field that commits on Enter or focus loss.

**Architecture:** Keep the existing backend `set_window(window_us)` path and adaptive polling logic. Limit the UI change to `_NcoPanel` by swapping the combo box for a validated line edit, then sync that field from status updates and cover the behavior with targeted GUI tests.

**Tech Stack:** Python 3, PySide6, `unittest`

---

### Task 1: Add failing GUI tests for the editable window field

**Files:**
- Modify: `tests/test_gui_layout.py`
- Test: `tests/test_gui_layout.py`

- [ ] **Step 1: Write the failing tests**

```python
from PySide6.QtCore import QObject, Signal


class _FakeBackend(QObject):
    sig_connected = Signal()
    sig_disconnected = Signal(str)
    sig_status = Signal(dict)
    sig_mode_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.mode = "pulse"
        self.window_calls = []

    def set_window(self, window_us: int):
        self.window_calls.append(window_us)


class TestMeasurementWindowField(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.backend = _FakeBackend()
        self.panel = gui.PulsePanel(self.backend, lambda _msg: None)
        self.panel._live = True
        self.panel._output_mode = "modulated"
        self.panel._update_mode_controls()
        self.addCleanup(self.panel.close)

    def test_enter_commits_integer_milliseconds_as_microseconds(self):
        self.panel._window_field.setText("250")
        self.panel._window_field.returnPressed.emit()
        self.assertEqual(self.backend.window_calls[-1], 250_000)

    def test_focus_loss_commits_integer_milliseconds_as_microseconds(self):
        self.panel._window_field.setFocus()
        self.panel._window_field.setText("25")
        self.panel.setFocus()
        self.app.processEvents()
        self.assertEqual(self.backend.window_calls[-1], 25_000)

    def test_sub_one_millisecond_input_clamps_to_one_millisecond(self):
        self.panel._window_field.setText("0")
        self.panel._window_field.returnPressed.emit()
        self.assertEqual(self.backend.window_calls[-1], 1_000)
        self.assertEqual(self.panel._window_field.text(), "1")

    def test_empty_input_reverts_to_previous_valid_value(self):
        self.panel._set_window_field_ms(100)
        self.panel._window_field.setText("")
        self.panel.setFocus()
        self.app.processEvents()
        self.assertEqual(self.panel._window_field.text(), "100")
        self.assertEqual(self.backend.window_calls, [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_gui_layout.TestMeasurementWindowField -v`

Expected: `ERROR` or `FAIL` because `PulsePanel` does not expose `_window_field` or `_set_window_field_ms` yet.

### Task 2: Implement the editable millisecond field in the shared NCO panel

**Files:**
- Modify: `redpitaya_combined_gui_qt.py`
- Test: `tests/test_gui_layout.py`

- [ ] **Step 1: Replace the combo box with a validated line edit**

```python
self._window_ms = 100
self._window_field = QLineEdit(str(self._window_ms))
self._window_field.setValidator(QIntValidator(0, 1_000_000, self))
self._window_field.setFixedHeight(32)
self._window_field.setFixedWidth(86)
self._window_field.setAlignment(Qt.AlignRight)
self._window_field.setFont(_mono_font(12, bold=True))
self._window_field.setStyleSheet(_line_edit_style())
self._window_field.returnPressed.connect(self._commit_window_field)
fields.addWidget(self._window_field, 1, 1)
fields.addWidget(_dim_label("ms"), 1, 2)
```

- [ ] **Step 2: Add one commit path used by Enter and focus loss**

```python
def _commit_window_field(self):
    text = self._window_field.text().strip()
    if not text:
        self._set_window_field_ms(self._window_ms)
        return

    window_ms = max(1, int(text))
    if window_ms != self._window_ms:
        self._window_ms = window_ms
        if self._live and self._be.mode == self.MODE:
            self._be.set_window(window_ms * 1000)
    self._set_window_field_ms(window_ms)
    self._update_window_suggestion()


def _set_window_field_ms(self, window_ms: int):
    self._window_ms = max(1, int(window_ms))
    self._window_field.setText(str(self._window_ms))
```

- [ ] **Step 3: Trigger commit on focus loss without duplicating logic**

```python
self._window_field.editingFinished.connect(self._commit_window_field)
```

- [ ] **Step 4: Update live sync points that still use preset indexes**

```python
if self._be.mode == self.MODE:
    self._be.set_window(self._window_ms * 1000)
```

```python
raw_us = d.get("meas_time_us")
if raw_us is not None:
    self._set_window_field_ms(max(1, int(raw_us) // 1000))
```

```python
current_us = self._window_ms * 1000
if current_us == WINDOW_OPTIONS_US[sug]:
    ...
else:
    self._lbl_win_suggest.setText(f"suggested: {WINDOW_NAMES[sug]} for {freq_str}")
```

- [ ] **Step 5: Run the targeted tests to verify they pass**

Run: `python3 -m unittest tests.test_gui_layout.TestMeasurementWindowField -v`

Expected: `OK`

### Task 3: Run regression verification for the touched GUI surface

**Files:**
- Test: `tests/test_gui_layout.py`

- [ ] **Step 1: Run the full GUI smoke test file**

Run: `python3 -m unittest tests.test_gui_layout -v`

Expected: `OK`

- [ ] **Step 2: Sanity-check the spec requirements against the final diff**

Checklist:
- Editable whole-number millisecond field exists.
- Commit happens on Enter.
- Commit happens on focus loss.
- Values below `1 ms` clamp to `1 ms`.
- Empty input restores the previous valid value.
- Existing backend `set_window()` path is unchanged.
