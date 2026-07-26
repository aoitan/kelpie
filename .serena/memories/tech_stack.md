# Tech stack
- Python 3.12, standard library CLI (`argparse`, JSON, subprocess/path handling); no project package manifest observed.
- Tests use `unittest` in `tests/test_run_issue_workflow_hooks.py`.
- Shell wrappers support macOS/Linux and Windows install entrypoints.
- Runtime packaging uses Docker Compose; image includes Python 3.12, Node.js 22, `uv`, `gh`, Gemini CLI, Codex CLI, and Copilot CLI.
- Config formats: JSON for runners/instruction staging; YAML for hooks.