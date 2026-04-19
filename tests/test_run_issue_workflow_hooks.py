from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts.run_issue_workflow import (
    HookConfig,
    InstructionStagingConfig,
    RunnerConfig,
    WorkflowRunner,
    parse_yaml_like_file,
)


class HookConfigTests(unittest.TestCase):
    def test_parse_yaml_like_hook_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "hooks.yaml"
            path.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "defaults:",
                        "  on_error: stop",
                        "  timeout_seconds: 300",
                        "phases:",
                        "  review-fix-loop:",
                        "    post:",
                        '      - run: ["bash", "-lc", "npm test"]',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = parse_yaml_like_file(path)

        self.assertEqual(parsed["defaults"]["timeout_seconds"], 300)
        self.assertEqual(parsed["phases"]["review-fix-loop"]["post"][0]["run"][2], "npm test")

    def test_repo_hook_overrides_user_hook_for_same_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            user = root / "user"
            repo = root / "repo"
            user.mkdir()
            (repo / ".kelpie").mkdir(parents=True)

            (user / "hooks.yaml").write_text(
                "\n".join(
                    [
                        "defaults:",
                        "  on_error: continue",
                        "phases:",
                        "  implementation:",
                        "    pre:",
                        '      - run: ["bash", "-lc", "echo from-user"]',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (repo / ".kelpie" / "hooks.yaml").write_text(
                "\n".join(
                    [
                        "phases:",
                        "  implementation:",
                        "    pre:",
                        '      - run: ["bash", "-lc", "echo from-repo"]',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            config = HookConfig.load(
                repo_hook_path=repo / ".kelpie" / "hooks.yaml",
                user_hook_path=user / "hooks.yaml",
            )

        commands = config.commands_for("implementation", "pre")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].run[2], "echo from-repo")
        self.assertEqual(commands[0].on_error, "continue")


class WorkflowHookExecutionTests(unittest.TestCase):
    def test_run_pre_hooks_writes_summary_and_stream_logs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            (workdir / "issues").mkdir()
            (workdir / "issues" / "1.md").write_text("# Issue 1\n", encoding="utf-8")
            (workdir / ".kelpie").mkdir()
            (workdir / ".kelpie" / "hooks.yaml").write_text(
                "\n".join(
                    [
                        "phases:",
                        "  implementation:",
                        "    pre:",
                        '      - run: ["bash", "-lc", "printf hook-output"]',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number="1",
                    runner_config=RunnerConfig(name="codex", command_template=["true"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="file",
                    dry_run=False,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            runner.run_pre_checks("implementation")

            checks_dir = workdir / ".kelpie" / "artifacts" / "file" / "local" / "issue-1" / "checks"
            summary = (checks_dir / "05-pre-check.txt").read_text(encoding="utf-8")
            stdout = (checks_dir / "05-pre-hook-01.stdout.txt").read_text(encoding="utf-8")
            stderr = (checks_dir / "05-pre-hook-01.stderr.txt").read_text(encoding="utf-8")

        self.assertIn("status: completed", summary)
        self.assertIn("05-pre-hook-01.stdout.txt", summary)
        self.assertEqual(stdout, "hook-output")
        self.assertEqual(stderr, "")

    def test_issue_optional_run_uses_manual_artifact_dir_and_prompt_context(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()

            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number=None,
                    runner_config=RunnerConfig(name="codex", command_template=["true"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="none",
                    task_label="Refactor Auth Flow",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            prompt = runner.compose_phase_prompt("prototype_planning")
            runner.run_phase("prototype_planning")

            artifact_dir = workdir / ".kelpie" / "artifacts" / "manual" / "local" / "task-refactor-auth-flow"
            prompt_file = artifact_dir / ".generated-prompts" / "prototype_planning.prompt.md"
            intent_file = artifact_dir / "intent-records" / "01-intent-record.json"

            self.assertTrue(prompt_file.exists())
            self.assertTrue(intent_file.exists())
            self.assertIn("Issue Number: (not provided)", prompt)
            self.assertIn("Issue Source: none", prompt)
            self.assertIn("Task Label: refactor-auth-flow", prompt)
            self.assertIn("No GitHub issue was provided", prompt)
            self.assertIn(str(artifact_dir.relative_to(workdir)), prompt)


if __name__ == "__main__":
    unittest.main()
