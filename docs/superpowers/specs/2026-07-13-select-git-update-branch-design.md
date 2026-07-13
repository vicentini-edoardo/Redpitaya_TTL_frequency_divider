# Select Git Update Branch

## Goal

Allow the desktop GUI user to choose a remote Git branch, switch the local checkout to it, and pull its latest changes.

## Scope

The existing Update control gains a branch selector beside it. The selector lists remote branches discovered with Git. Clicking Update runs the Git work in the existing background thread:

1. Fetch origin.
2. Switch to the selected remote branch, creating or updating its local tracking branch as needed.
3. Fast-forward pull that branch.

The UI disables the selector and Update button while this work runs. It reuses the current status label and log for success and error messages.

## Error Handling

The update must not force a checkout, discard local work, or create a merge commit. A fetch, checkout, or fast-forward failure is reported unchanged through the current error path, leaving the controls usable again.

## Testing

Add one focused GUI test that verifies the branch selector is present and that the update command builder selects the requested branch. Run the GUI layout tests and a Python syntax check.

## Out Of Scope

- Editing, creating, deleting, or pushing branches.
- Manual branch-name entry.
- Auto-updating on launch.
