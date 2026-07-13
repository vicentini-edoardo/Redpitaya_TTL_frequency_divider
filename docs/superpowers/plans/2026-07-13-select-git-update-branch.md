# Select Git Update Branch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a GUI user choose an origin branch, switch the local checkout to it, and fast-forward pull it.

**Architecture:** Keep Git work in the existing `MainWindow` background thread. Add two small module helpers: one lists known `origin/*` branches for the selector and one produces the three safe argument-list commands used by the worker. The existing update status signal remains the sole UI-thread completion path.

**Tech Stack:** Python 3, PySide6, `subprocess`, `unittest`.

---

## File Structure

- Modify: `redpitaya_combined_gui_qt.py` — branch discovery/command helpers, selector, and sequential update worker.
- Modify: `tests/test_gui_layout.py` — focused selector and command tests.

### Task 1: Specify the selectable branch behavior with tests

**Files:**
- Modify: `tests/test_gui_layout.py:4-38`

- [x] **Step 1: Write the failing tests**

Add `QComboBox` to the existing widget import. In `TestDarkWorkbenchLayout`, add:

```python
    def test_update_branch_selector_lists_remote_branches(self):
        with patch.object(gui, "_git_remote_branches", return_value=["main", "feature"]):
            win = gui.MainWindow()
        self.addCleanup(win.close)

        selector = win.findChild(QComboBox, "rpUpdateBranch")
        self.assertIsNotNone(selector)
        self.assertEqual([selector.itemText(i) for i in range(selector.count())], ["main", "feature"])

    def test_git_update_commands_switch_and_fast_forward_selected_branch(self):
        self.assertEqual(
            gui._git_update_commands("feature"),
            [
                ["git", "fetch", "origin", "--prune"],
                ["git", "checkout", "feature"],
                ["git", "pull", "--ff-only"],
            ],
        )
```

Also add `from unittest.mock import patch` and import `QComboBox` beside the existing Qt widgets.

- [x] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_gui_layout.TestDarkWorkbenchLayout.test_update_branch_selector_lists_remote_branches tests.test_gui_layout.TestDarkWorkbenchLayout.test_git_update_commands_switch_and_fast_forward_selected_branch
```

Expected: FAIL because `_git_remote_branches`, `_git_update_commands`, and `rpUpdateBranch` do not yet exist.

- [x] **Step 3: Commit the red test**

```bash
git add tests/test_gui_layout.py
git commit -m "test: define selectable update branch behavior"
```

### Task 2: Add the smallest branch-aware updater

**Files:**
- Modify: `redpitaya_combined_gui_qt.py:53-70` — add Git helpers after the `rp_math` import.
- Modify: `redpitaya_combined_gui_qt.py:1349-1377` — use the selector and run fetch, checkout, then fast-forward pull.
- Modify: `redpitaya_combined_gui_qt.py:1432-1441` — add the selector beside Update.

- [x] **Step 1: Add the helpers**

Add these module-level functions after the `rp_math` import:

```python
def _git_remote_branches(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "branch", "--remotes", "--format=%(refname:short)"],
        capture_output=True, text=True, cwd=repo,
    )
    if result.returncode:
        return []
    return [ref.removeprefix("origin/") for ref in result.stdout.splitlines()
            if ref.startswith("origin/") and ref != "origin/HEAD"]


def _git_update_commands(branch: str) -> list[list[str]]:
    return [
        ["git", "fetch", "origin", "--prune"],
        ["git", "checkout", branch],
        ["git", "pull", "--ff-only"],
    ]
```

- [x] **Step 2: Add the selector to the existing header row**

Immediately before `self._btn_update = QPushButton("Update")` in `_build_connection`, add:

```python
        self._cb_update_branch = QComboBox()
        self._cb_update_branch.setObjectName("rpUpdateBranch")
        self._cb_update_branch.setFixedHeight(30)
        self._cb_update_branch.setStyleSheet(_le_style())
        self._cb_update_branch.addItems(_git_remote_branches(Path(__file__).resolve().parent))
        top_row.addWidget(self._cb_update_branch)
```

Leave the existing Update button and status label in their current positions after the selector.

- [x] **Step 3: Replace the pull-only worker with the selected-branch workflow**

At the top of `_do_git_update`, capture the selection and disable both controls:

```python
        branch = self._cb_update_branch.currentText()
        self._btn_update.setEnabled(False)
        self._cb_update_branch.setEnabled(False)
```

Replace the single `subprocess.run(["git", "pull"], ...)` call in `_run` with:

```python
                outputs = []
                for command in _git_update_commands(branch):
                    result = subprocess.run(
                        command, capture_output=True, text=True, cwd=here,
                        timeout=30, check=True,
                    )
                    outputs.append((result.stdout + result.stderr).strip())
                self.sig_update_done.emit("\n".join(filter(None, outputs)) or "Done (no output)")
```

Remove the old `out` assignment and its success emit. Keep the broad exception handler so failed Git commands continue to display through the existing status label and log.

At the start of `_on_update_done`, re-enable the selector with the existing button:

```python
        self._btn_update.setEnabled(True)
        self._cb_update_branch.setEnabled(True)
```

- [x] **Step 4: Run the focused tests and verify they pass**

Run:

```bash
python3 -m unittest tests.test_gui_layout.TestDarkWorkbenchLayout.test_update_branch_selector_lists_remote_branches tests.test_gui_layout.TestDarkWorkbenchLayout.test_git_update_commands_switch_and_fast_forward_selected_branch
```

Expected: PASS.

- [x] **Step 5: Run regression checks**

Run:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile redpitaya_combined_gui_qt.py
```

Expected: all tests pass and `py_compile` produces no output.

- [x] **Step 6: Commit the implementation**

```bash
git add redpitaya_combined_gui_qt.py tests/test_gui_layout.py
git commit -m "feat: select branch before updating"
```

## Self-Review

- Spec coverage: Task 2 populates an origin-branch selector, disables it with Update, fetches, checks out the selection, and runs fast-forward-only pull. Its existing completion handler reports and logs both success and failure.
- Placeholder scan: no placeholders or deferred implementation steps remain.
- Type consistency: both helpers accept `Path`/`str` values provided by `MainWindow`; commands remain argument lists passed directly to `subprocess.run` without a shell.
