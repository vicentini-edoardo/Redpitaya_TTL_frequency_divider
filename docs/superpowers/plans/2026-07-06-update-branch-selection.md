# Update Branch Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Update action show remote branches, switch to the selected branch, pull updates, and restart the app only when the checked-out commit changes.

**Architecture:** Keep the existing `MainWindow` update button and background-thread model. Add one small pure-ish git helper for branch listing and update execution so the UI stays thin and the behavior is testable without a live repo.

**Tech Stack:** Python 3, PySide6, `subprocess`, `unittest`

---

### Task 1: Add regression coverage for the update helper

**Files:**
- Modify: `tests/test_gui_layout.py`
- Test: `tests/test_gui_layout.py`

- [ ] **Step 1: Write the failing test**

```python
class TestGitUpdateHelpers(unittest.TestCase):
    def test_list_remote_branches_filters_head_pointer(self):
        output = "origin/HEAD -> origin/main\norigin/main\norigin/feature\n"
        self.assertEqual(
            gui._parse_remote_branches(output),
            ["origin/feature", "origin/main"],
        )

    def test_run_git_update_switches_branch_and_reports_restart_needed(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            ...

        msg, restart_needed = gui._run_git_update(gui._APP_DIR, "origin/main", run=fake_run)
        self.assertTrue(restart_needed)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_gui_layout.TestGitUpdateHelpers -v`
Expected: FAIL with missing helper attributes such as `_parse_remote_branches`

- [ ] **Step 3: Write minimal implementation**

```python
def _parse_remote_branches(output: str) -> list[str]:
    ...

def _run_git_update(repo_dir: Path, remote_ref: str, run=subprocess.run) -> tuple[str, bool]:
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_gui_layout.TestGitUpdateHelpers -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_gui_layout.py redpitaya_combined_gui_qt.py
git commit -m "feat: add branch-aware updater"
```

### Task 2: Wire the helper into the Update button

**Files:**
- Modify: `redpitaya_combined_gui_qt.py`
- Test: `tests/test_gui_layout.py`

- [ ] **Step 1: Add branch selection to the existing Update action**

```python
branches = _remote_branches_for_dialog(here)
choice, ok = QInputDialog.getItem(self, "Select branch", "Remote branch:", branches, 0, False)
```

- [ ] **Step 2: Reuse the background thread for branch switch and pull**

```python
msg, restart_needed = _run_git_update(here, choice)
self.sig_update_done.emit(msg, restart_needed)
```

- [ ] **Step 3: Restart only when the checked-out commit changed**

```python
if restart_needed:
    self._restart_app()
```

- [ ] **Step 4: Run targeted GUI tests**

Run: `python3 -m unittest tests.test_gui_layout -v`
Expected: PASS

- [ ] **Step 5: Run repo sanity check**

Run: `python3 -m unittest discover -s tests`
Expected: PASS
