from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_issue_workflow import (
    MAX_CANONICAL_REVIEW_FINDINGS_BYTES,
    MAX_REVIEW_FINDING_DESCRIPTION_BYTES,
    MAX_REVIEW_FINDING_ID_BYTES,
    MAX_REVIEW_FINDINGS,
    MAX_REVIEW_RESULT_BYTES,
    IMPLEMENTATION_LOOP_STATUS_SCHEMA_VERSION,
    ImplementationStepFactory,
    ImplementationSafetyLimitReached,
    IMPLEMENTATION_STEP_TO_PROMPT,
    IMPLEMENTATION_STEP_TO_SKILL,
    InstructionStagingConfig,
    ReviewFinding,
    REVIEW_RESULT_FILENAME,
    ReviewResultLoader,
    ReviewResultValidationError,
    RunnerConfig,
    RunnerNotFoundError,
    StepSpec,
    WorkflowRunner,
)


class ImplementationItemLoopTests(unittest.TestCase):
    repo_root = Path(__file__).resolve().parents[1]

    def make_runner(self, tmpdir: str, *, dry_run: bool = False) -> WorkflowRunner:
        workdir = Path(tmpdir) / "target-repo"
        workdir.mkdir()
        old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
        os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
        try:
            return WorkflowRunner(
                repo_root=self.repo_root,
                workdir=workdir,
                issue_number=None,
                runner_config=RunnerConfig(name="codex", command_template=["true"]),
                instruction_staging_config=InstructionStagingConfig(),
                issue_source="none",
                task_label="implementation-loop",
                dry_run=dry_run,
            )
        finally:
            if old_config_home is None:
                os.environ.pop("KELPIE_CONFIG_HOME", None)
            else:
                os.environ["KELPIE_CONFIG_HOME"] = old_config_home

    @staticmethod
    def write_work_items(runner: WorkflowRunner, tasks: list[dict[str, object]]) -> None:
        runner.work_items_json_path().write_text(
            json.dumps({"version": "1.0", "tasks": tasks}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def task(item_id: str, description: str = "Description") -> dict[str, object]:
        return {
            "id": item_id,
            "title": f"Title {item_id}",
            "description": description,
            "dependencies": [],
            "files": ["scripts/run_issue_workflow.py"],
            "acceptance_criteria": ["It works"],
        }

    @staticmethod
    def write_review_result(
        runner: WorkflowRunner,
        step: StepSpec,
        payload: dict[str, object],
    ) -> None:
        scope = runner.resolve_artifact_scope(step)
        scope.mkdir(parents=True, exist_ok=True)
        (scope / REVIEW_RESULT_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def scripted_subpipeline(
        self,
        runner: WorkflowRunner,
        review_payloads: list[dict[str, object]],
    ) -> tuple[list[tuple[StepSpec, dict[str, str] | None]], object]:
        calls: list[tuple[StepSpec, dict[str, str] | None]] = []
        review_index = 0

        def fake_run_step(
            step: StepSpec,
            *,
            virtual_context: dict[str, str] | None = None,
        ) -> None:
            nonlocal review_index
            calls.append((step, None if virtual_context is None else dict(virtual_context)))
            if step.name == "implementation_reviewer":
                self.write_review_result(runner, step, review_payloads[review_index])
                review_index += 1

        return calls, fake_run_step

    def test_status_v2_preserves_snapshot_fields_and_initial_progress_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            tasks = [self.task("wi-1"), self.task("wi-2")]
            self.write_work_items(runner, tasks)
            snapshot = runner.load_implementation_items_snapshot()

            status = runner.build_implementation_loop_status(snapshot, run_id="run-status")

        self.assertEqual(status["schema_version"], IMPLEMENTATION_LOOP_STATUS_SCHEMA_VERSION)
        self.assertEqual(status["schema_version"], "2.0")
        self.assertEqual(status["run_id"], "run-status")
        self.assertEqual(status["order"], ["wi-1", "wi-2"])
        self.assertEqual(status["source"]["item_count"], 2)
        for item, snapshot_item in zip(status["items"], snapshot.items):
            self.assertEqual(item["id"], snapshot_item.id)
            self.assertEqual(item["position"], snapshot_item.position)
            self.assertEqual(item["payload_sha256"], snapshot_item.payload_sha256)
            self.assertEqual(item["artifact_scope"], f"work-items/{snapshot_item.id}")
            self.assertEqual(item["status"], "not_run")
            self.assertEqual(
                {field: item[field] for field in (
                    "reason",
                    "current_role",
                    "current_iteration",
                    "attempt_id",
                    "last_review_scope",
                    "error",
                )},
                {
                    "reason": None,
                    "current_role": None,
                    "current_iteration": None,
                    "attempt_id": None,
                    "last_review_scope": None,
                    "error": None,
                },
            )

    def test_status_tracks_role_attempts_and_terminal_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])
            snapshot = runner.load_implementation_items_snapshot()
            status = runner.build_implementation_loop_status(snapshot, run_id="run-status")

            runner.transition_implementation_loop_item(
                status,
                0,
                "running",
                role="coder",
                iteration=0,
            )
            item = status["items"][0]
            self.assertEqual(item["status"], "running")
            self.assertEqual(item["current_role"], "coder")
            self.assertEqual(item["current_iteration"], 0)
            self.assertEqual(item["attempt_id"], "run-status:wi-1:0000:coder")

            runner.transition_implementation_loop_item(
                status,
                0,
                "running",
                role="reviewer",
                iteration=0,
                last_review_scope="work-items/wi-1/iterations/0000/reviewer",
            )
            self.assertEqual(item["current_role"], "reviewer")
            self.assertEqual(item["attempt_id"], "run-status:wi-1:0000:reviewer")
            self.assertEqual(
                item["last_review_scope"],
                "work-items/wi-1/iterations/0000/reviewer",
            )

            runner.transition_implementation_loop_item(
                status,
                0,
                "failed",
                reason="safety_limit_reached",
                role="reviewer",
                iteration=1,
                last_review_scope="work-items/wi-1/iterations/0001/reviewer",
            )
            self.assertEqual(item["status"], "failed")
            self.assertEqual(item["reason"], "safety_limit_reached")
            self.assertIsNone(item["current_role"])
            self.assertEqual(item["current_iteration"], 1)
            self.assertEqual(item["attempt_id"], "run-status:wi-1:0001:reviewer")
            self.assertEqual(
                item["last_review_scope"],
                "work-items/wi-1/iterations/0001/reviewer",
            )
            self.assertIsNone(item["error"])

    def test_status_terminal_reason_mapping_is_fail_closed(self) -> None:
        terminal_cases = (
            ("succeeded", "no_findings", None),
            ("succeeded", "fixed", None),
            ("failed", "execution_failed", {"type": "RuntimeError", "message": "boom"}),
            ("failed", "invalid_review_output", {"type": "ValueError", "message": "bad result"}),
            ("failed", "safety_limit_reached", None),
            ("planned", "dry_run", None),
        )
        for new_status, reason, error in terminal_cases:
            with self.subTest(status=new_status, reason=reason), tempfile.TemporaryDirectory() as tmpdir:
                runner = self.make_runner(tmpdir, dry_run=new_status == "planned")
                self.write_work_items(runner, [self.task("wi-1")])
                snapshot = runner.load_implementation_items_snapshot()
                status = runner.build_implementation_loop_status(snapshot, run_id="run-status")
                runner.transition_implementation_loop_item(
                    status,
                    0,
                    "running",
                    role="reviewer",
                    iteration=0,
                )
                runner.transition_implementation_loop_item(
                    status,
                    0,
                    new_status,
                    reason=reason,
                    error=error,
                    role="reviewer",
                    iteration=0,
                )
                item = status["items"][0]
                self.assertEqual(item["status"], new_status)
                self.assertEqual(item["reason"], reason)
                self.assertEqual(item["error"], error)

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])
            snapshot = runner.load_implementation_items_snapshot()
            status = runner.build_implementation_loop_status(snapshot, run_id="run-status")
            runner.transition_implementation_loop_item(
                status,
                0,
                "running",
                role="reviewer",
                iteration=0,
            )
            with self.assertRaisesRegex(ValueError, "terminal reason"):
                runner.transition_implementation_loop_item(
                    status,
                    0,
                    "failed",
                    reason="no_findings",
                    error={"type": "ValueError", "message": "bad"},
                )

    def test_factory_builds_four_independent_role_specs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            snapshot = runner.load_implementation_items_snapshot()

            resolved = runner.preflight_implementation_item_subpipelines(snapshot)

            self.assertEqual(len(resolved), 2)
            self.assertTrue(all(len(item_steps) == 4 for item_steps in resolved))
            for item, item_steps in zip(snapshot.items, resolved):
                self.assertEqual(
                    [step.spec.name for step in item_steps],
                    [
                        "implementation_coder",
                        "implementation_reviewer",
                        "implementation_fix",
                        "implementation_reviewer",
                    ],
                )
                self.assertEqual(
                    [step.spec.artifact_subdir for step in item_steps],
                    [
                        f"{item.id}/iterations/0000/coder",
                        f"{item.id}/iterations/0000/reviewer",
                        f"{item.id}/iterations/0001/fix",
                        f"{item.id}/iterations/0001/reviewer",
                    ],
                )
                self.assertEqual(
                    [step.spec.prompt_file for step in item_steps],
                    [
                        IMPLEMENTATION_STEP_TO_PROMPT["implementation_coder"],
                        IMPLEMENTATION_STEP_TO_PROMPT["implementation_reviewer"],
                        IMPLEMENTATION_STEP_TO_PROMPT["implementation_fix"],
                        IMPLEMENTATION_STEP_TO_PROMPT["implementation_reviewer"],
                    ],
                )
                self.assertEqual(
                    [step.spec.skill_file for step in item_steps],
                    [
                        IMPLEMENTATION_STEP_TO_SKILL["implementation_coder"],
                        IMPLEMENTATION_STEP_TO_SKILL["implementation_reviewer"],
                        IMPLEMENTATION_STEP_TO_SKILL["implementation_fix"],
                        IMPLEMENTATION_STEP_TO_SKILL["implementation_reviewer"],
                    ],
                )
                self.assertEqual(
                    [step.spec.inputs for step in item_steps],
                    [["$loop_item"], ["$loop_item"], ["$loop_item", "$review_findings"], ["$loop_item"]],
                )
                self.assertEqual(
                    [step.spec.outputs for step in item_steps],
                    [
                        ["06-implementation-notes.md"],
                        ["review-result.json"],
                        ["06-implementation-notes.md"],
                        ["review-result.json"],
                    ],
                )
                self.assertEqual([step.spec.context_id for step in item_steps], ["work-items"] * 4)

            scopes = [step.artifact_dir for item_steps in resolved for step in item_steps]
            self.assertEqual(len(scopes), len(set(scopes)))
            self.assertFalse(runner.implementation_loop_status_path().exists())
            self.assertFalse((runner.artifact_dir / "work-items").exists())

    def test_factory_keeps_role_configuration_independent_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])
            snapshot = runner.load_implementation_items_snapshot()
            factory = ImplementationStepFactory(
                runner_names={"coder": "coder-runner", "reviewer": "reviewer-runner", "fixer": "fix-runner"},
            )

            coder = factory.coder(snapshot.items[0])
            reviewer = factory.reviewer(snapshot.items[0], 0)
            fixer = factory.fix(snapshot.items[0], 1)

        self.assertEqual(coder.runner_name, "coder-runner")
        self.assertEqual(reviewer.runner_name, "reviewer-runner")
        self.assertEqual(fixer.runner_name, "fix-runner")
        self.assertIsNot(coder.inputs, reviewer.inputs)
        self.assertIsNot(coder.outputs, fixer.outputs)
        self.assertEqual(factory.runner_names["implementation_coder"], "coder-runner")
        with self.assertRaises(TypeError):
            factory.runner_names["implementation_coder"] = "mutated"  # type: ignore[index]

    def test_role_preflight_failure_has_no_lifecycle_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            runner.implementation_step_factory = ImplementationStepFactory(
                runner_names={"reviewer": "missing-runner"},
            )

            with patch.object(runner, "run_step") as run_step:
                with self.assertRaises(RunnerNotFoundError):
                    runner.implementation()

            run_step.assert_not_called()
            self.assertFalse(runner.implementation_loop_status_path().exists())
            self.assertFalse((runner.artifact_dir / "work-items").exists())

    def test_preflight_supplies_only_a_placeholder_for_fix_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])
            snapshot = runner.load_implementation_items_snapshot()
            real_resolve = runner.step_resolver.resolve

            with patch.object(runner.step_resolver, "resolve", wraps=real_resolve) as resolve:
                runner.preflight_implementation_item_subpipelines(snapshot)

            self.assertEqual(resolve.call_count, 4)
            for call in resolve.call_args_list:
                step = call.args[0]
                context = call.kwargs["virtual_context"]
                self.assertEqual(context["$loop_item"], snapshot.items[0].canonical_json)
                if "$review_findings" in (step.inputs or []):
                    self.assertEqual(
                        context["$review_findings"],
                        '{"findings":[],"schema_version":"1.0"}',
                    )
                else:
                    self.assertNotIn("$review_findings", context)

    def test_valid_items_run_in_order_with_fixed_step_metadata_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            tasks = [self.task("wi-1"), self.task("wi-2"), self.task("wi-3")]
            self.write_work_items(runner, tasks)
            calls, fake_run_step = self.scripted_subpipeline(
                runner,
                [
                    {"schema_version": "1.0", "status": "no_findings", "findings": []}
                    for _ in tasks
                ],
            )

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                runner.implementation()

            self.assertEqual(
                [step.name for step, _ in calls],
                ["implementation_coder", "implementation_reviewer"] * 3,
            )
            self.assertEqual(
                [step.artifact_subdir for step, _ in calls],
                [
                    "wi-1/iterations/0000/coder",
                    "wi-1/iterations/0000/reviewer",
                    "wi-2/iterations/0000/coder",
                    "wi-2/iterations/0000/reviewer",
                    "wi-3/iterations/0000/coder",
                    "wi-3/iterations/0000/reviewer",
                ],
            )
            self.assertEqual(
                [step.context_id for step, _ in calls],
                ["work-items"] * 6,
            )
            self.assertEqual(
                [json.loads(context["$loop_item"]) for _, context in calls if context],
                [task for task in tasks for _ in range(2)],
            )

            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["schema_version"], "2.0")
            self.assertEqual(status["overall_status"], "succeeded")
            self.assertEqual(
                [item["status"] for item in status["items"]],
                ["succeeded", "succeeded", "succeeded"],
            )
            self.assertEqual(
                [item["reason"] for item in status["items"]],
                ["no_findings", "no_findings", "no_findings"],
            )
            self.assertEqual(
                [item["artifact_scope"] for item in status["items"]],
                ["work-items/wi-1", "work-items/wi-2", "work-items/wi-3"],
            )
            self.assertFalse(runner.implementation_loop_lock_path().exists())

    def test_no_findings_skips_fix_and_records_reviewer_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])
            calls, fake_run_step = self.scripted_subpipeline(
                runner,
                [{"schema_version": "1.0", "status": "no_findings", "findings": []}],
            )

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                runner.implementation()

            self.assertEqual(
                [step.name for step, _ in calls],
                ["implementation_coder", "implementation_reviewer"],
            )
            self.assertNotIn("implementation_fix", [step.name for step, _ in calls])
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            item = status["items"][0]
            self.assertEqual(status["overall_status"], "succeeded")
            self.assertEqual(item["status"], "succeeded")
            self.assertEqual(item["reason"], "no_findings")
            self.assertEqual(item["current_iteration"], 0)
            self.assertEqual(
                item["last_review_scope"],
                "work-items/wi-1/iterations/0000/reviewer",
            )

    def test_findings_flow_through_fix_and_re_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])
            first_review = {
                "findings": [{"description": "Fix the boundary", "id": "F-1"}],
                "schema_version": "1.0",
                "status": "findings_present",
            }
            calls, fake_run_step = self.scripted_subpipeline(
                runner,
                [
                    first_review,
                    {"schema_version": "1.0", "status": "no_findings", "findings": []},
                ],
            )

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                runner.implementation()

            self.assertEqual(
                [step.name for step, _ in calls],
                [
                    "implementation_coder",
                    "implementation_reviewer",
                    "implementation_fix",
                    "implementation_reviewer",
                ],
            )
            fix_context = calls[2][1]
            assert fix_context is not None
            expected_findings = json.dumps(
                {
                    "findings": [{"description": "Fix the boundary", "id": "F-1"}],
                    "schema_version": "1.0",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.assertEqual(fix_context["$review_findings"], expected_findings)
            self.assertEqual(json.loads(fix_context["$loop_item"])["id"], "wi-1")
            self.assertEqual(
                [step.artifact_subdir for step, _ in calls],
                [
                    "wi-1/iterations/0000/coder",
                    "wi-1/iterations/0000/reviewer",
                    "wi-1/iterations/0001/fix",
                    "wi-1/iterations/0001/reviewer",
                ],
            )
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            item = status["items"][0]
            self.assertEqual(status["overall_status"], "succeeded")
            self.assertEqual(item["status"], "succeeded")
            self.assertEqual(item["reason"], "fixed")
            self.assertEqual(item["current_iteration"], 1)
            self.assertEqual(
                item["last_review_scope"],
                "work-items/wi-1/iterations/0001/reviewer",
            )

    def test_invalid_review_output_is_distinct_from_step_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            calls: list[str] = []

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                _ = virtual_context
                calls.append(step.name)

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                with self.assertRaises(ReviewResultValidationError):
                    runner.implementation()

            self.assertEqual(calls, ["implementation_coder", "implementation_reviewer"])
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["overall_status"], "failed")
            self.assertEqual(
                [item["status"] for item in status["items"]],
                ["failed", "not_run"],
            )
            self.assertEqual(status["items"][0]["reason"], "invalid_review_output")
            self.assertEqual(status["items"][0]["current_role"], None)
            self.assertEqual(status["items"][0]["current_iteration"], 0)

    def test_missing_review_result_after_successful_step_is_loader_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])

            def fake_cli(command: list[str], **kwargs: object) -> SimpleNamespace:
                _ = command
                prompt = str(kwargs["input"])
                is_reviewer = "subpipeline の reviewer" in prompt
                role = "reviewer" if is_reviewer else "coder"
                step_name = "implementation_reviewer" if is_reviewer else "implementation_coder"
                scope = (
                    runner.artifact_dir
                    / "work-items"
                    / "wi-1"
                    / "iterations"
                    / "0000"
                    / role
                )
                scope.mkdir(parents=True, exist_ok=True)
                if not is_reviewer:
                    (scope / "06-implementation-notes.md").write_text(
                        "# Coder notes\n",
                        encoding="utf-8",
                    )
                (scope / f"{step_name}-phase-outcome.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "phase": "implementation",
                            "decision": "advance",
                            "reason_code": "implementation_ready_for_review",
                            "summary": "Step completed.",
                            "evidence_refs": [] if is_reviewer else ["06-implementation-notes.md"],
                            "resume_condition": None,
                            "artifact_digests": {},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("scripts.run_issue_workflow.subprocess.run", side_effect=fake_cli):
                with self.assertRaises(ReviewResultValidationError):
                    runner.implementation()

            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["items"][0]["reason"], "invalid_review_output")

    def test_reviewer_execution_failure_does_not_load_leftover_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])
            failure = SystemExit("review command failed")

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                _ = virtual_context
                if step.name == "implementation_reviewer":
                    self.write_review_result(
                        runner,
                        step,
                        {"schema_version": "1.0", "status": "no_findings", "findings": []},
                    )
                    raise failure

            with (
                patch.object(runner, "run_step", side_effect=fake_run_step),
                patch.object(runner.review_result_loader, "load") as load,
            ):
                with self.assertRaises(SystemExit) as raised:
                    runner.implementation()

            self.assertIs(raised.exception, failure)
            load.assert_not_called()
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["items"][0]["reason"], "execution_failed")
            self.assertEqual(status["items"][0]["error"]["type"], "SystemExit")

    def test_remaining_findings_hit_fixed_safety_limit_without_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            finding = {
                "schema_version": "1.0",
                "status": "findings_present",
                "findings": [{"id": "F-1", "description": "Still unresolved"}],
            }
            calls, fake_run_step = self.scripted_subpipeline(runner, [finding, finding])

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                with self.assertRaises(ImplementationSafetyLimitReached):
                    runner.implementation()

            self.assertEqual(
                [step.name for step, _ in calls],
                [
                    "implementation_coder",
                    "implementation_reviewer",
                    "implementation_fix",
                    "implementation_reviewer",
                ],
            )
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["overall_status"], "failed")
            self.assertEqual(
                [item["status"] for item in status["items"]],
                ["failed", "not_run"],
            )
            self.assertEqual(status["items"][0]["reason"], "safety_limit_reached")
            self.assertIsNone(status["items"][0]["error"])
            self.assertEqual(status["items"][0]["current_iteration"], 1)

    def test_explicit_loop_context_is_complete_and_does_not_use_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir, dry_run=True)
            task = self.task("long-item", "x" * 2100)
            self.write_work_items(runner, [task])

            with patch.dict(os.environ, {"KELPIE_LOOP_ITEM": "wrong legacy value"}):
                runner.implementation()

            scope = runner.artifact_dir / "work-items" / "long-item" / "iterations" / "0000" / "coder"
            prompt = (scope / ".generated-prompts" / "implementation_coder.prompt.md").read_text(
                encoding="utf-8"
            )
            intent = json.loads(
                (scope / "intent-records" / "implementation_coder-intent-record.json").read_text(
                    encoding="utf-8"
                )
            )
            canonical = json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            self.assertIn(canonical, prompt)
            self.assertIn('truncated="false"', prompt)
            self.assertEqual(intent["inputs"][0]["original_length"], len(canonical))
            self.assertFalse(intent["inputs"][0]["truncated"])

            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["mode"], "dry-run")
            self.assertEqual(status["overall_status"], "planned")
            self.assertEqual(status["items"][0]["status"], "planned")

    def test_explicit_context_requires_supported_keys_and_loop_item_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir, dry_run=True)
            with self.assertRaisesRegex(ValueError, "Unsupported virtual context keys"):
                runner.resolve_step_inputs(
                    ["$loop_item"],
                    virtual_context={"$unsupported": "value"},
                )
            with self.assertRaisesRegex(ValueError, "no loop item context"):
                runner.resolve_step_inputs(["$loop_item"], virtual_context={})

            self.assertFalse((runner.artifact_dir / "work-items").exists())

    def test_dry_run_keeps_step_lifecycle_artifacts_in_each_item_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir, dry_run=True)
            self.write_work_items(runner, [self.task("wi-a"), self.task("wi-b")])

            with (
                patch.object(runner.review_result_loader, "prepare_target") as prepare_target,
                patch.object(runner.review_result_loader, "load") as load,
            ):
                runner.implementation()

            prepare_target.assert_not_called()
            load.assert_not_called()

            for item_id in ("wi-a", "wi-b"):
                for iteration, role, step_name in (
                    (0, "coder", "implementation_coder"),
                    (0, "reviewer", "implementation_reviewer"),
                    (1, "fix", "implementation_fix"),
                    (1, "reviewer", "implementation_reviewer"),
                ):
                    scope = (
                        runner.artifact_dir
                        / "work-items"
                        / item_id
                        / "iterations"
                        / f"{iteration:04d}"
                        / role
                    )
                    self.assertTrue(
                        (scope / ".generated-prompts" / f"{step_name}.prompt.md").is_file()
                    )
                    self.assertTrue(
                        (scope / "intent-records" / f"{step_name}-intent-record.json").is_file()
                    )
                    self.assertTrue((scope / "checks" / f"{step_name}-pre-check.txt").is_file())
                    self.assertTrue((scope / "checks" / f"{step_name}-post-check.txt").is_file())

            self.assertFalse(
                (runner.artifact_dir / ".generated-prompts" / "implementation_coder.prompt.md").exists()
            )
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["overall_status"], "planned")
            self.assertEqual([item["reason"] for item in status["items"]], ["dry_run", "dry_run"])

    def test_non_dry_run_uses_real_step_lifecycle_for_each_item_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-a"), self.task("wi-b")])
            invoked: list[tuple[str, str, int]] = []

            def fake_cli(command: list[str], **kwargs: object) -> SimpleNamespace:
                prompt = str(kwargs["input"])
                item_id = next(item_id for item_id in ("wi-a", "wi-b") if f'"id":"{item_id}"' in prompt)
                if "subpipeline の reviewer" in prompt:
                    role = "reviewer"
                    step_name = "implementation_reviewer"
                else:
                    role = "coder"
                    step_name = "implementation_coder"
                iteration = 1 if "/iterations/0001/" in prompt else 0
                invoked.append((item_id, role, iteration))
                scope = (
                    runner.artifact_dir
                    / "work-items"
                    / item_id
                    / "iterations"
                    / f"{iteration:04d}"
                    / role
                )
                scope.mkdir(parents=True, exist_ok=True)
                evidence_refs: list[str] = []
                if role != "reviewer":
                    (scope / "06-implementation-notes.md").write_text(
                        f"# {role} {item_id}\n",
                        encoding="utf-8",
                    )
                    evidence_refs = ["06-implementation-notes.md"]
                if role == "reviewer":
                    (scope / REVIEW_RESULT_FILENAME).write_text(
                        json.dumps(
                            {
                                "schema_version": "1.0",
                                "status": "no_findings",
                                "findings": [],
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                (scope / f"{step_name}-phase-outcome.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "phase": "implementation",
                            "decision": "advance",
                            "reason_code": "implementation_ready_for_review",
                            "summary": "Item completed.",
                            "evidence_refs": evidence_refs,
                            "resume_condition": None,
                            "artifact_digests": {},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("scripts.run_issue_workflow.subprocess.run", side_effect=fake_cli):
                runner.implementation()

            self.assertEqual(
                invoked,
                [
                    ("wi-a", "coder", 0),
                    ("wi-a", "reviewer", 0),
                    ("wi-b", "coder", 0),
                    ("wi-b", "reviewer", 0),
                ],
            )
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["overall_status"], "succeeded")
            for item_id, role, iteration in invoked:
                scope = runner.artifact_dir / "work-items" / item_id
                scope = scope / "iterations" / f"{iteration:04d}" / role
                step_name = "implementation_reviewer" if role == "reviewer" else "implementation_coder"
                self.assertTrue((scope / f"{step_name}-phase-outcome.json").is_file())
                self.assertTrue((scope / "phase-outcomes" / "implementation" / "0001.json").is_file())

    def test_each_role_failure_boundary_is_execution_failed_and_fail_fast(self) -> None:
        failure_targets = (
            ("coder", 0, "implementation_coder", [("implementation_coder", 0)]),
            (
                "initial reviewer",
                0,
                "implementation_reviewer",
                [("implementation_coder", 0), ("implementation_reviewer", 0)],
            ),
            (
                "fix",
                1,
                "implementation_fix",
                [
                    ("implementation_coder", 0),
                    ("implementation_reviewer", 0),
                    ("implementation_fix", 1),
                ],
            ),
            (
                "re-review",
                1,
                "implementation_reviewer",
                [
                    ("implementation_coder", 0),
                    ("implementation_reviewer", 0),
                    ("implementation_fix", 1),
                    ("implementation_reviewer", 1),
                ],
            ),
        )

        for failure_kind in ("cli", "phase outcome", "post-check"):
            for target_name, target_iteration, target_step_name, expected_calls in failure_targets:
                with self.subTest(failure=failure_kind, target=target_name), tempfile.TemporaryDirectory() as tmpdir:
                    runner = self.make_runner(tmpdir)
                    self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
                    calls: list[tuple[str, int]] = []
                    target = (target_step_name, target_iteration)

                    def write_lifecycle_outputs(resolved: object) -> None:
                        step = resolved.spec
                        scope = resolved.artifact_dir
                        relative = scope.relative_to(runner.artifact_dir)
                        role = relative.parts[-1]
                        iteration = int(relative.parts[-2])
                        if step.name in {"implementation_coder", "implementation_fix"}:
                            (scope / "06-implementation-notes.md").write_text(
                                f"# {role} notes\n",
                                encoding="utf-8",
                            )
                            evidence_refs = ["06-implementation-notes.md"]
                        else:
                            review_payload = {
                                "schema_version": "1.0",
                                "status": "findings_present" if iteration == 0 else "no_findings",
                                "findings": (
                                    [{"id": "F-001", "description": "Fix this"}]
                                    if iteration == 0
                                    else []
                                ),
                            }
                            (scope / REVIEW_RESULT_FILENAME).write_text(
                                json.dumps(review_payload) + "\n",
                                encoding="utf-8",
                            )
                            evidence_refs = []

                        outcome = {
                            "schema_version": "1.0",
                            "phase": "implementation",
                            "decision": "advance",
                            "reason_code": "implementation_ready_for_review",
                            "summary": "Step completed.",
                            "evidence_refs": evidence_refs,
                            "resume_condition": None,
                            "artifact_digests": {},
                        }
                        runner.phase_outcome_path(
                            "implementation",
                            scope,
                            step_name=step.name,
                        ).write_text(
                            json.dumps(outcome) + "\n",
                            encoding="utf-8",
                        )

                    def fake_cli(
                        phase: str,
                        prompt_text: str,
                        prompt_file: Path,
                        runner_config: RunnerConfig,
                    ) -> None:
                        _ = phase, prompt_text, runner_config
                        scope = prompt_file.parent.parent
                        step_name = prompt_file.name.removesuffix(".prompt.md")
                        relative = scope.relative_to(runner.artifact_dir)
                        call = (step_name, int(relative.parts[-2]))
                        calls.append(call)
                        if call == target and failure_kind == "cli":
                            raise SystemExit(f"CLI failure at {target_name}")

                        resolved = SimpleNamespace(
                            spec=SimpleNamespace(name=step_name),
                            artifact_dir=scope,
                        )
                        write_lifecycle_outputs(resolved)
                        if call == target and failure_kind == "phase outcome":
                            runner.phase_outcome_path(
                                "implementation",
                                scope,
                                step_name=step_name,
                            ).write_text("{}\n", encoding="utf-8")

                    real_post_checks = runner.run_post_checks

                    def post_checks(
                        phase: str,
                        artifact_dir: Path | None = None,
                        step_name: str | None = None,
                    ) -> None:
                        if artifact_dir is not None and step_name is not None:
                            relative = artifact_dir.relative_to(runner.artifact_dir)
                            call = (step_name, int(relative.parts[-2]))
                            if call == target and failure_kind == "post-check":
                                raise SystemExit(f"post-check failure at {target_name}")
                        real_post_checks(phase, artifact_dir=artifact_dir, step_name=step_name)

                    with (
                        patch.object(runner, "invoke_cli", side_effect=fake_cli),
                        patch.object(runner, "run_post_checks", side_effect=post_checks),
                    ):
                        with self.assertRaises(SystemExit):
                            runner.implementation()

                    self.assertEqual(calls, expected_calls)
                    status = json.loads(
                        runner.implementation_loop_status_path().read_text(encoding="utf-8")
                    )
                    self.assertEqual(status["overall_status"], "failed")
                    self.assertEqual(
                        [item["status"] for item in status["items"]],
                        ["failed", "not_run"],
                    )
                    failed_item = status["items"][0]
                    self.assertEqual(failed_item["reason"], "execution_failed")
                    self.assertIsNone(failed_item["current_role"])
                    self.assertEqual(failed_item["current_iteration"], target_iteration)

    def test_role_boundary_status_write_failure_stops_before_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            real_writer = runner.write_implementation_loop_status

            def fail_on_role_start(status: dict[str, object]) -> None:
                items = status["items"]
                assert isinstance(items, list)
                first_item = items[0]
                assert isinstance(first_item, dict)
                if first_item["current_role"] == "coder":
                    raise OSError("role boundary status write failed")
                real_writer(status)

            with (
                patch.object(runner, "write_implementation_loop_status", side_effect=fail_on_role_start),
                patch.object(runner, "run_step") as run_step,
            ):
                with self.assertRaisesRegex(OSError, "role boundary status write failed"):
                    runner.implementation()

            run_step.assert_not_called()
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["overall_status"], "running")
            self.assertEqual(
                [item["status"] for item in status["items"]],
                ["not_run", "not_run"],
            )

    def test_preflight_rejects_invalid_items_before_status_or_scope_creation(self) -> None:
        cases = [
            ("missing required field", {"id": "wi-1", "title": "Title"}, "Invalid implementation"),
            ("duplicate id", [self.task("same"), self.task("same")], "Duplicate"),
            ("unsafe id", [self.task("../escape")], "Invalid implementation work item id"),
        ]
        for name, raw_tasks, message in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmpdir:
                runner = self.make_runner(tmpdir)
                tasks = raw_tasks if isinstance(raw_tasks, list) else [raw_tasks]
                self.write_work_items(runner, tasks)
                with patch.object(runner, "run_step") as run_step:
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        runner.implementation()
                    run_step.assert_not_called()
                self.assertFalse(runner.implementation_loop_status_path().exists())
                self.assertFalse((runner.artifact_dir / "work-items").exists())

    def test_preflight_rejects_duplicate_and_stale_targets_without_calling_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            stale_scope = runner.artifact_dir / "work-items" / "wi-2"
            stale_scope.mkdir(parents=True)

            with patch.object(runner, "run_step") as run_step:
                with self.assertRaisesRegex(RuntimeError, "artifact scope already exists"):
                    runner.implementation()
                run_step.assert_not_called()

            self.assertFalse(runner.implementation_loop_status_path().exists())

    def test_item_failure_stops_loop_and_preserves_not_run_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2"), self.task("wi-3")])
            calls: list[str] = []
            failure = RuntimeError("item failed")

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                assert virtual_context is not None
                item_id = json.loads(virtual_context["$loop_item"])["id"]
                calls.append(f"{item_id}:{step.name}")
                if item_id == "wi-2":
                    raise failure
                if step.name == "implementation_reviewer":
                    self.write_review_result(
                        runner,
                        step,
                        {"schema_version": "1.0", "status": "no_findings", "findings": []},
                    )

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                with self.assertRaises(RuntimeError) as raised:
                    runner.implementation()

            self.assertIs(raised.exception, failure)
            self.assertEqual(
                calls,
                ["wi-1:implementation_coder", "wi-1:implementation_reviewer", "wi-2:implementation_coder"],
            )
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["overall_status"], "failed")
            self.assertEqual(
                [item["status"] for item in status["items"]],
                ["succeeded", "failed", "not_run"],
            )
            failed_item = status["items"][1]
            self.assertEqual(
                failed_item["error"],
                {"type": "RuntimeError", "message": "item failed"},
            )
            self.assertEqual(failed_item["reason"], "execution_failed")
            self.assertIsNone(failed_item["current_role"])
            self.assertEqual(failed_item["current_iteration"], 0)
            self.assertEqual(
                failed_item["attempt_id"],
                f"{status['run_id']}:wi-2:0000:coder",
            )

    def test_control_exception_is_recorded_then_reraised(self) -> None:
        for control_exception in (SystemExit("stop"), KeyboardInterrupt()):
            with self.subTest(exception=type(control_exception).__name__), tempfile.TemporaryDirectory() as tmpdir:
                runner = self.make_runner(tmpdir)
                self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])

                def fake_run_step(
                    step: StepSpec,
                    *,
                    virtual_context: dict[str, str] | None = None,
                ) -> None:
                    _ = step, virtual_context
                    raise control_exception

                with patch.object(runner, "run_step", side_effect=fake_run_step):
                    with self.assertRaises(type(control_exception)):
                        runner.implementation()

                status = json.loads(
                    runner.implementation_loop_status_path().read_text(encoding="utf-8")
                )
                self.assertEqual([item["status"] for item in status["items"]], ["failed", "not_run"])

    def test_status_recording_failure_does_not_replace_executor_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            primary = ValueError("executor failure")

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                _ = step, virtual_context
                raise primary

            real_writer = runner.write_implementation_loop_status

            def failing_writer(status: dict[str, object]) -> None:
                if status.get("overall_status") == "failed":
                    raise OSError("disk full")
                real_writer(status)

            with (
                patch.object(runner, "run_step", side_effect=fake_run_step),
                patch.object(runner, "write_implementation_loop_status", side_effect=failing_writer),
            ):
                with self.assertRaises(ValueError) as raised:
                    runner.implementation()

            self.assertIs(raised.exception, primary)
            self.assertTrue(any("status recording failed" in note for note in raised.exception.__notes__))

    def test_success_status_persistence_failure_stops_before_next_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            calls: list[str] = []

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                assert virtual_context is not None
                calls.append(json.loads(virtual_context["$loop_item"])["id"])
                if step.name == "implementation_reviewer":
                    self.write_review_result(
                        runner,
                        step,
                        {"schema_version": "1.0", "status": "no_findings", "findings": []},
                    )

            real_writer = runner.write_implementation_loop_status

            def failing_writer(status: dict[str, object]) -> None:
                items = status["items"]
                assert isinstance(items, list)
                if items[0]["status"] == "succeeded":
                    raise OSError("status write failed")
                real_writer(status)

            with (
                patch.object(runner, "run_step", side_effect=fake_run_step),
                patch.object(runner, "write_implementation_loop_status", side_effect=failing_writer),
            ):
                with self.assertRaisesRegex(OSError, "status write failed"):
                    runner.implementation()

            self.assertEqual(calls, ["wi-1", "wi-1"])

    def test_source_is_snapshotted_before_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            initial_tasks = [self.task("wi-1"), self.task("wi-2")]
            self.write_work_items(runner, initial_tasks)
            calls: list[str] = []

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                assert virtual_context is not None
                calls.append(json.loads(virtual_context["$loop_item"])["id"])
                if len(calls) == 1:
                    self.write_work_items(runner, [self.task("replacement")])
                if step.name == "implementation_reviewer":
                    self.write_review_result(
                        runner,
                        step,
                        {"schema_version": "1.0", "status": "no_findings", "findings": []},
                    )

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                runner.implementation()

            self.assertEqual(calls, ["wi-1", "wi-1", "wi-2", "wi-2"])
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["order"], ["wi-1", "wi-2"])

    def test_existing_loop_lock_is_rejected_without_status_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])
            runner.implementation_loop_lock_path().write_text("pid=existing\n", encoding="utf-8")

            with patch.object(runner, "run_step") as run_step:
                with self.assertRaisesRegex(RuntimeError, "already locked"):
                    runner.implementation()
                run_step.assert_not_called()

            self.assertTrue(runner.implementation_loop_lock_path().exists())
            self.assertFalse(runner.implementation_loop_status_path().exists())


class ReviewResultLoaderTests(unittest.TestCase):
    def make_loader(self, tmpdir: str) -> tuple[ReviewResultLoader, Path]:
        artifact_root = Path(tmpdir) / "artifacts"
        reviewer_scope = artifact_root / "work-items" / "wi-1" / "iterations" / "0000" / "reviewer"
        reviewer_scope.mkdir(parents=True)
        return ReviewResultLoader(artifact_root), reviewer_scope

    @staticmethod
    def write_result(expectation: object, payload: object) -> None:
        target = expectation.target
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load_payload(self, tmpdir: str, payload: object):
        loader, scope = self.make_loader(tmpdir)
        expectation = loader.prepare_target(scope)
        self.write_result(expectation, payload)
        return loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

    def assert_invalid_payload(self, payload: object) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            loader, scope = self.make_loader(tmpdir)
            expectation = loader.prepare_target(scope)
            self.write_result(expectation, payload)
            with self.assertRaises(ReviewResultValidationError):
                loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

    def test_valid_results_are_immutable_and_canonicalized(self) -> None:
        cases = [
            (
                {
                    "schema_version": "1.0",
                    "status": "no_findings",
                    "findings": [],
                },
                (),
                '{"findings":[],"schema_version":"1.0"}',
            ),
            (
                {
                    "findings": [
                        {"description": "修正してください", "id": "F-001"},
                        {"description": "second", "id": "F-002"},
                    ],
                    "schema_version": "1.0",
                    "status": "findings_present",
                },
                (
                    ReviewFinding(id="F-001", description="修正してください"),
                    ReviewFinding(id="F-002", description="second"),
                ),
                '{"findings":[{"description":"修正してください","id":"F-001"},{"description":"second","id":"F-002"}],"schema_version":"1.0"}',
            ),
        ]
        for payload, expected_findings, expected_canonical in cases:
            with self.subTest(status=payload["status"]), tempfile.TemporaryDirectory() as tmpdir:
                result = self.load_payload(tmpdir, payload)

            self.assertEqual(result.findings, expected_findings)
            self.assertIsInstance(result.findings, tuple)
            self.assertEqual(result.canonical_findings_json, expected_canonical)
            with self.assertRaises(AttributeError):
                result.findings += (ReviewFinding(id="F-003", description="not allowed"),)

    def test_prepare_requires_absent_fixed_target_and_load_rejects_missing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            loader, scope = self.make_loader(tmpdir)
            stale_target = scope / "review-result.json"
            stale_target.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ReviewResultValidationError, "absent"):
                loader.prepare_target(scope)

            stale_target.unlink()
            expectation = loader.prepare_target(scope)
            with self.assertRaisesRegex(ReviewResultValidationError, "missing"):
                loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

    def test_containment_and_symlink_boundaries_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            loader, scope = self.make_loader(tmpdir)
            outside = Path(tmpdir) / "outside"
            outside.mkdir()
            with self.assertRaises(ReviewResultValidationError):
                loader.prepare_target(outside)

            expectation = loader.prepare_target(scope)
            outside_result = outside / "result.json"
            outside_result.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "status": "no_findings",
                        "findings": [],
                    }
                ),
                encoding="utf-8",
            )
            expectation.target.symlink_to(outside_result)
            with self.assertRaises(ReviewResultValidationError):
                loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

            expectation.target.unlink()
            linked_scope = Path(tmpdir) / "linked-scope"
            linked_scope.symlink_to(scope, target_is_directory=True)
            with self.assertRaises(ReviewResultValidationError):
                loader.prepare_target(linked_scope)

    def test_directory_and_oversized_result_are_not_read_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            loader, scope = self.make_loader(tmpdir)
            expectation = loader.prepare_target(scope)
            expectation.target.mkdir()
            with self.assertRaisesRegex(ReviewResultValidationError, "regular file"):
                loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            loader, scope = self.make_loader(tmpdir)
            expectation = loader.prepare_target(scope)
            expectation.target.write_bytes(b"x" * (MAX_REVIEW_RESULT_BYTES + 1))
            with self.assertRaisesRegex(ReviewResultValidationError, "exceeds"):
                loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is not available on this platform")
    def test_special_file_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            loader, scope = self.make_loader(tmpdir)
            expectation = loader.prepare_target(scope)
            os.mkfifo(expectation.target)
            with self.assertRaisesRegex(ReviewResultValidationError, "regular file"):
                loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

    def test_malformed_and_inconsistent_results_are_invalid(self) -> None:
        invalid_payloads = [
            {"schema_version": "1.0", "status": "no_findings", "findings": [{"id": "F", "description": "x"}]},
            {"schema_version": "1.0", "status": "findings_present", "findings": []},
            {"schema_version": "1.0", "status": "unknown", "findings": []},
            {"schema_version": "1.0", "status": [], "findings": []},
            {"schema_version": "2.0", "status": "no_findings", "findings": []},
            {"schema_version": "1.0", "status": "no_findings", "findings": [], "extra": True},
            {"schema_version": "1.0", "status": "no_findings", "findings": {}},
            {"schema_version": "1.0", "status": "no_findings", "findings": [{"id": "F"}]},
            {"schema_version": "1.0", "status": "no_findings", "findings": ["not an object"]},
            {"schema_version": "1.0", "status": "no_findings", "findings": [{"id": "F", "description": "x", "extra": True}]},
            {"schema_version": "1.0", "status": "no_findings", "findings": [{"id": "", "description": "x"}]},
            {"schema_version": "1.0", "status": "no_findings", "findings": [{"id": "F", "description": ""}]},
            {"schema_version": "1.0", "status": "no_findings", "findings": [{"id": "F", "description": "x"}, {"id": "F", "description": "y"}]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assert_invalid_payload(payload)

        for raw_bytes in (
            b"\xff",
            b"{",
            b"[]",
            b'{"schema_version":"1.0","schema_version":"1.0","status":"no_findings","findings":[]}',
        ):
            with self.subTest(raw_bytes=raw_bytes), tempfile.TemporaryDirectory() as tmpdir:
                loader, scope = self.make_loader(tmpdir)
                expectation = loader.prepare_target(scope)
                expectation.target.write_bytes(raw_bytes)
                with self.assertRaises(ReviewResultValidationError):
                    loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

        deep_value: object = []
        for _ in range(33):
            deep_value = [deep_value]
        with tempfile.TemporaryDirectory() as tmpdir:
            loader, scope = self.make_loader(tmpdir)
            expectation = loader.prepare_target(scope)
            expectation.target.write_text(json.dumps(deep_value), encoding="utf-8")
            with self.assertRaises(ReviewResultValidationError):
                loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

    def test_all_review_limits_are_enforced_without_truncation(self) -> None:
        exact_payload = {
            "schema_version": "1.0",
            "status": "findings_present",
            "findings": [
                {
                    "id": "i" * MAX_REVIEW_FINDING_ID_BYTES,
                    "description": "d" * MAX_REVIEW_FINDING_DESCRIPTION_BYTES,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.load_payload(tmpdir, exact_payload)
            self.assertEqual(
                len(result.findings[0].id.encode("utf-8")),
                MAX_REVIEW_FINDING_ID_BYTES,
            )
            self.assertEqual(
                len(result.findings[0].description.encode("utf-8")),
                MAX_REVIEW_FINDING_DESCRIPTION_BYTES,
            )

        for field, value in (
            ("id", "i" * (MAX_REVIEW_FINDING_ID_BYTES + 1)),
            ("description", "d" * (MAX_REVIEW_FINDING_DESCRIPTION_BYTES + 1)),
        ):
            with self.subTest(field=field):
                payload = {
                    "schema_version": "1.0",
                    "status": "findings_present",
                    "findings": [{"id": "F-1", "description": "valid"}],
                }
                payload["findings"][0][field] = value
                self.assert_invalid_payload(payload)

        with tempfile.TemporaryDirectory() as tmpdir:
            loader, scope = self.make_loader(tmpdir)
            expectation = loader.prepare_target(scope)
            too_many = {
                "schema_version": "1.0",
                "status": "findings_present",
                "findings": [
                    {"id": f"F-{index}", "description": "x"}
                    for index in range(MAX_REVIEW_FINDINGS + 1)
                ],
            }
            self.write_result(expectation, too_many)
            with self.assertRaises(ReviewResultValidationError):
                loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            loader, scope = self.make_loader(tmpdir)
            expectation = loader.prepare_target(scope)
            canonical_too_large = {
                "schema_version": "1.0",
                "status": "findings_present",
                "findings": [
                    {"id": f"F-{index}", "description": "x" * MAX_REVIEW_FINDING_DESCRIPTION_BYTES}
                    for index in range(16)
                ],
            }
            self.write_result(expectation, canonical_too_large)
            self.assertLessEqual(expectation.target.stat().st_size, MAX_REVIEW_RESULT_BYTES)
            self.assertGreater(
                len(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "findings": canonical_too_large["findings"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
                MAX_CANONICAL_REVIEW_FINDINGS_BYTES,
            )
            with self.assertRaises(ReviewResultValidationError):
                loader.load(expectation, run_id="run-1", item_id="wi-1", iteration=0)


if __name__ == "__main__":
    unittest.main()
