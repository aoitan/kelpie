from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.pipeline_executor import (
    PipelineExecutor,
    PipelineRunResult,
    StepCompletionEvent,
    StepExecutionRequest,
    prepare_workflow_run,
)
from scripts.workflow_config import ArtifactOutputValidationError, parse_workflow_config


class RecordingPort:
    """Small trusted fake for the structural executor tests."""

    def __init__(
        self,
        *,
        fail_ids: set[str] | None = None,
        write_outputs: bool = True,
        opaque_result: object = None,
    ) -> None:
        self.requests: list[StepExecutionRequest] = []
        self.fail_ids = fail_ids or set()
        self.write_outputs = write_outputs
        self.opaque_result = opaque_result

    def execute(self, request: StepExecutionRequest) -> object:
        self.requests.append(request)
        if self.write_outputs:
            for output in request.expected_outputs:
                output.path.parent.mkdir(parents=True, exist_ok=True)
                if output.kind == "file":
                    output.path.write_text(request.node_instance_id, encoding="utf-8")
                else:  # pragma: no cover - current fixtures only use files
                    output.path.mkdir(parents=True, exist_ok=True)
        if request.node_instance_id in self.fail_ids:
            return StepCompletionEvent(
                success=False,
                status="failed",
                error=f"failed: {request.node_instance_id}",
            )
        if self.opaque_result is not None:
            return {
                "status": "completed",
                "result": self.opaque_result,
            }
        return None


class PipelineExecutorTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def read_payload(self) -> dict[str, object]:
        return json.loads(self.fixture.read_text(encoding="utf-8"))

    @staticmethod
    def write_resources(root: Path) -> None:
        for relative in (
            "prompts/plan.md",
            "prompts/code.md",
            "skills/plan.md",
            "skills/code.md",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {relative}\n", encoding="utf-8")

    def prepare(
        self,
        root: Path,
        payload: dict[str, object],
        *,
        items: list[dict[str, object]] | None = None,
    ):
        self.write_resources(root)
        return prepare_workflow_run(
            parse_workflow_config(payload),
            repo_root=root,
            artifact_root=root / "artifacts",
            providers={
                "kelpie.work_items.v1": (
                    items if items is not None else [{"id": "item-a"}, {"id": "item-b"}]
                )
            },
        )

    @staticmethod
    def run_executor(prepared, port: RecordingPort) -> PipelineRunResult:
        return PipelineExecutor(
            port,
            virtual_inputs={"$issue": {"number": 20}},
        ).execute(prepared)

    def top_only_payload(self) -> dict[str, object]:
        template = self.read_payload()["nodes"][0]
        nodes = []
        for node_id in ("first", "second", "third"):
            node = deepcopy(template)
            node["id"] = node_id  # type: ignore[index]
            node["inputs"] = []  # type: ignore[index]
            node["outputs"][0]["id"] = f"{node_id}_output"  # type: ignore[index]
            node["outputs"][0]["path"] = f"{node_id}.md"  # type: ignore[index]
            node["depends_on"] = []  # type: ignore[index]
            nodes.append(node)
        payload = self.read_payload()
        payload["nodes"] = nodes
        return payload

    def test_config_declaration_order_controls_top_level_execution(self) -> None:
        payload = self.top_only_payload()
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first_root = Path(first_dir)
            second_root = Path(second_dir)
            first_port = RecordingPort()
            first_result = self.run_executor(self.prepare(first_root, payload), first_port)

            reordered = deepcopy(payload)
            reordered["nodes"] = list(reversed(reordered["nodes"]))  # type: ignore[arg-type]
            second_port = RecordingPort()
            second_result = self.run_executor(self.prepare(second_root, reordered), second_port)

        self.assertTrue(first_result.succeeded)
        self.assertTrue(second_result.succeeded)
        self.assertEqual(
            [request.node_instance_id for request in first_port.requests],
            ["nodes/first", "nodes/second", "nodes/third"],
        )
        self.assertEqual(
            [request.node_instance_id for request in second_port.requests],
            ["nodes/third", "nodes/second", "nodes/first"],
        )

    def test_top_level_and_loop_steps_share_request_and_resolve_item_context(self) -> None:
        payload = self.read_payload()
        review = deepcopy(payload["nodes"][1]["body"][0])  # type: ignore[index]
        review["id"] = "review"  # type: ignore[index]
        review["inputs"] = [  # type: ignore[index]
            {"name": "notes", "from": "item-artifact:coder.notes"},
            {"name": "plan", "from": "artifact:plan"},
        ]
        review["outputs"][0]["id"] = "review"  # type: ignore[index]
        review["outputs"][0]["path"] = "review.md"  # type: ignore[index]
        review["depends_on"] = ["coder"]  # type: ignore[index]
        payload["nodes"][1]["body"].append(review)  # type: ignore[index]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            port = RecordingPort()
            result = self.run_executor(
                self.prepare(
                    root,
                    payload,
                    items=[{"id": "api", "title": "API"}, {"id": "cli", "title": "CLI"}],
                ),
                port,
            )

        self.assertTrue(result.succeeded, result.error)
        self.assertTrue(all(isinstance(request, StepExecutionRequest) for request in port.requests))
        self.assertEqual(
            [request.node_instance_id for request in port.requests],
            [
                "nodes/plan",
                "nodes/implementation/body/coder@api",
                "nodes/implementation/body/review@api",
                "nodes/implementation/body/coder@cli",
                "nodes/implementation/body/review@cli",
            ],
        )
        top_request, coder_api, review_api, coder_cli, review_cli = port.requests
        self.assertIsNone(top_request.loop_context)
        self.assertEqual(coder_api.loop_context.item_id, "api")
        self.assertEqual(coder_cli.loop_context.item_id, "cli")
        self.assertEqual(coder_api.resolved_inputs[0].value["id"], "api")
        self.assertEqual(coder_cli.resolved_inputs[0].value["id"], "cli")
        self.assertNotEqual(
            coder_api.expected_outputs[0].path,
            coder_cli.expected_outputs[0].path,
        )
        self.assertEqual(review_api.resolved_inputs[0].artifacts[0].namespace.item_id, "api")
        self.assertEqual(review_cli.resolved_inputs[0].artifacts[0].namespace.item_id, "cli")
        self.assertEqual(review_api.resolved_inputs[1].artifacts[0].namespace.relative_path, "plan.md")
        self.assertIn("nodes/implementation", result.completed_instances)

    def test_collection_export_is_resolved_as_ordered_item_artifacts(self) -> None:
        payload = self.read_payload()
        loop = payload["nodes"][1]
        loop["exports"] = [  # type: ignore[index]
            {"id": "notes", "from": "coder.notes", "cardinality": "collection"}
        ]
        publish = deepcopy(payload["nodes"][0])
        publish["id"] = "publish"  # type: ignore[index]
        publish["inputs"] = [  # type: ignore[index]
            {"name": "notes", "from": "artifact:implementation.notes[*]"}
        ]
        publish["outputs"][0]["id"] = "published"  # type: ignore[index]
        publish["outputs"][0]["path"] = "published.md"  # type: ignore[index]
        publish["depends_on"] = []  # type: ignore[index]
        payload["nodes"].append(publish)  # type: ignore[index]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            port = RecordingPort()
            result = self.run_executor(
                self.prepare(root, payload, items=[{"id": "api"}, {"id": "cli"}]),
                port,
            )

        self.assertTrue(result.succeeded, result.error)
        publish_request = port.requests[-1]
        collection = publish_request.resolved_inputs[0]
        self.assertEqual(collection.cardinality, "collection")
        self.assertEqual(
            [artifact.namespace.item_id for artifact in collection.artifacts],
            ["api", "cli"],
        )
        self.assertEqual(len(collection.value), 2)
        self.assertEqual(
            [path.name for path in collection.value],
            ["06-implementation-notes.md", "06-implementation-notes.md"],
        )

    def test_failure_stops_before_following_step_and_does_not_complete_failed_step(self) -> None:
        payload = self.top_only_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            port = RecordingPort(fail_ids={"nodes/second"})
            result = self.run_executor(self.prepare(root, payload), port)

        self.assertFalse(result.succeeded)
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            [request.node_instance_id for request in port.requests],
            ["nodes/first", "nodes/second"],
        )
        self.assertIn("nodes/first", result.completed_instances)
        self.assertNotIn("nodes/second", result.completed_instances)
        self.assertNotIn("nodes/third", result.completed_instances)

    def test_missing_output_fails_before_artifact_consumer_is_called(self) -> None:
        payload = self.top_only_payload()
        consumer = deepcopy(payload["nodes"][1])
        consumer["id"] = "consumer"  # type: ignore[index]
        consumer["inputs"] = [  # type: ignore[index]
            {"name": "first", "from": "artifact:first.first_output"}
        ]
        consumer["outputs"][0]["id"] = "consumer_output"  # type: ignore[index]
        consumer["outputs"][0]["path"] = "consumer.md"  # type: ignore[index]
        consumer["depends_on"] = []  # type: ignore[index]
        payload["nodes"] = [payload["nodes"][0], consumer]  # type: ignore[index]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            port = RecordingPort(write_outputs=False)
            result = self.run_executor(self.prepare(root, payload), port)

        self.assertFalse(result.succeeded)
        self.assertIsInstance(result.error, ArtifactOutputValidationError)
        self.assertEqual([request.node_instance_id for request in port.requests], ["nodes/first"])

    def test_stale_existing_output_is_not_consumed_by_a_following_step(self) -> None:
        payload = self.top_only_payload()
        consumer = deepcopy(payload["nodes"][1])
        consumer["id"] = "consumer"  # type: ignore[index]
        consumer["inputs"] = [  # type: ignore[index]
            {"name": "first", "from": "artifact:first.first_output"}
        ]
        consumer["outputs"][0]["id"] = "consumer_output"  # type: ignore[index]
        consumer["outputs"][0]["path"] = "consumer.md"  # type: ignore[index]
        consumer["depends_on"] = []  # type: ignore[index]
        payload["nodes"] = [payload["nodes"][0], consumer]  # type: ignore[index]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            prepared = self.prepare(root, payload)
            stale_output = root / "artifacts" / "first.md"
            stale_output.parent.mkdir(parents=True)
            stale_output.write_text("from an earlier attempt", encoding="utf-8")
            port = RecordingPort(write_outputs=False)
            result = self.run_executor(prepared, port)

        self.assertFalse(result.succeeded)
        self.assertIsInstance(result.error, ArtifactOutputValidationError)
        self.assertEqual([request.node_instance_id for request in port.requests], ["nodes/first"])
        self.assertNotIn("nodes/consumer", result.completed_instances)

    def test_opaque_verdict_data_is_not_interpreted_as_retry_or_route_policy(self) -> None:
        payload = self.top_only_payload()
        opaque = {"verdict": "retry", "budget": 0, "human_gate": True}
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            port = RecordingPort(opaque_result=opaque)
            result = self.run_executor(self.prepare(root, payload), port)

        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.events[0].result, opaque)
        self.assertEqual(len(port.requests), 3)


if __name__ == "__main__":
    unittest.main()
