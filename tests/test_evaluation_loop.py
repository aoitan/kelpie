from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.evaluation_loop import (
    EvaluationLoopRequest,
    EvaluationLoopRequestValidator,
    EvaluationLoopStore,
    EvidenceRef,
    ReviewProcessResult,
    derive_verdict,
    run_evaluation_loop,
)
from scripts.single_change import ActiveTarget, CheckSpec, SingleChangeRequest
from scripts.run_issue_workflow import (
    InstructionStagingConfig,
    RunnerConfig,
    StepSpec,
    WorkflowRunner,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _make_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "evaluation@example.com")
    _git(root, "config", "user.name", "Evaluation Tests")
    (root / "target.txt").write_text("base\n", encoding="utf-8")
    (root / "requirements.md").write_text("# acceptance\n\n- target changes\n", encoding="utf-8")
    (root / "plan.md").write_text("# plan\n\n- use one fixed loop\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")


def _request(
    *,
    checks: tuple[CheckSpec, ...] | None = None,
    refs: tuple[str, ...] = ("requirements.md",),
    reviewer: object = None,
) -> EvaluationLoopRequest:
    single_change = SingleChangeRequest(
        work_item_id="wi-10",
        active_targets=(
            ActiveTarget(
                kind="work_item",
                id="wi-10",
                source_ref="requirements.md#acceptance",
            ),
        ),
        change_intent="apply the smallest target change",
        allowed_paths=("target.txt",),
        checks=checks
        if checks is not None
        else (
            CheckSpec(
                argv=(sys.executable, "-c", "print('check passed')"),
                timeout_seconds=10,
            ),
        ),
    )
    return EvaluationLoopRequest(
        work_item_id="wi-10",
        single_change=single_change,
        requirement_refs=refs,
        reviewer=reviewer,
    )


def _run(
    root: Path,
    request: EvaluationLoopRequest,
    *,
    executor=None,
):
    return run_evaluation_loop(
        request,
        workdir=root,
        artifact_root=root / ".kelpie" / "artifacts",
        executor=executor or (lambda _request, _scope: (root / "target.txt").write_text("changed\n", encoding="utf-8")),
    )


class EvaluationLoopContractTests(unittest.TestCase):
    def test_happy_path_persists_all_channels_and_summary_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)

            def reviewer(invocation):
                self.assertNotIn("completion_claim", json.dumps(invocation.input_manifest))
                return {"schema_version": "1.0", "findings": []}

            result = _run(root, _request(reviewer=reviewer))

            self.assertEqual(result.verdict, "satisfied")
            self.assertEqual(result.loop_id, "0001")
            expected = {
                "manifest.json",
                "implementation.json",
                "verify/execution.json",
                "review/input.json",
                "review/execution.json",
                "review/raw-output.bin",
                "review/raw-output.json",
                "review/validation.json",
                "review/validated.json",
                "result.json",
                "summary.md",
                "finalized",
            }
            actual = {
                path.relative_to(result.loop_dir).as_posix()
                for path in result.loop_dir.rglob("*")
                if path.is_file()
            }
            self.assertTrue(expected <= actual)
            machine = json.loads((result.loop_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(machine["verdict"], "satisfied")
            self.assertEqual(machine["findings"], [])
            self.assertEqual(machine["observations"]["review"]["state"], "valid")
            self.assertIn("Verdict: `satisfied`", (result.loop_dir / "summary.md").read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads((result.loop_dir / "lifecycle.json").read_text(encoding="utf-8"))["state"],
                "finalized",
            )

    def test_check_failure_is_execution_failed_and_reviewer_is_not_called(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)
            called = False

            def reviewer(_invocation):
                nonlocal called
                called = True
                return {"schema_version": "1.0", "findings": []}

            request = _request(
                reviewer=reviewer,
                checks=(
                    CheckSpec(
                        argv=(sys.executable, "-c", "import sys; sys.exit(3)"),
                        timeout_seconds=10,
                    ),
                ),
            )
            result = _run(root, request)

            self.assertEqual(result.verdict, "execution_failed")
            self.assertEqual(result.result["decision_reason"], "targeted_check_failed")
            self.assertEqual(result.result["observations"]["implement"]["state"], "succeeded")
            self.assertEqual(result.result["observations"]["verify"]["state"], "failed")
            self.assertFalse(called)
            execution = json.loads((result.loop_dir / "review" / "execution.json").read_text())
            validation = json.loads((result.loop_dir / "review" / "validation.json").read_text())
            self.assertEqual(execution["state"], "not_started_due_to_dependency")
            self.assertEqual(validation["state"], "not_started_due_to_dependency")
            self.assertEqual(json.loads((result.loop_dir / "result.json").read_text())["findings"], [])

    def test_empty_and_schema_invalid_review_are_invalid_output(self) -> None:
        for review in ("", {"schema_version": "1.0"}):
            with self.subTest(review=review), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir) / "repo"
                _make_repo(root)
                result = _run(root, _request(reviewer=lambda _invocation, value=review: value))
                self.assertEqual(result.verdict, "invalid_output")
                validation = json.loads((result.loop_dir / "review" / "validation.json").read_text())
                self.assertIn(validation["state"], {"empty", "schema_invalid"})
                self.assertFalse((result.loop_dir / "review" / "validated.json").exists())

    def test_review_process_failure_is_execution_failed_not_invalid_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)
            result = _run(
                root,
                _request(reviewer=lambda _invocation: ReviewProcessResult.failure("reviewer unavailable")),
            )
            self.assertEqual(result.verdict, "execution_failed")
            execution = json.loads((result.loop_dir / "review" / "execution.json").read_text())
            self.assertEqual(execution["state"], "failed")
            self.assertEqual(json.loads((result.loop_dir / "review" / "validation.json").read_text())["state"], "execution_failed")

    def test_unsupported_reviewer_return_value_is_invalid_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)
            result = _run(root, _request(reviewer=lambda _invocation: object()))
            self.assertEqual(result.verdict, "invalid_output")
            self.assertEqual(
                json.loads((result.loop_dir / "review" / "validation.json").read_text())["state"],
                "schema_invalid",
            )

    def test_source_mutation_during_review_is_execution_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)

            def reviewer(_invocation):
                (root / "target.txt").write_text("reviewer mutation\n", encoding="utf-8")
                return {"schema_version": "1.0", "findings": []}

            result = _run(root, _request(reviewer=reviewer))
            self.assertEqual(result.verdict, "execution_failed")
            execution = json.loads((result.loop_dir / "review" / "execution.json").read_text())
            self.assertEqual(execution["reason_code"], "review_execution_failed")
            self.assertIn("source binding changed", execution["process_error"])

    def test_ordinary_finding_has_system_id_and_open_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)

            def reviewer(invocation):
                evidence = [
                    item.to_payload()
                    for item in invocation.evidence
                    if item.kind == "requirement" or item.kind == "diff"
                ]
                return {
                    "schema_version": "1.0",
                    "findings": [
                        {
                            "finding_key": "target-change-review",
                            "severity": "high",
                            "category": "implementation_defect",
                            "message": "The target change needs review.",
                            "evidence": evidence,
                        }
                    ],
                }

            result = _run(root, _request(reviewer=reviewer))
            self.assertEqual(result.verdict, "changes_requested")
            finding = result.result["findings"][0]
            self.assertRegex(finding["id"], r"^F-[0-9a-f]{32}$")
            self.assertEqual(finding["status"], "open")
            self.assertNotIn("id", json.loads((result.loop_dir / "review" / "raw-output.bin").read_text(encoding="utf-8")))
            summary = (result.loop_dir / "summary.md").read_text(encoding="utf-8")
            self.assertIn(finding["id"], summary)

    def test_plan_defect_primary_verdict_retains_ordinary_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)

            def reviewer(invocation):
                by_kind = {}
                for item in invocation.evidence:
                    by_kind.setdefault(item.kind, []).append(item.to_payload())
                normal_evidence = by_kind["requirement"] + by_kind["diff"]
                plan_evidence = by_kind["requirement"] + by_kind["plan"]
                return {
                    "schema_version": "1.0",
                    "findings": [
                        {
                            "finding_key": "implementation-gap",
                            "severity": "medium",
                            "category": "implementation_defect",
                            "message": "The implementation is incomplete.",
                            "evidence": normal_evidence,
                        },
                        {
                            "finding_key": "plan-gap",
                            "severity": "high",
                            "category": "plan_defect",
                            "message": "The plan omits a required branch.",
                            "evidence": plan_evidence,
                        },
                    ],
                }

            request = _request(
                reviewer=reviewer,
                refs=(EvidenceRef("requirement", "requirements.md"), EvidenceRef("plan", "plan.md")),
            )
            result = _run(root, request)
            self.assertEqual(result.verdict, "plan_defect")
            self.assertEqual(len(result.result["findings"]), 2)
            self.assertEqual(
                {item["category"] for item in result.result["findings"]},
                {"implementation_defect", "plan_defect"},
            )

    def test_reviewer_cannot_inject_id_status_or_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)

            def reviewer(invocation):
                evidence = [item.to_payload() for item in invocation.evidence if item.kind in {"requirement", "diff"}]
                return {
                    "schema_version": "1.0",
                    "findings": [
                        {
                            "id": "F-user-controlled",
                            "status": "resolved",
                            "verdict": "satisfied",
                            "finding_key": "bad-wire",
                            "severity": "high",
                            "category": "implementation_defect",
                            "message": "bad",
                            "evidence": evidence,
                        }
                    ],
                }

            result = _run(root, _request(reviewer=reviewer))
            self.assertEqual(result.verdict, "invalid_output")
            self.assertEqual(
                json.loads((result.loop_dir / "review" / "validation.json").read_text())["state"],
                "schema_invalid",
            )

    def test_finding_id_is_stable_when_message_severity_and_evidence_order_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)
            mode = {"second": False}

            def reviewer(invocation):
                evidence = [
                    item.to_payload()
                    for item in invocation.evidence
                    if item.kind in {"requirement", "diff"}
                ]
                if mode["second"]:
                    evidence.reverse()
                return {
                    "schema_version": "1.0",
                    "findings": [
                        {
                            "finding_key": "stable-key",
                            "severity": "low" if mode["second"] else "high",
                            "category": "implementation_defect",
                            "message": "different wording" if mode["second"] else "first wording",
                            "evidence": evidence,
                        }
                    ],
                }

            first = _run(root, _request(reviewer=reviewer), executor=lambda _request, _scope: None)
            first_id = first.result["findings"][0]["id"]
            mode["second"] = True
            second = _run(root, _request(reviewer=reviewer), executor=lambda _request, _scope: None)
            self.assertEqual(first_id, second.result["findings"][0]["id"])
            self.assertEqual(second.loop_id, "0002")

    def test_request_requires_requirements_and_targeted_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)
            reviewer = lambda _invocation: {"schema_version": "1.0", "findings": []}
            with self.assertRaisesRegex(ValueError, "at least one targeted check"):
                EvaluationLoopRequestValidator(root).validate(
                    _request(reviewer=reviewer, checks=())
                )
            with self.assertRaisesRegex(ValueError, "requirement"):
                EvaluationLoopRequestValidator(root).validate(
                    _request(reviewer=reviewer, refs=())
                )

    def test_store_rejects_reentry_and_monotonically_reserves_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts"
            store = EvaluationLoopStore(root, "wi-10")
            first, first_path = store.reserve()
            second, _second_path = store.reserve()
            self.assertEqual((first, second), ("0001", "0002"))
            with self.assertRaisesRegex(ValueError, "transition"):
                store.transition(first_path, "finalized")

    def test_decision_table_keeps_failure_channels_distinct(self) -> None:
        common = {"state": "succeeded"}
        self.assertEqual(
            derive_verdict(
                implementation=common,
                verify={"state": "failed", "reason_code": "targeted_check_failed"},
                review_execution=common,
                review_validation={"state": "valid"},
            ),
            ("execution_failed", "targeted_check_failed"),
        )
        self.assertEqual(
            derive_verdict(
                implementation=common,
                verify=common,
                review_execution=common,
                review_validation={"state": "empty"},
            ),
            ("invalid_output", "review_empty"),
        )

    def test_workflow_runner_exposes_one_shot_opt_in_entry_point(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_repo(root)
            runner = WorkflowRunner(
                repo_root=repo_root,
                workdir=root,
                issue_number=None,
                runner_config=RunnerConfig(name="codex", command_template=["true"]),
                instruction_staging_config=InstructionStagingConfig(),
                issue_source="none",
                task_label="evaluation-loop",
                dry_run=True,
            )
            request = _request(reviewer=lambda _invocation: {"schema_version": "1.0", "findings": []})
            with patch.object(runner, "run_step") as run_step:
                result = runner.run_evaluation_loop(request)

            self.assertEqual(result.verdict, "satisfied")
            run_step.assert_called_once()
            step = run_step.call_args.args[0]
            self.assertIsInstance(step, StepSpec)
            self.assertEqual(step.name, "evaluation-loop-implement")


if __name__ == "__main__":
    unittest.main()
