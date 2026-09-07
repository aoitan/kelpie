from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import workflow_config
from scripts import workflow_normalization
from scripts.workflow_normalization import validate_dependency_graph


class WorkflowNormalizationPlanTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def test_graph_validation_is_independent_of_normalization_entrypoint(self) -> None:
        payload = json.loads(self.fixture.read_text(encoding="utf-8"))
        config = workflow_config.parse_workflow_config(payload)
        plan = workflow_config.normalize_workflow_config(config)
        self.assertEqual(validate_dependency_graph(plan), ())

        cyclic_graph = {
            "nodes/plan": ("nodes/implementation",),
            "nodes/implementation": ("nodes/plan",),
        }
        cyclic = workflow_config.WorkflowPlan(
            schema_version=plan.schema_version,
            workflow_id=plan.workflow_id,
            profile=plan.profile,
            limits=plan.limits,
            nodes=plan.nodes,
            dependency_graph=cyclic_graph,
            artifact_graph=plan.artifact_graph,
        )
        diagnostics = validate_dependency_graph(cyclic)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].code, "dependency_cycle")
        self.assertEqual(diagnostics[0].phase, "graph_validation")

    def test_normalization_entrypoint_runs_each_phase_once(self) -> None:
        payload = json.loads(self.fixture.read_text(encoding="utf-8"))
        config = workflow_config.parse_workflow_config(payload)
        with (
            patch.object(
                workflow_normalization,
                "register_declarations",
                wraps=workflow_normalization.register_declarations,
            ) as register,
            patch.object(
                workflow_normalization,
                "resolve_references",
                wraps=workflow_normalization.resolve_references,
            ) as resolve,
            patch.object(
                workflow_normalization,
                "build_workflow_plan",
                wraps=workflow_normalization.build_workflow_plan,
            ) as build,
            patch.object(
                workflow_normalization,
                "validate_dependency_graph",
                wraps=workflow_normalization.validate_dependency_graph,
            ) as validate,
        ):
            workflow_config.normalize_workflow_config(config)

        self.assertEqual(register.call_count, 1)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(validate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
