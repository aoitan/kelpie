from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.run_issue_workflow import (
    DEFAULT_WORKFLOW_CONFIG_PATH,
    PHASES,
    WorkflowRunner,
    load_configured_workflow_definition,
    main,
    parse_args,
)


class WorkflowCliTests(unittest.TestCase):
    repository_root = Path(__file__).resolve().parents[1]

    @staticmethod
    def runner_config_payload() -> dict[str, object]:
        return {
            "runners": {
                "codex": {
                    "command_template": ["true"],
                    "prompt_mode": "stdin",
                }
            }
        }

    @staticmethod
    def step(
        node_id: str,
        lifecycle: str,
        prompt: str,
        skill: str,
    ) -> dict[str, object]:
        return {
            "type": "step",
            "id": node_id,
            "lifecycle": lifecycle,
            "runner": "codex",
            "prompt": prompt,
            "skill": skill,
            "inputs": [],
            "outputs": [],
            "depends_on": [],
        }

    def config_payload(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "id": "cli-workflow",
            "profile": "repository_issue",
            "limits": {},
            "nodes": [
                self.step(
                    "first",
                    "kelpie.phase.prototype_planning.v1",
                    "prompts/01_prototype_planning.md",
                    "skills/prototype-planning/SKILL.md",
                ),
                self.step(
                    "second",
                    "kelpie.phase.prototyping.v1",
                    "prompts/02_prototyping.md",
                    "skills/prototyping/SKILL.md",
                ),
            ],
        }

    def write_cli_files(self, root: Path) -> tuple[Path, Path]:
        workflow_path = root / "workflow.json"
        runner_path = root / "runner.json"
        workflow_path.write_text(
            json.dumps(self.config_payload()),
            encoding="utf-8",
        )
        runner_path.write_text(
            json.dumps(self.runner_config_payload()),
            encoding="utf-8",
        )
        return workflow_path, runner_path

    def cli_argv(
        self,
        *,
        workdir: Path,
        workflow_path: Path | None = None,
        runner_path: Path | None = None,
        dry_run: bool = True,
        extra: tuple[str, ...] = (),
    ) -> list[str]:
        argv = [
            "run_issue_workflow.py",
            "--repo-root",
            str(self.repository_root),
            "--workdir",
            str(workdir),
            "--issue-source",
            "none",
            "--task-label",
            "cli-test",
            "--runner",
            "codex",
            "--runner-config",
            str(runner_path or self.repository_root / "examples" / "runner_config.json"),
            "--instruction-staging-config",
            str(self.repository_root / "examples" / "instruction_staging.json"),
        ]
        if workflow_path is not None:
            argv.extend(("--workflow-config", str(workflow_path)))
        if dry_run:
            argv.append("--dry-run")
        argv.extend(extra)
        return argv

    def test_defaults_select_configured_workflow_and_legacy_requires_flag(self) -> None:
        with patch.object(sys, "argv", [
            "run_issue_workflow.py",
            "--workdir",
            ".",
            "--runner",
            "codex",
        ]):
            args = parse_args()
        self.assertEqual(args.workflow_config, DEFAULT_WORKFLOW_CONFIG_PATH)
        self.assertFalse(args.legacy_workflow)

        with patch.object(sys, "argv", [
            "run_issue_workflow.py",
            "--workdir",
            ".",
            "--runner",
            "codex",
            "--legacy-workflow",
            "--from-phase",
            "implementation",
            "--to-phase",
            "implementation",
        ]):
            args = parse_args()
        self.assertTrue(args.legacy_workflow)
        self.assertEqual(args.from_phase, "implementation")
        self.assertEqual(args.to_phase, "implementation")

    def test_invalid_config_is_rejected_before_runner_construction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            invalid_path = root / "invalid.json"
            invalid_path.write_text('{"schema_version":"2.0"}\n', encoding="utf-8")
            workdir = root / "workdir"
            workdir.mkdir()
            argv = self.cli_argv(
                workdir=workdir,
                workflow_path=invalid_path,
                dry_run=True,
            )

            with patch.object(sys, "argv", argv), patch(
                "scripts.run_issue_workflow.WorkflowRunner"
            ) as runner_class:
                with self.assertRaises(SystemExit) as context:
                    main()

        self.assertIn("Invalid workflow config", str(context.exception))
        runner_class.assert_not_called()
        self.assertFalse((workdir / ".kelpie").exists())

    def test_explicit_config_controls_cli_execution_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow_path, runner_path = self.write_cli_files(root)
            workdir = root / "workdir"
            workdir.mkdir()
            calls: list[str] = []
            artifact_dir_exists = False

            def record_invoke(
                _runner: WorkflowRunner,
                phase: str,
                _prompt_text: str,
                _prompt_path: Path,
                _runner_config: object,
            ) -> None:
                calls.append(phase)

            reordered = self.config_payload()
            reordered["nodes"] = list(reversed(reordered["nodes"]))  # type: ignore[arg-type]
            workflow_path.write_text(json.dumps(reordered), encoding="utf-8")
            argv = self.cli_argv(
                workdir=workdir,
                workflow_path=workflow_path,
                runner_path=runner_path,
            )
            with patch.dict(
                os.environ,
                {"KELPIE_CONFIG_HOME": str(root / "empty-config")},
                clear=False,
            ), patch.object(sys, "argv", argv), patch.object(
                WorkflowRunner,
                "invoke_cli",
                autospec=True,
                side_effect=record_invoke,
            ):
                main()
                artifact_dir_exists = (
                    workdir
                    / ".kelpie"
                    / "artifacts"
                    / "manual"
                    / "local"
                    / "task-cli-test"
                ).is_dir()

        self.assertEqual(calls, ["prototyping", "prototype_planning"])
        self.assertTrue(artifact_dir_exists)

    def test_legacy_execution_is_only_reached_with_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workdir = root / "workdir"
            workdir.mkdir()
            runner = Mock()
            argv = self.cli_argv(
                workdir=workdir,
                dry_run=True,
                extra=(
                    "--legacy-workflow",
                    "--from-phase",
                    "prototype_planning",
                    "--to-phase",
                    "prototype_planning",
                ),
            )
            with patch.object(sys, "argv", argv), patch(
                "scripts.run_issue_workflow.WorkflowRunner",
                return_value=runner,
            ) as runner_class:
                main()

        runner_class.assert_called_once()
        runner.run.assert_called_once_with(["prototype_planning"])

    def test_default_planning_config_dry_run_does_not_require_work_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workdir = root / "workdir"
            workdir.mkdir()
            calls: list[str] = []

            def record_invoke(
                _runner: WorkflowRunner,
                phase: str,
                _prompt_text: str,
                _prompt_path: Path,
                _runner_config: object,
            ) -> None:
                calls.append(phase)

            argv = self.cli_argv(workdir=workdir)
            with patch.object(sys, "argv", argv), patch.object(
                WorkflowRunner,
                "invoke_cli",
                autospec=True,
                side_effect=record_invoke,
            ):
                main()

        self.assertEqual(
            calls,
            [
                "prototype_planning",
                "prototyping",
                "red_team_review",
                "solution_design",
                "work_breakdown",
            ],
        )

    def test_execution_config_requires_preexisting_work_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workdir = root / "workdir"
            workdir.mkdir()
            argv = self.cli_argv(
                workdir=workdir,
                workflow_path=self.repository_root / "workflows" / "issue-v1-execution.json",
            )
            with patch.object(sys, "argv", argv), patch.object(
                WorkflowRunner,
                "invoke_cli",
                autospec=True,
            ) as invoke_cli:
                with self.assertRaisesRegex(
                    SystemExit,
                    "configured workflow loop source is unavailable",
                ):
                    main()

        invoke_cli.assert_not_called()

    def test_execution_config_dry_run_uses_preexisting_work_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workdir = root / "workdir"
            workdir.mkdir()
            source_dir = (
                workdir
                / ".kelpie"
                / "artifacts"
                / "manual"
                / "local"
                / "task-cli-test"
            )
            source_dir.mkdir(parents=True)
            (source_dir / "work_items.json").write_text(
                json.dumps(
                    {
                        "version": "1.0",
                        "tasks": [
                            {
                                "id": "WB-10",
                                "title": "CLI migration",
                                "description": "exercise the split execution workflow",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            calls: list[str] = []

            def record_invoke(
                _runner: WorkflowRunner,
                phase: str,
                _prompt_text: str,
                _prompt_path: Path,
                _runner_config: object,
            ) -> None:
                calls.append(phase)

            argv = self.cli_argv(
                workdir=workdir,
                workflow_path=self.repository_root / "workflows" / "issue-v1-execution.json",
            )
            with patch.object(sys, "argv", argv), patch.object(
                WorkflowRunner,
                "invoke_cli",
                autospec=True,
                side_effect=record_invoke,
            ):
                main()

        self.assertEqual(
            calls,
            [
                "implementation",
                "implementation",
                "implementation",
                "implementation",
                "review_fix_loop",
                "pull_request",
            ],
        )

    def test_runner_registry_loader_includes_configured_step_runners(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow_path, runner_path = self.write_cli_files(root)
            config, runners = load_configured_workflow_definition(
                workflow_path,
                repo_root=self.repository_root,
                runner_config_path=runner_path,
                bundled_runner_config_path=self.repository_root / "examples" / "runner_config.json",
                default_runner="codex",
            )

        self.assertEqual(config.workflow_id, "cli-workflow")
        self.assertEqual(tuple(runners), ("codex",))


if __name__ == "__main__":
    unittest.main()
