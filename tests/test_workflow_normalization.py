from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from scripts.workflow_config import (
    ArtifactKey,
    LoopPlan,
    StepPlan,
    WorkflowConfigError,
    load_workflow_config,
    normalize_workflow_config,
    parse_workflow_config,
)


class WorkflowNormalizationTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def read_payload(self) -> dict[str, object]:
        return json.loads(self.fixture.read_text(encoding="utf-8"))

    def normalize(self, payload: dict[str, object]):
        return normalize_workflow_config(parse_workflow_config(payload))

    def assert_codes(self, error: WorkflowConfigError, *codes: str) -> None:
        actual = {diagnostic.code for diagnostic in error.diagnostics}
        self.assertTrue(set(codes).issubset(actual), msg=str(error))

    @staticmethod
    def step(
        step_id: str,
        *,
        inputs: list[dict[str, str]] | None = None,
        outputs: list[dict[str, str]] | None = None,
        depends_on: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "type": "step",
            "id": step_id,
            "lifecycle": "kelpie.phase.plan.v1",
            "runner": "codex",
            "prompt": "prompts/plan.md",
            "skill": "skills/plan.md",
            "inputs": inputs or [],
            "outputs": outputs or [],
            "depends_on": depends_on or [],
        }

    def test_top_level_and_body_use_one_immutable_step_contract(self) -> None:
        plan = self.normalize(self.read_payload())

        self.assertIsInstance(plan.nodes[0], StepPlan)
        self.assertIsInstance(plan.nodes[1], LoopPlan)
        loop = plan.nodes[1]
        assert isinstance(loop, LoopPlan)
        self.assertIsInstance(loop.body[0], StepPlan)
        self.assertEqual(plan.nodes[0].canonical_id, "nodes/plan")
        self.assertEqual(loop.canonical_id, "nodes/implementation")
        self.assertEqual(loop.body[0].canonical_id, "nodes/implementation/body/coder")
        self.assertEqual(plan.execution_order, ("nodes/plan", "nodes/implementation"))

        top_output = plan.nodes[0].outputs[0].artifact_key
        body_output = loop.body[0].outputs[0].artifact_key
        self.assertEqual(
            top_output,
            ArtifactKey("nodes/plan", "plan", "workflow", "scalar"),
        )
        self.assertEqual(body_output.scope, "loop_item")
        self.assertEqual(body_output.cardinality, "scalar")
        self.assertEqual(loop.source.artifact.key, top_output)
        self.assertEqual(plan.nodes[1].source.artifact.canonical_source, "artifact:nodes/plan.plan")

        payload = self.read_payload()
        payload["nodes"][1]["body"].append(  # type: ignore[index]
            self.step(
                "review",
                inputs=[
                    {
                        "name": "notes",
                        "from": "item-artifact:nodes/implementation/body/coder.notes",
                    }
                ],
            )
        )
        canonical_plan = self.normalize(payload)
        canonical_loop = canonical_plan.nodes[1]
        assert isinstance(canonical_loop, LoopPlan)
        self.assertEqual(
            canonical_loop.body[1].inputs[0].artifact.key,
            body_output,
        )

        with self.assertRaises(TypeError):
            plan.dependency_graph["nodes/new"] = ()  # type: ignore[index]
        with self.assertRaises(AttributeError):
            plan.nodes[0].runner = "other"  # type: ignore[misc]

    def test_collection_export_is_the_only_body_to_workflow_promotion(self) -> None:
        payload = self.read_payload()
        loop = payload["nodes"][1]  # type: ignore[index]
        loop["exports"] = [  # type: ignore[index]
            {"id": "notes", "from": "coder.notes", "cardinality": "collection"}
        ]
        after = self.step(
            "after",
            inputs=[{"name": "all_notes", "from": "artifact:implementation.notes"}],
            depends_on=["implementation"],
        )
        payload["nodes"].append(after)  # type: ignore[index]

        plan = self.normalize(payload)
        loop_plan = plan.nodes[1]
        after_plan = plan.nodes[2]
        assert isinstance(loop_plan, LoopPlan)
        assert isinstance(after_plan, StepPlan)
        export = loop_plan.exports[0]
        self.assertEqual(export.source_artifact.scope, "loop_item")
        self.assertEqual(export.artifact_key.scope, "workflow")
        self.assertEqual(export.artifact_key.cardinality, "collection")
        self.assertEqual(after_plan.inputs[0].artifact.key, export.artifact_key)
        self.assertIn("nodes/implementation", after_plan.dependencies)

        direct = deepcopy(payload)
        direct["nodes"][2]["inputs"] = [  # type: ignore[index]
            {"name": "notes", "from": "artifact:coder.notes"}
        ]
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(direct)
        self.assert_codes(context.exception, "cross_scope_reference")

    def test_artifact_inputs_add_typed_implicit_dependencies(self) -> None:
        payload = self.read_payload()
        body = payload["nodes"][1]["body"]  # type: ignore[index]
        body.append(  # type: ignore[union-attr]
            self.step(
                "review",
                inputs=[{"name": "notes", "from": "item-artifact:coder.notes"}],
                depends_on=[],
            )
        )
        plan = self.normalize(payload)
        loop = plan.nodes[1]
        assert isinstance(loop, LoopPlan)
        review = loop.body[1]
        self.assertEqual(review.dependencies, ("nodes/implementation/body/coder",))
        self.assertEqual(review.explicit_dependencies, ())
        self.assertEqual(review.inputs[0].cardinality, "scalar")

    def test_declaration_order_is_preserved_and_forward_dependencies_are_rejected(self) -> None:
        payload = self.read_payload()
        payload["nodes"] = [payload["nodes"][1], payload["nodes"][0]]  # type: ignore[index]
        # The loop's source now refers to a producer declared after it.
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "unreachable_dependency")

        independent = self.read_payload()
        independent["nodes"] = [independent["nodes"][1], independent["nodes"][0]]  # type: ignore[index]
        independent["nodes"][0]["source"]["from"] = "$issue"  # type: ignore[index]
        plan = self.normalize(independent)
        self.assertEqual(
            plan.execution_order,
            ("nodes/implementation", "nodes/plan"),
        )

    def test_duplicate_ids_and_outputs_are_rejected_in_their_sibling_scope(self) -> None:
        payload = self.read_payload()
        duplicate_node = deepcopy(payload["nodes"][0])  # type: ignore[index]
        payload["nodes"].append(duplicate_node)  # type: ignore[index]
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "duplicate_id")

        payload = self.read_payload()
        payload["nodes"][0]["outputs"].append(  # type: ignore[index]
            {"id": "plan", "kind": "file", "path": "other.md"}
        )
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "duplicate_id")

        payload = self.read_payload()
        payload["nodes"][1]["exports"] = [  # type: ignore[index]
            {"id": "same", "from": "coder.notes", "cardinality": "collection"},
            {"id": "same", "from": "coder.notes", "cardinality": "collection"},
        ]
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "duplicate_id")

    def test_undefined_and_cross_container_dependencies_are_rejected(self) -> None:
        payload = self.read_payload()
        payload["nodes"][0]["depends_on"] = ["missing"]  # type: ignore[index]
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "undefined_reference")

        payload = self.read_payload()
        payload["nodes"][1]["body"][0]["depends_on"] = ["plan"]  # type: ignore[index]
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "cross_scope_reference")

        payload = self.read_payload()
        payload["nodes"][0]["inputs"] = [  # type: ignore[index]
            {"name": "item", "from": "$loop_item"}
        ]
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "cross_scope_reference")

    def test_cycle_and_cardinality_mismatch_are_rejected(self) -> None:
        payload = self.read_payload()
        first = payload["nodes"][0]  # type: ignore[index]
        first["depends_on"] = ["implementation"]  # type: ignore[index]
        payload["nodes"][1]["source"]["from"] = "$issue"  # type: ignore[index]
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "unreachable_dependency")

        payload = self.read_payload()
        payload["nodes"][1]["exports"] = [  # type: ignore[index]
            {"id": "notes", "from": "coder.notes", "cardinality": "collection"}
        ]
        payload["nodes"].append(  # type: ignore[index]
            self.step(
                "after",
                inputs=[{"name": "notes", "from": "artifact:implementation.notes[scalar]"}],
                depends_on=["implementation"],
            )
        )
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "cardinality_mismatch")

    def test_mutual_forward_edges_report_a_cycle_without_reordering(self) -> None:
        payload = self.read_payload()
        first = payload["nodes"][0]  # type: ignore[index]
        loop = payload["nodes"][1]  # type: ignore[index]
        loop["source"]["from"] = "$issue"  # type: ignore[index]
        first["depends_on"] = ["implementation"]  # type: ignore[index]
        loop["body"][0]["depends_on"] = []  # type: ignore[index]
        # A second top-level node is needed for an actual same-container cycle.
        second = deepcopy(first)
        second["id"] = "second"  # type: ignore[index]
        second["depends_on"] = ["plan"]  # type: ignore[index]
        first["depends_on"] = ["second"]  # type: ignore[index]
        payload["nodes"] = [first, second, loop]
        with self.assertRaises(WorkflowConfigError) as context:
            self.normalize(payload)
        self.assert_codes(context.exception, "dependency_cycle", "unreachable_dependency")


if __name__ == "__main__":
    unittest.main()
