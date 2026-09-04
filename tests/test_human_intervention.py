from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.human_intervention import (
    ACTIONS_REQUIRING_PROMPT,
    available_actions,
    build_request_payload,
    dump_payload,
    normalize_action,
    validate_request_payload,
)
from scripts.run_issue_workflow import (
    PhaseOutcomeStop,
    InstructionStagingConfig,
    RunnerConfig,
    StepSpec,
    WorkflowRunner,
    load_run_manifest,
    main,
    resolve_run_dir,
)
from scripts.workflow_outcomes import PhaseOutcome


class HumanInterventionPolicyTests(unittest.TestCase):
    def test_high_severity_unresolved_requires_a_change_or_reopen_decision(self) -> None:
        actions = available_actions("high_severity_unresolved", "pause")

        self.assertEqual(actions, ("request-changes", "reopen", "abort"))
        self.assertNotIn("approve", actions)
        self.assertIn("request-changes", ACTIONS_REQUIRING_PROMPT)

    def test_request_payload_round_trips_with_strict_schema(self) -> None:
        payload = build_request_payload(
            request_id="intervention-0001",
            phase="review_fix_loop",
            decision="pause",
            reason_code="high_severity_unresolved",
            summary="A high severity finding remains.",
            resume_condition="Resolve RF-01 and rerun review.",
            outcome_path="phase-outcomes/review_fix_loop/0001.json",
            outcome_sha256="a" * 64,
            evidence_refs=["07-review-fix-loop.md"],
            created_at="2026-09-01T00:00:00+00:00",
        )

        self.assertEqual(validate_request_payload(payload), payload)
        self.assertEqual(normalize_action("request_changes"), "request-changes")


class WorkflowRunnerHumanInterventionTests(unittest.TestCase):
    def make_runner(self, tmpdir: str, *, task_label: str = "human-intervention") -> WorkflowRunner:
        repo_root = Path(__file__).resolve().parents[1]
        workdir = Path(tmpdir) / "target-repo"
        workdir.mkdir()
        config_home = Path(tmpdir) / "empty-config"
        return WorkflowRunner(
            repo_root=repo_root,
            workdir=workdir,
            issue_number=None,
            runner_config=RunnerConfig(name="test", command_template=["test-cli"]),
            instruction_staging_config=InstructionStagingConfig(),
            issue_source="none",
            task_label=task_label,
            dry_run=False,
            runner_registry={},
        )

    def test_pause_writes_request_and_resume_prompt_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"KELPIE_CONFIG_HOME": str(Path(tmpdir) / "empty-config")}):
                runner = self.make_runner(tmpdir)
                artifact_dir = runner.artifact_dir
                (artifact_dir / "07-review-fix-loop.md").write_text(
                    "# Review\n\nRF-01 remains open.\n",
                    encoding="utf-8",
                )
                (artifact_dir / "review-evidence.md").write_text("RF-01\n", encoding="utf-8")
                outcome_path = runner.phase_outcome_path("review_fix_loop", artifact_dir)
                outcome_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "phase": "review_fix_loop",
                            "decision": "pause",
                            "reason_code": "high_severity_unresolved",
                            "summary": "A high severity finding remains.",
                            "evidence_refs": ["review-evidence.md"],
                            "resume_condition": "Resolve RF-01 and rerun review.",
                            "artifact_digests": {},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(PhaseOutcomeStop, "high_severity_unresolved"):
                    runner.evaluate_phase_outcome("review_fix_loop", artifact_dir)

                state = json.loads((artifact_dir / "workflow-state.json").read_text(encoding="utf-8"))
                request_path = artifact_dir / state["intervention_request_path"]
                request = json.loads(request_path.read_text(encoding="utf-8"))
                intervention = runner.record_human_intervention(
                    state,
                    "request-changes",
                    "Resolve RF-01 in the implementation and add a regression test.",
                )
                prompt = runner.compose_phase_prompt(
                    "review_fix_loop",
                    runner.runner_config.resolve_for_phase("review_fix_loop"),
                    artifact_dir=artifact_dir,
                )
                response_state = json.loads(
                    (artifact_dir / "workflow-state.json").read_text(encoding="utf-8")
                )

            self.assertIsNotNone(intervention)
            self.assertEqual(request["available_actions"], ["request-changes", "reopen", "abort"])
            self.assertIn("Human Intervention", prompt)
            self.assertIn("Resolve RF-01 in the implementation", prompt)
            response_path = artifact_dir / response_state["intervention_response_path"]
            response = json.loads(response_path.read_text(encoding="utf-8"))
            self.assertEqual(response["request_id"], request["request_id"])
            self.assertEqual(response["action"], "request-changes")
            self.assertEqual(response_state["intervention_status"], "accepted")

    def test_stale_request_is_rejected_before_accepting_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"KELPIE_CONFIG_HOME": str(Path(tmpdir) / "empty-config")}):
                runner = self.make_runner(tmpdir, task_label="stale-request")
                artifact_dir = runner.artifact_dir
                (artifact_dir / "07-review-fix-loop.md").write_text("# Review\n", encoding="utf-8")
                outcome_path = runner.phase_outcome_path("review_fix_loop", artifact_dir)
                outcome_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "phase": "review_fix_loop",
                            "decision": "pause",
                            "reason_code": "high_severity_unresolved",
                            "summary": "A high severity finding remains.",
                            "evidence_refs": [],
                            "resume_condition": "Resolve the finding.",
                            "artifact_digests": {},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(PhaseOutcomeStop):
                    runner.evaluate_phase_outcome("review_fix_loop", artifact_dir)
                state = json.loads((artifact_dir / "workflow-state.json").read_text(encoding="utf-8"))
                request_path = artifact_dir / state["intervention_request_path"]
                request_path.write_text(request_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

                with self.assertRaisesRegex(SystemExit, "digest"):
                    runner.record_human_intervention(state, "request-changes", "Fix it.")

    def test_runner_failure_becomes_retryable_human_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"KELPIE_CONFIG_HOME": str(Path(tmpdir) / "empty-config")}):
                runner = self.make_runner(tmpdir, task_label="runner-failure")
                with patch.object(runner, "invoke_cli", side_effect=SystemExit("runner unavailable")):
                    with self.assertRaisesRegex(SystemExit, "runner unavailable"):
                        runner.run_step(StepSpec(name="review", phase="review_fix_loop"))

                state = json.loads((runner.artifact_dir / "workflow-state.json").read_text(encoding="utf-8"))
                request = json.loads(
                    (runner.artifact_dir / state["intervention_request_path"]).read_text(encoding="utf-8")
                )

            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["reason_code"], "execution_error")
            self.assertEqual(request["available_actions"], ["retry", "reopen", "abort"])

    def test_missing_outcome_becomes_retryable_human_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"KELPIE_CONFIG_HOME": str(Path(tmpdir) / "empty-config")}):
                runner = self.make_runner(tmpdir, task_label="missing-outcome")
                with self.assertRaisesRegex(PhaseOutcomeStop, "did not create required outcome"):
                    runner.evaluate_phase_outcome("review_fix_loop", runner.artifact_dir)

                state = json.loads(
                    (runner.artifact_dir / "workflow-state.json").read_text(encoding="utf-8")
                )
                request = json.loads(
                    (runner.artifact_dir / state["intervention_request_path"]).read_text(encoding="utf-8")
                )

            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["reason_code"], "artifact_invalid")
            self.assertEqual(request["available_actions"], ["retry", "reopen", "abort"])

    def test_scoped_request_prints_scoped_run_dir_in_resume_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"KELPIE_CONFIG_HOME": str(Path(tmpdir) / "empty-config")}):
                runner = self.make_runner(tmpdir, task_label="scoped-request")
                artifact_dir = runner.artifact_dir / "work-items" / "WI-01"
                runner.prepare_artifact_scope(artifact_dir)
                outcome_path = runner.phase_outcome_path("review_fix_loop", artifact_dir)
                outcome_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "phase": "review_fix_loop",
                            "decision": "pause",
                            "reason_code": "high_severity_unresolved",
                            "summary": "A high severity finding remains.",
                            "evidence_refs": [],
                            "resume_condition": "Resolve the finding.",
                            "artifact_digests": {},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                outcome = PhaseOutcome.from_dict(
                    json.loads(outcome_path.read_text(encoding="utf-8")),
                    expected_phase="review_fix_loop",
                )
                stdout = io.StringIO()

                with redirect_stdout(stdout):
                    runner.write_human_intervention_request(
                        outcome,
                        outcome_path,
                        artifact_dir=artifact_dir,
                    )

            self.assertIn(
                f"--run-dir {artifact_dir.relative_to(runner.workdir)}",
                stdout.getvalue(),
            )


class RunDirectoryTests(unittest.TestCase):
    def test_run_dir_is_constrained_to_artifact_root(self) -> None:
        workdir = Path("/tmp/kelpie-target")

        self.assertEqual(
            resolve_run_dir(workdir, ".kelpie/artifacts/manual/local/task-1"),
            workdir / ".kelpie/artifacts/manual/local/task-1",
        )
        with self.assertRaises(SystemExit):
            resolve_run_dir(workdir, "../outside")

    def test_run_manifest_is_optional_and_json_backed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            self.assertEqual(load_run_manifest(run_dir), {})
            (run_dir / "run-manifest.json").write_text(
                dump_payload({"schema_version": "1.0", "runner": "codex"}),
                encoding="utf-8",
            )

            self.assertEqual(load_run_manifest(run_dir)["runner"], "codex")

    def test_resume_runner_reuses_cached_github_context(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            with patch.dict(os.environ, {"KELPIE_CONFIG_HOME": str(Path(tmpdir) / "empty-config")}):
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number="12",
                    runner_config=RunnerConfig(name="test", command_template=["test-cli"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="github",
                    github_repo="owner/repo",
                    reuse_issue_cache=True,
                )
                (runner.issue_cache_dir / "issue.json").write_text(
                    json.dumps({"number": 12, "title": "Cached", "body": "offline context"}),
                    encoding="utf-8",
                )
                with patch.object(runner, "run_gh_json") as gh_json:
                    issue_text = runner.read_issue_text()

            gh_json.assert_not_called()
            self.assertIn("offline context", issue_text)

    def test_main_can_resume_from_manifest_without_issue_arguments(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            workdir = tmp_path / "target-repo"
            workdir.mkdir()
            config_home = tmp_path / "empty-config"
            runner_config_path = tmp_path / "runner-config.json"
            runner_config_path.write_text(
                json.dumps(
                    {"runners": {"test": {"command_template": ["test-cli"]}}}
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"KELPIE_CONFIG_HOME": str(config_home)}):
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number=None,
                    runner_config=RunnerConfig(name="test", command_template=["test-cli"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="none",
                    task_label="manifest-resume",
                    dry_run=False,
                )
                (runner.artifact_dir / "07-review-fix-loop.md").write_text(
                    "# Review\n",
                    encoding="utf-8",
                )
                outcome_path = runner.phase_outcome_path("review_fix_loop", runner.artifact_dir)
                outcome_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "phase": "review_fix_loop",
                            "decision": "pause",
                            "reason_code": "high_severity_unresolved",
                            "summary": "A high severity finding remains.",
                            "evidence_refs": [],
                            "resume_condition": "Resolve the finding.",
                            "artifact_digests": {},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(PhaseOutcomeStop):
                    runner.evaluate_phase_outcome("review_fix_loop", runner.artifact_dir)
                run_dir = runner.artifact_dir.relative_to(workdir)

                argv = [
                    "run_issue_workflow.py",
                    "--repo-root",
                    str(repo_root),
                    "--workdir",
                    str(workdir),
                    "--run-dir",
                    str(run_dir),
                    "--runner-config",
                    str(runner_config_path),
                    "--resume",
                    "--resume-action",
                    "reopen",
                    "--resume-phase",
                    "implementation",
                    "--resume-prompt",
                    "Recreate the implementation artifact and rerun review.",
                ]
                with patch("sys.argv", argv), patch.object(WorkflowRunner, "run") as run_mock:
                    main()

                state = json.loads(
                    (runner.artifact_dir / "workflow-state.json").read_text(encoding="utf-8")
                )

            run_mock.assert_called_once()
            self.assertEqual(
                run_mock.call_args.args[0],
                ["implementation", "review_fix_loop", "pull_request"],
            )
            self.assertEqual(state["intervention_action"], "reopen")
            self.assertEqual(state["intervention_target_phase"], "implementation")
            self.assertEqual(state["intervention_status"], "accepted")


if __name__ == "__main__":
    unittest.main()
