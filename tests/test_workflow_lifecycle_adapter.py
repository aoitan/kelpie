from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.pipeline_executor import (
    PipelineExecutor,
    StepCompletionEvent,
    StepExecutionRequest,
    UnsupportedLoopControllerError,
    prepare_workflow_run,
)
from scripts.run_issue_workflow import (
    ImplementationReviewController,
    InstructionStagingConfig,
    LegacyLifecycleBinding,
    ReviewFinding,
    ReviewResult,
    RunnerConfig,
    StepSpec,
    WorkflowRunner,
    WorkflowRunnerStepExecutionPort,
)
from scripts.workflow_config import parse_workflow_config


class WorkflowLifecycleAdapterTests(unittest.TestCase):
    repo_root = Path(__file__).resolve().parents[1]

    def make_runner(self, tmpdir: str) -> WorkflowRunner:
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
                task_label="lifecycle-adapter",
            )
        finally:
            if old_config_home is None:
                os.environ.pop("KELPIE_CONFIG_HOME", None)
            else:
                os.environ["KELPIE_CONFIG_HOME"] = old_config_home

    @staticmethod
    def request_for(prepared, *, node_index: int = 0) -> StepExecutionRequest:
        step = prepared.plan.nodes[node_index]
        return StepExecutionRequest(
            run_identity=prepared.identity.digest,
            node_instance_id=step.canonical_id,
            step=step,
            artifact_scope=prepared.artifact_root,
            loop_context=None,
            resolved_inputs=(),
            expected_outputs=(),
        )

    def prepare_single_step(
        self,
        root: Path,
        *,
        lifecycle: str = "kelpie.phase.implementation.v1",
        prompt: str = "prompts/06_implementation_coder.md",
        skill: str = "skills/implementation-coder/SKILL.md",
        inputs: list[dict[str, str]] | None = None,
        outputs: list[dict[str, str]] | None = None,
    ):
        payload = {
            "schema_version": "1.0",
            "id": "lifecycle-workflow",
            "profile": "repository_issue",
            "limits": {},
            "nodes": [
                {
                    "type": "step",
                    "id": "renamed-node",
                    "lifecycle": lifecycle,
                    "runner": "codex",
                    "prompt": prompt,
                    "skill": skill,
                    "inputs": [] if inputs is None else inputs,
                    "outputs": [] if outputs is None else outputs,
                    "depends_on": [],
                }
            ],
        }
        return prepare_workflow_run(
            parse_workflow_config(payload),
            repo_root=self.repo_root,
            artifact_root=root,
        )

    def test_adapter_keeps_node_id_separate_from_lifecycle_and_delegates_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            prepared = self.prepare_single_step(runner.artifact_dir)
            adapter = WorkflowRunnerStepExecutionPort(runner)
            request = self.request_for(prepared)
            spec, context, binding = adapter.build_step_spec(request)

            self.assertEqual(spec.name, "renamed-node")
            self.assertEqual(spec.phase, "implementation")
            self.assertEqual(spec.lifecycle, "kelpie.phase.implementation.v1")
            self.assertEqual(binding.role, None)
            self.assertEqual(context, {})

            calls: list[str] = []
            with (
                patch.object(
                    runner,
                    "write_intent_record_stub",
                    side_effect=lambda *args, **kwargs: calls.append("intent"),
                ),
                patch.object(
                    runner,
                    "run_pre_checks",
                    side_effect=lambda *args, **kwargs: calls.append("pre"),
                ),
                patch.object(
                    runner,
                    "invoke_cli",
                    side_effect=lambda *args, **kwargs: calls.append("execute"),
                ),
                patch.object(
                    runner,
                    "run_step_post_actions",
                    side_effect=lambda *args, **kwargs: calls.append("post-action"),
                ),
                patch.object(
                    runner,
                    "run_post_checks",
                    side_effect=lambda *args, **kwargs: calls.append("post-check"),
                ),
                patch.object(
                    runner,
                    "evaluate_phase_outcome",
                    side_effect=lambda *args, **kwargs: calls.append("outcome"),
                ),
            ):
                event = adapter.execute(request)

        self.assertTrue(event.succeeded)
        self.assertEqual(calls, ["intent", "pre", "execute", "post-action", "post-check", "outcome"])

    def test_adapter_translates_inputs_outputs_and_runner_fields_without_node_branching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            prepared = self.prepare_single_step(
                runner.artifact_dir,
                inputs=[{"name": "issue", "from": "$issue"}],
                outputs=[{"id": "result", "kind": "file", "path": "result.md"}],
            )
            adapter = WorkflowRunnerStepExecutionPort(runner)
            captured: list[tuple[StepSpec, dict[str, str]]] = []

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                captured.append((step, {} if virtual_context is None else dict(virtual_context)))

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                result = PipelineExecutor(
                    adapter,
                    virtual_inputs={"$issue": {"number": 20}},
                    validate_outputs=False,
                ).execute(prepared)

        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(len(captured), 1)
        spec, context = captured[0]
        self.assertEqual(spec.name, "renamed-node")
        self.assertEqual(spec.runner_name, "codex")
        self.assertEqual(spec.inputs, ["$issue"])
        self.assertEqual(spec.outputs, ["result.md"])
        self.assertEqual(context, {"$issue": '{"number":20}'})

    def test_reviewer_adapter_loads_only_the_fresh_typed_review_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            prepared = self.prepare_single_step(
                runner.artifact_dir,
                lifecycle="kelpie.phase.implementation_reviewer.v1",
                prompt="prompts/06_implementation_reviewer.md",
                skill="skills/implementation-reviewer/SKILL.md",
                outputs=[{"id": "review", "kind": "file", "path": "review-result.json"}],
            )
            request = self.request_for(prepared)
            adapter = WorkflowRunnerStepExecutionPort(runner)

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                _ = virtual_context
                scope = runner.resolve_artifact_scope(step)
                (scope / "review-result.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "status": "findings_present",
                            "findings": [{"id": "F-001", "description": "Repair this"}],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                event = adapter.execute(request)

        self.assertIsInstance(event.result, ReviewResult)
        self.assertEqual(event.result.status, "findings_present")
        self.assertEqual(event.result.findings[0], ReviewFinding("F-001", "Repair this"))

    def test_adapter_preserves_pause_outcome_as_a_pause_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            prepared = self.prepare_single_step(runner.artifact_dir)
            request = self.request_for(prepared)
            adapter = WorkflowRunnerStepExecutionPort(runner)
            outcome_path = runner.phase_outcome_path(
                "implementation",
                runner.artifact_dir,
                step_name="renamed-node",
            )
            outcome = {
                "schema_version": "1.0",
                "phase": "implementation",
                "decision": "pause",
                "reason_code": "required_tests_unresolved",
                "summary": "Tests need attention.",
                "evidence_refs": [],
                "resume_condition": "Run the required tests.",
                "artifact_digests": {},
            }

            def pause(step: StepSpec, *, virtual_context: dict[str, str]) -> None:
                _ = step, virtual_context
                outcome_path.parent.mkdir(parents=True, exist_ok=True)
                outcome_path.write_text(json.dumps(outcome) + "\n", encoding="utf-8")
                raise SystemExit("Workflow paused in phase 'implementation'")

            with patch.object(runner, "run_step", side_effect=pause):
                event = adapter.execute(request)

        self.assertFalse(event.succeeded)
        self.assertEqual(event.status, "paused")
        self.assertEqual(event.result.decision, "pause")

    def test_adapter_does_not_treat_a_stale_phase_outcome_as_runner_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            prepared = self.prepare_single_step(runner.artifact_dir)
            request = self.request_for(prepared)
            adapter = WorkflowRunnerStepExecutionPort(runner)
            outcome_path = runner.phase_outcome_path(
                "implementation",
                runner.artifact_dir,
                step_name="renamed-node",
            )
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text("{}\n", encoding="utf-8")
            failure = SystemExit("runner failed")

            with patch.object(runner, "run_step", side_effect=failure):
                with self.assertRaises(SystemExit) as raised:
                    adapter.execute(request)

        self.assertIs(raised.exception, failure)

    def test_custom_lifecycle_binding_can_preserve_role_without_node_name_branching(self) -> None:
        binding = LegacyLifecycleBinding(
            capability_id="kelpie.phase.custom_reviewer.v1",
            phase="implementation",
            runner_step_name="implementation_reviewer",
            role="reviewer",
        )
        self.assertEqual(binding.role, "implementation_reviewer")
        self.assertEqual(binding.lifecycle_kind, "kelpie.phase.custom_reviewer.v1")

    def test_registered_implementation_controller_drives_review_fix_transition(self) -> None:
        payload = {
            "schema_version": "1.0",
            "id": "implementation-workflow",
            "profile": "repository_issue",
            "limits": {"max_loop_items": 2, "max_total_steps": 10},
            "nodes": [
                {
                    "type": "step",
                    "id": "plan",
                    "lifecycle": "kelpie.phase.implementation.v1",
                    "runner": "codex",
                    "prompt": "prompts/06_implementation_coder.md",
                    "skill": "skills/implementation-coder/SKILL.md",
                    "inputs": [],
                    "outputs": [{"id": "plan", "kind": "file", "path": "plan.md"}],
                    "depends_on": [],
                },
                {
                    "type": "loop",
                    "id": "implementation",
                    "source": {
                        "from": "artifact:plan",
                        "provider": "kelpie.work_items.v1",
                    },
                    "max_items": 2,
                    "controller": "implementation_review_v1",
                    "body": [
                        {
                            "type": "step",
                            "id": "builder",
                            "lifecycle": "kelpie.phase.implementation_coder.v1",
                            "runner": "codex",
                            "prompt": "prompts/06_implementation_coder.md",
                            "skill": "skills/implementation-coder/SKILL.md",
                            "inputs": [],
                            "outputs": [{"id": "code", "kind": "file", "path": "code.md"}],
                            "depends_on": [],
                        },
                        {
                            "type": "step",
                            "id": "inspect_initial",
                            "lifecycle": "kelpie.phase.implementation_reviewer.v1",
                            "runner": "codex",
                            "prompt": "prompts/06_implementation_reviewer.md",
                            "skill": "skills/implementation-reviewer/SKILL.md",
                            "inputs": [],
                            "outputs": [{"id": "review", "kind": "file", "path": "review-initial.json"}],
                            "depends_on": ["builder"],
                        },
                        {
                            "type": "step",
                            "id": "repair",
                            "lifecycle": "kelpie.phase.implementation_fix.v1",
                            "runner": "codex",
                            "prompt": "prompts/06_implementation_fix.md",
                            "skill": "skills/implementation-fixer/SKILL.md",
                            "inputs": [{"name": "review", "from": "item-artifact:inspect_initial.review"}],
                            "outputs": [{"id": "fix", "kind": "file", "path": "fix.md"}],
                            "depends_on": ["inspect_initial"],
                        },
                        {
                            "type": "step",
                            "id": "inspect_final",
                            "lifecycle": "kelpie.phase.implementation_reviewer.v1",
                            "runner": "codex",
                            "prompt": "prompts/06_implementation_reviewer.md",
                            "skill": "skills/implementation-reviewer/SKILL.md",
                            "inputs": [],
                            "outputs": [{"id": "final_review", "kind": "file", "path": "review-final.json"}],
                            "depends_on": ["repair"],
                        },
                    ],
                    "exports": [],
                },
            ],
        }
        for relative in (
            "prompts/06_implementation_coder.md",
            "prompts/06_implementation_reviewer.md",
            "prompts/06_implementation_fix.md",
            "skills/implementation-coder/SKILL.md",
            "skills/implementation-reviewer/SKILL.md",
            "skills/implementation-fixer/SKILL.md",
        ):
            path = self.repo_root / relative
            self.assertTrue(path.is_file(), relative)

        class Port:
            def __init__(self) -> None:
                self.requests: list[StepExecutionRequest] = []

            def execute(self, request: StepExecutionRequest) -> object:
                self.requests.append(request)
                for output in request.expected_outputs:
                    output.path.parent.mkdir(parents=True, exist_ok=True)
                    output.path.write_text("output\n", encoding="utf-8")
                if request.step.lifecycle == "kelpie.phase.implementation_reviewer.v1":
                    status = (
                        "findings_present"
                        if request.step.local_id == "inspect_initial"
                        else "no_findings"
                    )
                    findings = (
                        (ReviewFinding(id="F-001", description="Repair this"),)
                        if status == "findings_present"
                        else ()
                    )
                    return StepCompletionEvent(
                        result=ReviewResult(
                            schema_version="1.0",
                            status=status,
                            findings=findings,
                            canonical_findings_json=(
                                '{"findings":[{"description":"Repair this",'
                                '"id":"F-001"}],"schema_version":"1.0"}'
                                if status == "findings_present"
                                else '{"findings":[],"schema_version":"1.0"}'
                            ),
                        )
                    )
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for relative in (
                "prompts/06_implementation_coder.md",
                "prompts/06_implementation_reviewer.md",
                "prompts/06_implementation_fix.md",
                "skills/implementation-coder/SKILL.md",
                "skills/implementation-reviewer/SKILL.md",
                "skills/implementation-fixer/SKILL.md",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# resource\n", encoding="utf-8")
            prepared = prepare_workflow_run(
                parse_workflow_config(payload),
                repo_root=root,
                artifact_root=root / "artifacts",
                providers={"kelpie.work_items.v1": [{"id": "item-a"}]},
            )
            port = Port()
            result = PipelineExecutor(
                port,
                controllers={"implementation_review_v1": ImplementationReviewController()},
            ).execute(prepared)

        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(
            [request.node_instance_id for request in port.requests],
            [
                "nodes/plan",
                "nodes/implementation/body/builder@item-a",
                "nodes/implementation/body/inspect_initial@item-a",
                "nodes/implementation/body/repair@item-a",
                "nodes/implementation/body/inspect_final@item-a",
            ],
        )

    def test_pipeline_rejects_controller_selection_outside_declared_body(self) -> None:
        payload = json.loads(
            (self.repo_root / "tests/fixtures/workflows/valid-v1.json").read_text(
                encoding="utf-8"
            )
        )
        payload["nodes"][1]["controller"] = "implementation_review_v1"  # type: ignore[index]

        class BadController:
            def initial_steps(self, loop: object, item: object) -> tuple[str, ...]:
                _ = loop, item
                return ("not-declared",)

        class Port:
            def __init__(self) -> None:
                self.requests: list[StepExecutionRequest] = []

            def execute(self, request: StepExecutionRequest) -> None:
                self.requests.append(request)
                for output in request.expected_outputs:
                    output.path.parent.mkdir(parents=True, exist_ok=True)
                    output.path.write_text("output\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for relative in (
                "prompts/plan.md",
                "prompts/code.md",
                "skills/plan.md",
                "skills/code.md",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# resource\n", encoding="utf-8")
            prepared = prepare_workflow_run(
                parse_workflow_config(payload),
                repo_root=root,
                artifact_root=root / "artifacts",
                providers={"kelpie.work_items.v1": [{"id": "item-a"}]},
            )
            port = Port()
            result = PipelineExecutor(
                port,
                virtual_inputs={"$issue": {"number": 20}},
                controllers={"implementation_review_v1": BadController()},
            ).execute(prepared)

        self.assertFalse(result.succeeded)
        self.assertIsInstance(result.error, UnsupportedLoopControllerError)
        self.assertEqual(
            [request.node_instance_id for request in port.requests],
            ["nodes/plan"],
        )


if __name__ == "__main__":
    unittest.main()
