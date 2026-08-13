from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_issue_workflow import (
    PHASES,
    HookConfig,
    InstructionStagingConfig,
    RunnerPhaseOverride,
    RunnerConfig,
    RunnerNotFoundError,
    StepSpec,
    WorkflowRunner,
    parse_work_items_from_text,
    parse_yaml_like_file,
    slice_phases,
    validate_work_items_payload,
)


class HookConfigTests(unittest.TestCase):
    def test_plan_refinement_uses_base_runner_by_default(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = RunnerConfig.from_json(repo_root / "examples" / "runner_config.json", "codex")

        refinement = config.resolve_for_step("plan_refinement")

        self.assertEqual(refinement.command_template, config.command_template)
        self.assertNotEqual(
            refinement.command_template,
            config.resolve_for_phase("plan_comprehension_check").command_template,
        )

    def test_plan_refinement_supports_dedicated_step_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runner_config.json"
            path.write_text(
                json.dumps(
                    {
                        "runners": {
                            "custom": {
                                "command_template": ["strong", "-"],
                                "step_overrides": {
                                    "plan_refinement": {
                                        "command_template": ["stronger", "-"]
                                    }
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = RunnerConfig.from_json(path, "custom")

        self.assertEqual(
            config.resolve_for_step("plan_refinement").command_template,
            ["stronger", "-"],
        )

    def test_example_agy_runner_uses_codex_for_plan_check(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = RunnerConfig.from_json(repo_root / "examples" / "runner_config.json", "agy")
        plan_check = config.resolve_for_phase("plan_comprehension_check")

        self.assertIn("gemini-3.6-flash-medium", config.command_template)
        self.assertIn("accept-edits", config.command_template)
        self.assertEqual(plan_check.command_template[0], "codex")
        self.assertIn("gpt-5.6-luna", plan_check.command_template)
        self.assertIn('model_reasoning_effort="low"', plan_check.command_template)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", plan_check.command_template)

    def test_example_codex_runner_uses_copilot_for_plan_check(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = RunnerConfig.from_json(repo_root / "examples" / "runner_config.json", "codex")
        plan_check = config.resolve_for_phase("plan_comprehension_check")

        self.assertEqual(plan_check.command_template[0], "copilot")
        self.assertIn("gpt-5.6-luna", plan_check.command_template)
        self.assertIn("low", plan_check.command_template)
        self.assertIn("--disable-builtin-mcps", plan_check.command_template)

    def test_non_codex_example_runners_use_codex_for_plan_check(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "examples" / "runner_config.json"

        for runner_name in ("agy", "copilot", "opencode_ollama", "custom_file_prompt", "hybrid_cli"):
            with self.subTest(runner=runner_name):
                config = RunnerConfig.from_json(config_path, runner_name)
                plan_check = config.resolve_for_phase("plan_comprehension_check")
                self.assertEqual(plan_check.command_template[0], "codex")
                self.assertIn("gpt-5.6-luna", plan_check.command_template)
                self.assertIn('model_reasoning_effort="low"', plan_check.command_template)
                self.assertIn(
                    "--dangerously-bypass-approvals-and-sandbox",
                    plan_check.command_template,
                )
                self.assertEqual(plan_check.prompt_mode, "stdin")

    def test_example_codex_commands_use_role_specific_current_models(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "examples" / "runner_config.json"
        codex = RunnerConfig.from_json(config_path, "codex")
        hybrid = RunnerConfig.from_json(config_path, "hybrid_cli")

        for phase in ("prototype_planning", "review_fix_loop"):
            with self.subTest(phase=phase):
                command = codex.resolve_for_phase(phase).command_template
                self.assertIn("gpt-5.6-sol", command)

        for runner in (codex, hybrid):
            with self.subTest(runner=runner.name):
                command = runner.resolve_for_phase("implementation").command_template
                self.assertIn("gpt-5.6-luna", command)
                self.assertIn('model_reasoning_effort="max"', command)
                self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)

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
    def test_plan_refinement_runs_strong_model_after_clean_probe(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            runner = WorkflowRunner(
                repo_root=repo_root,
                workdir=workdir,
                issue_number=None,
                runner_config=RunnerConfig(name="codex", command_template=["strong-cli"]),
                instruction_staging_config=InstructionStagingConfig(),
                issue_source="none",
                task_label="refinement-clean",
                dry_run=False,
                allow_plan_check_external_send=True,
            )
            artifact_dir = runner.artifact_dir
            for name in ("04-solution-design.md", "05-work-breakdown.md"):
                (artifact_dir / name).write_text(f"# {name}\n", encoding="utf-8")
            (artifact_dir / "work_items.json").write_text('{"version":"1.0","tasks":[]}\n', encoding="utf-8")
            iteration = artifact_dir / "plan-check" / "iterations" / "0001"
            iteration.mkdir(parents=True)

            def fake_refinement(*args: object, **kwargs: object) -> None:
                _ = args, kwargs
                (iteration / "adjudication.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "input_snapshot_id": "snapshot-1",
                            "findings": [],
                            "plan_modified": False,
                            "modified_artifacts": [],
                            "unresolved_reasons": [],
                        }
                    ),
                    encoding="utf-8",
                )

            with (
                patch(
                    "scripts.run_issue_workflow.run_plan_check",
                    return_value={
                        "status": "completed_no_findings",
                        "snapshot_id": "snapshot-1",
                        "findings": [],
                    },
                ),
                patch.object(runner, "invoke_cli", side_effect=fake_refinement) as mock_refinement,
            ):
                result = runner.run_plan_refinement_loop(
                    artifact_dir=artifact_dir,
                    probe_runner=runner.runner_config.resolve_for_phase("plan_comprehension_check"),
                )
            intent = json.loads(
                (iteration / "refinement-intent-record.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "completed_no_change")
        self.assertEqual(intent["status"], "completed")
        self.assertEqual(intent["snapshot_id"], "snapshot-1")
        self.assertIn("prompt_sha256", intent)
        mock_refinement.assert_called_once()

    def test_plan_refinement_pause_persists_workflow_state(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            runner = WorkflowRunner(
                repo_root=repo_root,
                workdir=workdir,
                issue_number=None,
                runner_config=RunnerConfig(name="codex", command_template=["true"]),
                instruction_staging_config=InstructionStagingConfig(),
                issue_source="none",
                task_label="refinement-paused",
                dry_run=False,
            )
            (runner.artifact_dir / "05a-plan-comprehension-check.md").write_text(
                "# Plan Comprehension Check\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SystemExit, "paused_unresolved"):
                runner.record_plan_refinement_outcome(
                    runner.artifact_dir,
                    {"status": "paused_unresolved"},
                )
            state = json.loads(
                (runner.artifact_dir / "workflow-state.json").read_text(encoding="utf-8")
            )

        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["phase"], "plan_comprehension_check")

    def test_plan_comprehension_check_is_between_work_breakdown_and_implementation(self) -> None:
        self.assertLess(PHASES.index("work_breakdown"), PHASES.index("plan_comprehension_check"))
        self.assertLess(PHASES.index("plan_comprehension_check"), PHASES.index("implementation"))
        self.assertEqual(
            slice_phases("work_breakdown", "implementation"),
            ["work_breakdown", "plan_comprehension_check", "implementation"],
        )

    def test_plan_comprehension_dry_run_uses_internal_handler_without_external_invocation(self) -> None:
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
                    runner_config=RunnerConfig(name="codex", command_template=["codex", "exec", "-"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="none",
                    task_label="plan-check-test",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with (
                patch("scripts.run_issue_workflow.run_plan_check", return_value={"status": "prepared"}) as mock_check,
                patch.object(runner, "invoke_cli") as mock_invoke,
            ):
                runner.plan_comprehension_check()

        mock_check.assert_called_once()
        self.assertTrue(mock_check.call_args.kwargs["dry_run"])
        self.assertIn("plan comprehension check prompt", mock_check.call_args.kwargs["prompt_text"])
        self.assertIn("SKILL: plan comprehension check", mock_check.call_args.kwargs["prompt_text"])
        mock_invoke.assert_not_called()

    def test_plan_comprehension_without_external_opt_in_does_not_require_agy(self) -> None:
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
                    runner_config=RunnerConfig(name="custom", command_template=["custom-cli"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="none",
                    task_label="plan-check-no-send",
                    dry_run=False,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with patch(
                "scripts.run_issue_workflow.run_plan_check",
                return_value={"status": "approval_required"},
            ) as mock_check:
                with self.assertRaisesRegex(SystemExit, "approval_required"):
                    runner.plan_comprehension_check()

        mock_check.assert_called_once()
        self.assertFalse(mock_check.call_args.kwargs["allow_external_send"])

    def test_run_phase_delegates_to_run_step(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            (workdir / "issues").mkdir()
            (workdir / "issues" / "1.md").write_text("# Issue 1\n", encoding="utf-8")

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
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with patch.object(runner, "run_step") as mock_run_step:
                runner.run_phase("implementation")

        mock_run_step.assert_called_once()
        step_arg = mock_run_step.call_args.args[0]
        self.assertIsInstance(step_arg, StepSpec)
        self.assertEqual(step_arg.name, "implementation")
        self.assertEqual(step_arg.phase, "implementation")

    def test_every_top_level_phase_method_delegates_to_run_step(self) -> None:
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
                    task_label="phase-delegation",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with patch.object(runner, "run_step") as mock_run_step:
                for phase in PHASES:
                    getattr(runner, phase)()
                    step = mock_run_step.call_args.args[0]
                    self.assertEqual(step.name, phase)
                    self.assertEqual(step.phase, phase)

            self.assertEqual(mock_run_step.call_count, len(PHASES))

    def test_run_step_preserves_execution_order(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            (workdir / "issues").mkdir()
            (workdir / "issues" / "1.md").write_text("# Issue 1\n", encoding="utf-8")

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

            calls: list[str] = []
            step = StepSpec(name="implementation", phase="implementation")
            with (
                patch.object(runner, "write_intent_record_stub", side_effect=lambda *args, **kwargs: calls.append("intent")),
                patch.object(runner, "run_pre_checks", side_effect=lambda *args, **kwargs: calls.append("pre")),
                patch.object(runner, "invoke_cli", side_effect=lambda *args, **kwargs: calls.append("invoke")),
                patch.object(runner, "run_step_post_actions", side_effect=lambda *args, **kwargs: calls.append("post_actions")),
                patch.object(runner, "run_post_checks", side_effect=lambda *args, **kwargs: calls.append("post")),
                patch.object(runner, "evaluate_phase_outcome", side_effect=lambda *args, **kwargs: calls.append("outcome")),
            ):
                runner.run_step(step)

        self.assertEqual(calls, ["intent", "pre", "invoke", "post_actions", "post", "outcome"])

    def test_plan_comprehension_uses_common_lifecycle_dispatch(self) -> None:
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
                    task_label="plan-lifecycle",
                    dry_run=False,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            calls: list[str] = []
            with (
                patch.object(runner, "write_intent_record_stub", side_effect=lambda *args, **kwargs: calls.append("intent")),
                patch.object(runner, "run_pre_checks", side_effect=lambda *args, **kwargs: calls.append("pre")),
                patch.object(
                    runner,
                    "run_plan_refinement_loop",
                    side_effect=lambda *args, **kwargs: (calls.append("execute") or {"status": "completed_no_change"}),
                ),
                patch.object(runner, "run_step_post_actions", side_effect=lambda *args, **kwargs: calls.append("post_actions")),
                patch.object(runner, "run_post_checks", side_effect=lambda *args, **kwargs: calls.append("post")),
                patch.object(runner, "record_plan_refinement_outcome", side_effect=lambda *args, **kwargs: calls.append("outcome")),
            ):
                runner.run_phase("plan_comprehension_check")

        self.assertEqual(calls, ["intent", "pre", "execute", "post_actions", "post", "outcome"])

    def test_resolve_virtual_inputs_rejects_unknown_token(self) -> None:
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
                    task_label="virtual-input-test",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with self.assertRaisesRegex(ValueError, "Unknown virtual input token"):
                runner.resolve_virtual_inputs(["$unknown"])

    def test_resolve_artifact_scope_rejects_parent_traversal(self) -> None:
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
                    task_label="artifact-scope-test",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with self.assertRaisesRegex(ValueError, "Invalid artifact scope segment"):
                runner.resolve_artifact_scope(StepSpec(name="implementation", phase="implementation", artifact_subdir="../x"))

    def test_custom_step_resolves_named_runner_and_isolates_artifacts(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            base = RunnerConfig(name="base", command_template=["base-cli"])
            alternate = RunnerConfig(
                name="alternate",
                command_template=["alternate-cli", "{phase}"],
                phase_overrides={
                    "implementation": RunnerPhaseOverride(command_template=["alternate-implementation"])
                },
            )
            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number=None,
                    runner_config=base,
                    runner_registry={"alternate": alternate},
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="none",
                    task_label="named-step",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            runner.run_step(
                StepSpec(
                    name="loop-like",
                    phase="implementation",
                    runner_name="alternate",
                    prompt_file="prompts/04_solution_design.md",
                    skill_file="skills/solution-design/SKILL.md",
                    inputs=["plain-id"],
                    outputs=["declared-only.md"],
                    context_id="loop-7",
                    artifact_subdir="step-a",
                )
            )

            scoped = runner.artifact_dir / "loop-7" / "step-a"
            prompt = (scoped / ".generated-prompts" / "loop-like.prompt.md").read_text(encoding="utf-8")
            intent = json.loads(
                (scoped / "intent-records" / "loop-like-intent-record.json").read_text(encoding="utf-8")
            )
            output_exists = (scoped / "declared-only.md").exists()

        self.assertIn("# solution design prompt", prompt)
        self.assertIn("plain-id: plain-id", prompt)
        self.assertEqual(intent["effective_runner_config"]["command_template"], ["alternate-implementation"])
        self.assertEqual(intent["outputs"], ["declared-only.md"])
        self.assertFalse(output_exists)

    def test_invalid_step_metadata_has_no_scoped_artifact_side_effect(self) -> None:
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
                    task_label="invalid-step",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with self.assertRaisesRegex(ValueError, "Unknown virtual input token"):
                runner.run_step(
                    StepSpec(
                        name="loop-like",
                        phase="implementation",
                        context_id="ctx-1",
                        inputs=["$unknown"],
                    )
                )

            self.assertFalse((runner.artifact_dir / "ctx-1").exists())

    def test_external_prompt_override_is_rejected_by_run_step(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            external_prompt = Path(tmpdir) / "external.md"
            external_prompt.write_text("# External\n", encoding="utf-8")
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
                    task_label="external-prompt",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with self.assertRaisesRegex(ValueError, "prompt_file"):
                runner.run_step(
                    StepSpec(
                        name="loop-like",
                        phase="implementation",
                        prompt_file=str(external_prompt),
                        context_id="ctx-1",
                    )
                )
            self.assertFalse((runner.artifact_dir / "ctx-1").exists())

    def test_unknown_runner_is_rejected_before_scope_creation(self) -> None:
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
                    task_label="unknown-runner",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with self.assertRaisesRegex(RunnerNotFoundError, "not registered"):
                runner.run_step(
                    StepSpec(
                        name="loop-like",
                        phase="implementation",
                        runner_name="missing",
                        context_id="ctx-1",
                    )
                )
            self.assertFalse((runner.artifact_dir / "ctx-1").exists())

    def test_artifact_scope_symlink_is_rejected(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            outside = Path(tmpdir) / "outside"
            outside.mkdir()
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
                    task_label="symlink-scope",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            (runner.artifact_dir / "ctx-1").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "artifact root|Symlink"):
                runner.run_step(
                    StepSpec(
                        name="loop-like",
                        phase="implementation",
                        context_id="ctx-1",
                    )
                )
            self.assertEqual(list(outside.iterdir()), [])

    def test_child_artifact_symlink_swap_is_rejected_before_prompt_write(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = (Path(tmpdir) / "target-repo").resolve()
            workdir.mkdir()
            outside = Path(tmpdir) / "outside"
            outside.mkdir()
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
                    task_label="symlink-swap",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            resolved = runner.step_resolver.resolve(
                StepSpec(name="loop-like", phase="implementation", context_id="ctx-1")
            )
            prompt_dir = resolved.prompt_path.parent
            original_reject = runner._reject_symlink_components
            injected = False

            def inject_symlink_after_check(root: Path, path: Path) -> None:
                nonlocal injected
                original_reject(root, path)
                if path == prompt_dir and not injected:
                    prompt_dir.symlink_to(outside, target_is_directory=True)
                    injected = True

            with patch.object(
                runner,
                "_reject_symlink_components",
                side_effect=inject_symlink_after_check,
            ):
                with self.assertRaisesRegex(ValueError, "artifact root|Symlink"):
                    runner.prepare_resolved_step(resolved)

            self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_kelpie_root_is_rejected_before_artifact_writes(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            outside = Path(tmpdir) / "outside"
            outside.mkdir()
            (workdir / ".kelpie").symlink_to(outside, target_is_directory=True)
            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                with self.assertRaisesRegex(ValueError, "Symlinked kelpie directory"):
                    WorkflowRunner(
                        repo_root=repo_root,
                        workdir=workdir,
                        issue_number=None,
                        runner_config=RunnerConfig(name="codex", command_template=["true"]),
                        instruction_staging_config=InstructionStagingConfig(),
                        issue_source="none",
                        task_label="symlink-root",
                        dry_run=True,
                    )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            self.assertEqual(list(outside.iterdir()), [])

    def test_same_scope_rerun_is_allowed_but_existing_lock_fails_fast(self) -> None:
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
                    task_label="scope-lock",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            step = StepSpec(name="loop-like", phase="implementation", context_id="ctx-1")
            runner.run_step(step)
            runner.run_step(step)
            lock_path = runner.artifact_dir / "ctx-1" / ".step-lock"
            lock_path.write_text("held\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "already locked"):
                runner.run_step(step)

    def test_virtual_input_truncation_is_recorded(self) -> None:
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
                    task_label="truncation",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with patch.dict(os.environ, {"KELPIE_LOOP_ITEM": "x" * 2001}):
                runner.run_step(
                    StepSpec(
                        name="loop-like",
                        phase="implementation",
                        inputs=["$loop_item"],
                        context_id="ctx-1",
                    )
                )

            scoped = runner.artifact_dir / "ctx-1"
            prompt = (scoped / ".generated-prompts" / "loop-like.prompt.md").read_text(encoding="utf-8")
            intent = json.loads(
                (scoped / "intent-records" / "loop-like-intent-record.json").read_text(encoding="utf-8")
            )

        self.assertIn('original_length="2001"', prompt)
        self.assertIn('truncated="true"', prompt)
        self.assertEqual(intent["inputs"][0]["original_length"], 2001)
        self.assertTrue(intent["inputs"][0]["truncated"])

    def test_parse_work_items_from_text_returns_first_schema_valid_candidate(self) -> None:
        source = """
```json
{
  "tasks": [
    {
      "id": "task-0",
      "title": "Invalid: missing description"
    }
  ]
}
```

```json
{
  "tasks": [
    {
      "id": "task-1-valid",
      "title": "Title",
      "description": "Description"
    }
  ]
}
```
""".strip()
        payload = parse_work_items_from_text(source)
        self.assertEqual(payload["tasks"][0]["id"], "task-1-valid")

    def test_validate_work_items_payload_rejects_missing_required_field(self) -> None:
        payload = {
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Title",
                }
            ]
        }
        self.assertEqual(
            validate_work_items_payload(payload),
            "tasks[0].description must be a non-empty string",
        )

    def test_run_phase_work_breakdown_writes_work_items_json(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            (workdir / "issues").mkdir()
            (workdir / "issues" / "1.md").write_text("# Issue 1\n", encoding="utf-8")

            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number="1",
                    runner_config=RunnerConfig(name="codex", command_template=["mock-cli"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="file",
                    dry_run=False,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
                _ = args, kwargs
                runner.work_breakdown_markdown_path().write_text(
                    "\n".join(
                        [
                            "# Work Breakdown",
                            "```json",
                            '{"tasks":[{"id":"task-1","title":"Title","description":"Description"}]}',
                            "```",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0)

            with (
                patch("scripts.run_issue_workflow.subprocess.run", side_effect=fake_run),
                patch.object(runner, "evaluate_phase_outcome"),
            ):
                runner.run_phase("work_breakdown")

            payload = json.loads(runner.work_items_json_path().read_text(encoding="utf-8"))
            self.assertEqual(payload["tasks"][0]["id"], "task-1")
            self.assertFalse(runner.work_items_error_path().exists())

    def test_run_phase_work_breakdown_fails_when_work_items_invalid(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            (workdir / "issues").mkdir()
            (workdir / "issues" / "1.md").write_text("# Issue 1\n", encoding="utf-8")

            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number="1",
                    runner_config=RunnerConfig(name="codex", command_template=["mock-cli"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="file",
                    dry_run=False,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            runner.work_items_json_path().write_text(
                json.dumps({"tasks": [{"id": "stale", "title": "Old", "description": "Old"}]}),
                encoding="utf-8",
            )

            def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
                _ = args, kwargs
                runner.work_breakdown_markdown_path().write_text(
                    "\n".join(
                        [
                            "# Work Breakdown",
                            "```json",
                            '{"tasks":[{"id":"task-1","title":"Title"}]}',
                            "```",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0)

            with patch("scripts.run_issue_workflow.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(SystemExit, "Invalid work_items payload"):
                    runner.run_phase("work_breakdown")

            self.assertTrue(runner.work_items_error_path().exists())
            self.assertFalse(runner.work_items_json_path().exists())

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

            with (
                patch("scripts.run_issue_workflow.subprocess.run") as mock_run,
                patch.object(runner, "evaluate_phase_outcome"),
            ):
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

    def test_compose_phase_prompt_uses_prompt_file_and_skill_file_overrides(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()

            custom_prompt = Path(tmpdir) / "custom_prompt.md"
            custom_prompt.write_text("# Custom Prompt\nMy custom task instructions.\n", encoding="utf-8")
            custom_skill = Path(tmpdir) / "custom_skill.md"
            custom_skill.write_text("# Custom Skill\nMy custom skill rules.\n", encoding="utf-8")

            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                runner_config = RunnerConfig(
                    name="codex",
                    command_template=["true"],
                    prompt_file=str(custom_prompt),
                    skill_file=str(custom_skill),
                )
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number=None,
                    runner_config=runner_config,
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="none",
                    task_label="Test Override",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            resolved = runner_config.resolve_for_phase("prototype_planning")
            prompt = runner.compose_phase_prompt("prototype_planning", resolved)

        self.assertIn("My custom task instructions.", prompt)
        self.assertIn("My custom skill rules.", prompt)
        self.assertNotIn("prototype_planning", prompt.split("# Phase Prompt")[1].split("\n")[0])

    def test_compose_phase_prompt_documents_artifact_relative_outcome_paths(self) -> None:
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
                    issue_number="2",
                    runner_config=RunnerConfig(name="codex", command_template=["true"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="none",
                    dry_run=True,
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            prompt = runner.compose_phase_prompt(
                "red_team_review",
                runner.runner_config.resolve_for_phase("red_team_review"),
            )

        self.assertIn("`evidence_refs` paths are relative to the current `Artifact Directory`", prompt)
        self.assertIn("Do not prefix an evidence path with `.kelpie/`", prompt)
        self.assertIn("Do not use `..`, `src/...`, or", prompt)
        self.assertIn("Leave `artifact_digests` as {}", prompt)
        self.assertIn("without a `sha256:` prefix", prompt)


if __name__ == "__main__":
    unittest.main()
