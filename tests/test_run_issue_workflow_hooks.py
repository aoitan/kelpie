from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_issue_workflow import (
    HookConfig,
    InstructionStagingConfig,
    RunnerConfig,
    WorkflowRunner,
    parse_yaml_like_file,
)


class HookConfigTests(unittest.TestCase):
    def test_runner_config_resolve_for_phase_uses_base_values_without_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                """
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "-"],
      "prompt_mode": "stdin"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = RunnerConfig.from_json(path, "codex")
            resolved = config.resolve_for_phase("implementation")

        self.assertEqual(resolved.command_template, ["codex", "exec", "-"])
        self.assertEqual(resolved.prompt_mode, "stdin")

    def test_runner_config_resolve_for_phase_applies_override_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                """
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "--full-auto", "-"],
      "prompt_mode": "stdin",
      "phase_overrides": {
        "implementation": {
          "command_template": ["codex", "exec", "--model", "gpt-5-codex", "--full-auto", "-"],
          "prompt_mode": "arg"
        }
      }
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = RunnerConfig.from_json(path, "codex")
            resolved = config.resolve_for_phase("implementation")
            fallback = config.resolve_for_phase("planning")

        self.assertEqual(
            resolved.command_template,
            ["codex", "exec", "--model", "gpt-5-codex", "--full-auto", "-"],
        )
        self.assertEqual(resolved.prompt_mode, "arg")
        self.assertEqual(fallback.command_template, ["codex", "exec", "--full-auto", "-"])
        self.assertEqual(fallback.prompt_mode, "stdin")

    def test_runner_config_from_json_rejects_invalid_override_prompt_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                """
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "-"],
      "phase_overrides": {
        "review_fix_loop": {
          "prompt_mode": "pipe"
        }
      }
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "Unsupported phase_overrides.review_fix_loop.prompt_mode: pipe",
            ):
                RunnerConfig.from_json(path, "codex")

    def test_runner_config_from_json_rejects_invalid_base_command_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                """
{
  "runners": {
    "codex": {
      "command_template": []
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "command_template must be a non-empty list\\[str\\]",
            ):
                RunnerConfig.from_json(path, "codex")

    def test_runner_config_from_json_rejects_invalid_override_command_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                """
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "-"],
      "phase_overrides": {
        "implementation": {
          "command_template": ["codex", 123]
        }
      }
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "phase_overrides.implementation.command_template must be a non-empty list\\[str\\]",
            ):
                RunnerConfig.from_json(path, "codex")

    def test_runner_config_from_json_rejects_unknown_override_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                """
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "-"],
      "phase_overrides": {
        "implementaton": {
          "prompt_mode": "arg"
        }
      }
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "Unsupported phase in phase_overrides: implementaton",
            ):
                RunnerConfig.from_json(path, "codex")

    def test_runner_config_from_json_rejects_unknown_override_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                """
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "-"],
      "phase_overrides": {
        "implementation": {
          "command_templte": ["codex", "exec", "--model", "gpt-5-codex", "-"]
        }
      }
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "phase_overrides.implementation has unsupported keys: command_templte",
            ):
                RunnerConfig.from_json(path, "codex")

    def test_runner_config_from_json_rejects_non_mapping_phase_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                """
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "-"],
      "phase_overrides": {
        "implementation": ["codex", "exec", "--full-auto", "-"]
      }
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "phase_overrides.implementation must be a mapping",
            ):
                RunnerConfig.from_json(path, "codex")

    def test_runner_config_from_json_normalizes_hyphenated_phase_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                """
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "-"],
      "phase_overrides": {
        "review-fix-loop": {
          "prompt_mode": "arg"
        }
      }
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = RunnerConfig.from_json(path, "codex")

        resolved = config.resolve_for_phase("review_fix_loop")
        self.assertEqual(resolved.prompt_mode, "arg")

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
    def test_run_phase_uses_resolved_runner_config_for_cli_and_intent_record(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            (workdir / "issues").mkdir()
            (workdir / "issues" / "issue-phase-overrides-runner-config.md").write_text("# Issue\n", encoding="utf-8")

            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                runner_config_path = self._write_runner_config_with_override(Path(tmpdir))
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number="phase-overrides-runner-config",
                    runner_config=RunnerConfig.from_json(runner_config_path, "codex"),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="file",
                    dry_run=False,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with patch("scripts.run_issue_workflow.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = ""
                mock_run.return_value.stderr = ""

                runner.run_phase("implementation")

            call = mock_run.call_args
            self.assertIsNotNone(call)
            self.assertEqual(call.args[0], ["override-cli", "implementation"])
            self.assertIn("input", call.kwargs)
            self.assertNotIn("base-cli", call.args[0])

            artifact_dir = workdir / ".kelpie" / "artifacts" / "file" / "local" / "issue-phase-overrides-runner-config"
            intent_payload = json.loads(
                (artifact_dir / "intent-records" / "06-intent-record.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                intent_payload["effective_runner_config"],
                {
                    "command_template": ["override-cli", "{phase}"],
                    "prompt_mode": "stdin",
                },
            )

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
            summary = (checks_dir / "06-pre-check.txt").read_text(encoding="utf-8")
            stdout = (checks_dir / "06-pre-hook-01.stdout.txt").read_text(encoding="utf-8")
            stderr = (checks_dir / "06-pre-hook-01.stderr.txt").read_text(encoding="utf-8")

        self.assertIn("status: completed", summary)
        self.assertIn("06-pre-hook-01.stdout.txt", summary)
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

            prompt = runner.compose_phase_prompt(
                "prototype_planning",
                runner.runner_config.resolve_for_phase("prototype_planning"),
            )
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

    def _write_runner_config_with_override(self, root: Path) -> Path:
        path = root / "runner_config.json"
        path.write_text(
            """
{
  "runners": {
    "codex": {
      "command_template": ["base-cli", "{phase}"],
      "prompt_mode": "stdin",
      "phase_overrides": {
        "implementation": {
          "command_template": ["override-cli", "{phase}"]
        }
      }
    }
  }
}
""".strip()
            + "\n",
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
