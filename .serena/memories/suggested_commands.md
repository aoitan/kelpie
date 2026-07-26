# Suggested commands
- Run tests: `python3 -m unittest tests/test_run_issue_workflow_hooks.py`
- Dry-run manual task: `python3 scripts/run_issue_workflow.py --repo-root . --workdir /path/to/target --issue-source none --task-label <label> --runner <runner> --dry-run`
- Dry-run GitHub Issue: add `--issue <n> --issue-source github --github-repo owner/repo`.
- Container build: `docker compose -f compose.llm.yaml -f compose.local.yaml build llm`
- Wrapper execution: `kelpie --target-workdir /path/to/target -- <workflow args>`
- Inspect working changes: `git status --short`; prefer `rg`/`rg --files` for repository search.