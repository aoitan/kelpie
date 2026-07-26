# Task completion
- Confirm work stayed within the active Kelpie phase.
- Ensure the required phase artifact exists and includes completed work, excluded work, next input, and risks/open issues.
- For Python executor/config changes run `python3 -m unittest tests/test_run_issue_workflow_hooks.py`.
- Exercise a representative `scripts/run_issue_workflow.py ... --dry-run` when prompt rendering, phase selection, runner config, hooks, or artifact paths change.
- Check `git diff`/`git status --short`; preserve unrelated user changes.
- Update README/examples when user-facing CLI or config behavior changes.