"""Regression coverage for optional, non-blocking plan clarification."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.run_issue_workflow import (
    PHASES, InstructionStagingConfig, RunnerConfig, WorkflowRunner, main, parse_args,
)
from scripts.plan_comprehension import run_plan_check


class OptionalPlanCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {"KELPIE_CONFIG_HOME": str(self.root / "config")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.repo = Path(__file__).resolve().parents[1]
        self.runner = WorkflowRunner(
            repo_root=self.repo, workdir=self.root, issue_number=None,
            runner_config=RunnerConfig(name="codex", command_template=["unused"]),
            instruction_staging_config=InstructionStagingConfig(),
            issue_source="none", task_label="optional-check",
        )
        self.artifacts = self.runner.artifact_dir
        self.tasks = {"tasks": [{"id": "fix", "title": "Fix", "description": "Existing requirement"}]}
        self.original = {
            "04-solution-design.md": b"# Existing design\n",
            "05-work-breakdown.md": json.dumps(self.tasks).encode(),
            "work_items.json": json.dumps(self.tasks).encode(),
        }
        for name, content in self.original.items():
            (self.artifacts / name).write_bytes(content)
        (self.artifacts / "05a-plan-comprehension-check.md").write_text("Probe evidence.\n")
        self.iteration = self.artifacts / "plan-check" / "iterations" / "0001"
        self.iteration.mkdir(parents=True)

    def adjudicate(self, findings=(), modified=(), unresolved=()):
        (self.iteration / "adjudication.json").write_text(json.dumps({
            "schema_version": "1.0", "input_snapshot_id": "snapshot",
            "findings": list(findings), "plan_modified": bool(modified),
            "modified_artifacts": list(modified), "unresolved_reasons": list(unresolved),
        }))

    @staticmethod
    def finding(identifier, verdict):
        return {
            "finding_id": identifier, "verdict": verdict,
            "evidence_refs": ["05-work-breakdown.md"], "rationale": "Compared with the plan.",
            "action": {"accepted": "plan_modified", "unresolved": "record_only"}[verdict],
        }

    def run_check(self, invoke, *, ids=(), probe_status=None, max_iterations=1):
        probe_result = {
            "status": probe_status or ("findings_recorded" if ids else "completed_no_findings"),
            "snapshot_id": "snapshot", "findings": [{"finding_id": identifier} for identifier in ids],
        }
        with patch("scripts.run_issue_workflow.run_plan_check", return_value=probe_result) as probe, patch.object(
            self.runner, "invoke_cli", side_effect=invoke,
        ) as strong:
            result = self.runner.run_plan_refinement_loop(
                artifact_dir=self.artifacts, probe_runner=self.runner.runner_config,
                max_iterations=max_iterations,
            )
        outcome = self.runner.record_plan_refinement_outcome(self.artifacts, result)
        self.assertEqual(outcome.decision, "advance")
        self.assertIsNone(outcome.resume_condition)
        self.assertFalse((self.artifacts / "human-interventions").exists())
        self.assertFalse((self.artifacts / "plan-check-external-send-approval.json").exists())
        self.assertNotIn("plan_check_policy", self.runner.read_workflow_state())
        return result, probe.call_count, strong.call_count

    def assert_restored(self):
        for name, content in self.original.items():
            self.assertEqual((self.artifacts / name).read_bytes(), content)

    def test_probe_failures_are_unavailable_without_strong_model_or_intervention(self):
        for status in ("execution_error", "invalid_output", "spec_error", "blocked_sensitive_input", "stale_input"):
            with self.subTest(status=status):
                result, _, calls = self.run_check(lambda *args: self.fail("Unexpected strong call"), probe_status=status)
                self.assertEqual(result["status"], status)
                self.assertEqual(calls, 0)
                self.assertEqual(self.runner.read_workflow_state()["reason_code"], "advisory_check_unavailable")

    def test_missing_probe_executable_is_recorded_as_unavailable(self):
        result = run_plan_check(self.artifacts, command_template=[str(self.root / "missing-runner")])
        outcome = self.runner.record_plan_refinement_outcome(self.artifacts, result)
        self.assertEqual(result["status"], "execution_error")
        self.assertEqual(outcome.decision, "advance")
        self.assertEqual(outcome.reason_code, "advisory_check_unavailable")
        report = (self.artifacts / "05a-plan-comprehension-check.md").read_text()
        self.assertIn("did not complete", report)
        self.assertNotIn("No source-backed", report)
        self.assertFalse((self.artifacts / "human-interventions").exists())

    def test_strong_runner_failure_restores_plan_and_advances(self):
        for failure in (SystemExit("runner failed"), OSError("runner missing")):
            with self.subTest(failure=type(failure).__name__):
                def invoke(*args):
                    for name in self.original:
                        (self.artifacts / name).write_text("partial update")
                    raise failure
                result, _, _ = self.run_check(invoke)
                self.assertEqual(result["status"], "execution_error")
                self.assert_restored()

    def test_missing_invalid_and_mismatched_adjudication_restore_plan(self):
        for mode in ("missing", "invalid", "mismatch"):
            with self.subTest(mode=mode):
                (self.iteration / "adjudication.json").unlink(missing_ok=True)
                def invoke(*args):
                    (self.artifacts / "04-solution-design.md").write_text("partial update")
                    if mode == "invalid":
                        (self.iteration / "adjudication.json").write_text("not JSON")
                    elif mode == "mismatch":
                        self.adjudicate()
                result, _, _ = self.run_check(invoke)
                self.assertEqual(result["status"], "invalid_output")
                self.assert_restored()

    def test_unresolved_finding_only_records_notes(self):
        def invoke(*args):
            self.adjudicate([self.finding("remaining", "unresolved")], unresolved=["Not explained in the supplied plan."])
        result, _, _ = self.run_check(invoke, ids=["remaining"])
        self.assertEqual(result["status"], "completed_with_notes")
        self.assertEqual(result["unresolved_finding_count"], 1)
        self.assert_restored()

    def test_accepted_and_unresolved_findings_regenerate_handoff_before_advancing(self):
        updated = {"tasks": [{**self.tasks["tasks"][0], "description": "Existing requirement, clarified"}]}
        def invoke(*args):
            (self.artifacts / "05-work-breakdown.md").write_text(json.dumps(updated))
            self.adjudicate(
                [self.finding("clarify", "accepted"), self.finding("remaining", "unresolved")],
                modified=["05-work-breakdown.md"],
            )
        result, _, _ = self.run_check(invoke, ids=["clarify", "remaining"])
        self.assertEqual(result["status"], "completed_with_notes")
        self.assertEqual(json.loads((self.artifacts / "work_items.json").read_text()), updated)

    def test_invalid_handoff_restores_all_plan_files_without_error_artifact_side_effects(self):
        error_path = self.runner.work_items_error_path()
        error_path.write_text("pre-existing diagnostic")
        def invoke(*args):
            (self.artifacts / "05-work-breakdown.md").write_text("No task JSON")
            self.adjudicate([self.finding("clarify", "accepted")], modified=["05-work-breakdown.md"])
        result, _, _ = self.run_check(invoke, ids=["clarify"])
        self.assertEqual(result["status"], "invalid_output")
        self.assert_restored()
        self.assertEqual(error_path.read_text(), "pre-existing diagnostic")

    def test_iteration_limit_keeps_valid_clarification_and_records_notes(self):
        def invoke(*args):
            (self.artifacts / "04-solution-design.md").write_text("Existing design, clarified")
            self.adjudicate([self.finding("clarify", "accepted")], modified=["04-solution-design.md"])
        result, calls, _ = self.run_check(invoke, ids=["clarify"], max_iterations=1)
        self.assertEqual(result["status"], "completed_with_notes")
        self.assertEqual(calls, 1)
        self.assertEqual((self.artifacts / "04-solution-design.md").read_text(), "Existing design, clarified")

    def test_scope_violation_fails_even_when_runner_also_fails(self):
        for runner_fails in (False, True):
            with self.subTest(runner_fails=runner_fails):
                (self.root / "outside.py").unlink(missing_ok=True)
                def invoke(*args):
                    (self.root / "outside.py").write_text("unexpected change")
                    (self.artifacts / "04-solution-design.md").write_text("partial update")
                    if runner_fails:
                        raise SystemExit("runner failure")
                    self.adjudicate()
                with self.assertRaisesRegex(SystemExit, "outside the planning artifact allowlist"):
                    self.run_check(invoke)
                self.assert_restored()

    def test_unexpected_programming_error_is_restored_and_reraised(self):
        def invoke(*args):
            (self.artifacts / "04-solution-design.md").write_text("partial update")
            raise TypeError("unexpected implementation bug")
        with self.assertRaisesRegex(TypeError, "unexpected implementation bug"):
            self.run_check(invoke)
        self.assert_restored()

    def test_removed_cli_flags_are_rejected(self):
        for flag in ("--allow-plan-check-external-send", "--require-plan-comprehension-check", "--waive-plan-comprehension-check"):
            with self.subTest(flag=flag), patch.object(sys, "argv", ["kelpie", "--workdir", str(self.root), flag]), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    parse_args()
                self.assertEqual(error.exception.code, 2)

    def test_legacy_default_skips_check_and_explicit_selection_or_old_resume_runs_it(self):
        cases = [
            ([], [phase for phase in PHASES if phase != "plan_comprehension_check"]),
            (["--from-phase", "plan_comprehension_check", "--to-phase", "plan_comprehension_check"], ["plan_comprehension_check"]),
            (["--resume"], PHASES[PHASES.index("plan_comprehension_check"):]),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                fake_runner = Mock()
                fake_runner.read_workflow_state.return_value = {
                    "status": "paused", "phase": "plan_comprehension_check", "plan_check_policy": "required",
                    "reason_code": "invalid_output",
                }
                argv = ["kelpie", "--repo-root", str(self.repo), "--workdir", str(self.root), "--runner", "codex", "--legacy-workflow", *extra]
                with patch.object(sys, "argv", argv), patch("scripts.run_issue_workflow.WorkflowRunner", return_value=fake_runner):
                    main()
                fake_runner.run.assert_called_once_with(expected)
                self.assertNotIn("plan_check_required", vars(fake_runner))
