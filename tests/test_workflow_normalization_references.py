from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from scripts import workflow_config
from scripts.workflow_normalization import (
    build_workflow_plan,
    register_declarations,
    resolve_references,
)


class WorkflowNormalizationReferenceTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def config(self, mutation: str | None = None):
        payload = json.loads(self.fixture.read_text(encoding="utf-8"))
        if mutation == "undefined":
            payload["nodes"][1]["source"]["from"] = "artifact:missing"
        return workflow_config.parse_workflow_config(payload)

    def test_resolution_returns_immutable_inputs_dependencies_and_loop_sources(self) -> None:
        config = self.config()
        deps = workflow_config._normalization_dependencies()  # type: ignore[attr-defined]
        registration = register_declarations(config, deps=deps)
        before = dict(registration.index.top_outputs)
        resolution = resolve_references(config, registration, deps=deps)

        self.assertEqual(resolution.dependencies["nodes/implementation"], ("nodes/plan",))
        self.assertEqual(resolution.inputs["nodes/plan"][0].virtual_input, "$issue")
        self.assertIn("nodes/implementation", resolution.loop_sources)
        self.assertEqual(dict(registration.index.top_outputs), before)
        with self.assertRaises(TypeError):
            resolution.dependencies["nodes/new"] = ()  # type: ignore[index]

        plan = build_workflow_plan(config, registration, resolution, deps=deps)
        self.assertEqual(plan.execution_order, ("nodes/plan", "nodes/implementation"))

    def test_resolution_preserves_reference_diagnostic_contract(self) -> None:
        config = self.config("undefined")
        deps = workflow_config._normalization_dependencies()  # type: ignore[attr-defined]
        registration = register_declarations(config, deps=deps)
        resolution = resolve_references(config, registration, deps=deps)

        self.assertEqual(
            [(item.code, item.path, item.ordinal) for item in resolution.diagnostics],
            [("undefined_reference", "/nodes/1/source/from", 0)],
        )

    def test_resolution_drops_unreachable_loop_source_from_boundary_outputs(self) -> None:
        payload = json.loads(self.fixture.read_text(encoding="utf-8"))
        payload["nodes"] = [payload["nodes"][1], payload["nodes"][0]]  # type: ignore[index]
        config = workflow_config.parse_workflow_config(payload)
        deps = workflow_config._normalization_dependencies()  # type: ignore[attr-defined]
        registration = register_declarations(config, deps=deps)
        resolution = resolve_references(config, registration, deps=deps)

        self.assertEqual(
            [(item.code, item.path) for item in resolution.diagnostics],
            [("unreachable_dependency", "/nodes/0/source/from")],
        )
        self.assertIsNone(resolution.loop_sources["nodes/implementation"].artifact)
        self.assertEqual(resolution.dependencies["nodes/implementation"], ())

    def test_plan_artifact_lookup_preserves_compatibility_aliases(self) -> None:
        plan = workflow_config.normalize_workflow_config(self.config())

        artifact = plan.artifact_for("artifact:plan[scalar]")

        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertEqual(artifact.reference, "artifact:nodes/plan.plan")

    def test_resolution_uses_registration_index_instead_of_rerunning_registration(self) -> None:
        config = self.config()
        deps = workflow_config._normalization_dependencies()  # type: ignore[attr-defined]
        registration = register_declarations(config, deps=deps)
        altered_index = replace(
            registration.index,
            top_outputs={},
            artifact_graph={},
        )
        altered_registration = replace(registration, index=altered_index)

        resolution = resolve_references(config, altered_registration, deps=deps)

        self.assertEqual(
            [(item.code, item.path) for item in resolution.diagnostics],
            [("undefined_reference", "/nodes/1/source/from")],
        )

    def test_plan_builder_uses_resolution_values_instead_of_re_resolving(self) -> None:
        config = self.config()
        deps = workflow_config._normalization_dependencies()  # type: ignore[attr-defined]
        registration = register_declarations(config, deps=deps)
        resolution = resolve_references(config, registration, deps=deps)
        altered_resolution = replace(
            resolution,
            inputs={},
            dependencies={},
            explicit_dependencies={},
            loop_sources=resolution.loop_sources,
            exports=resolution.exports,
        )

        plan = build_workflow_plan(config, registration, altered_resolution, deps=deps)

        self.assertEqual(plan.nodes[0].inputs, ())
        self.assertEqual(plan.nodes[0].dependencies, ())
        self.assertEqual(plan.nodes[1].dependencies, ())
        self.assertEqual(plan.nodes[1].body[0].inputs, ())

    def test_resolution_rejects_registration_from_another_config(self) -> None:
        config = self.config()
        other_config = self.config()
        deps = workflow_config._normalization_dependencies()  # type: ignore[attr-defined]
        other_registration = register_declarations(other_config, deps=deps)

        with self.assertRaises(ValueError):
            resolve_references(config, other_registration, deps=deps)

    def test_resolution_rejects_registration_from_config_sharing_nodes(self) -> None:
        config = self.config()
        other_config = workflow_config.WorkflowConfig(
            schema_version=config.schema_version,
            id="other-workflow",
            profile=config.profile,
            limits=config.limits,
            nodes=config.nodes,
        )
        deps = workflow_config._normalization_dependencies()  # type: ignore[attr-defined]
        other_registration = register_declarations(other_config, deps=deps)

        with self.assertRaises(ValueError):
            resolve_references(config, other_registration, deps=deps)

    def test_plan_builder_rejects_resolution_from_another_registration(self) -> None:
        config = self.config()
        other_config = self.config()
        deps = workflow_config._normalization_dependencies()  # type: ignore[attr-defined]
        registration = register_declarations(config, deps=deps)
        other_registration = register_declarations(other_config, deps=deps)
        other_resolution = resolve_references(other_config, other_registration, deps=deps)

        with self.assertRaises(ValueError):
            build_workflow_plan(config, registration, other_resolution, deps=deps)


if __name__ == "__main__":
    unittest.main()
