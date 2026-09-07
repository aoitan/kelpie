from __future__ import annotations

import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.workflow_models import (
    DiagnosticEvent,
    ReferenceResolution,
    RegistrationIndex,
    RegistrationResult,
)
from scripts.workflow_config import normalize_workflow_config, parse_workflow_config


class WorkflowNormalizationContractTests(unittest.TestCase):
    def test_boundary_contracts_are_immutable_and_read_only(self) -> None:
        diagnostic = DiagnosticEvent(
            phase="registration",
            code="duplicate_id",
            path="/nodes/1/id",
            message="duplicate node id",
            ordinal=0,
        )
        index = RegistrationIndex(
            top_nodes={"plan": "node"},
            body_nodes={"implementation": {"coder": "body-node"}},
            top_outputs={("plan", "plan"): "artifact"},
            body_outputs={},
            body_output_short={},
            exports={},
            artifact_graph={"artifact:plan.plan": "artifact"},
            declaration_order=("nodes/plan",),
        )
        result = RegistrationResult(index=index, diagnostics=(diagnostic,))
        resolution = ReferenceResolution(
            inputs={"nodes/plan": ()},
            dependencies={"nodes/plan": ()},
            loop_sources={},
            diagnostics=(),
        )

        with self.assertRaises(TypeError):
            result.index.top_nodes["other"] = "node"  # type: ignore[index]
        with self.assertRaises(TypeError):
            result.index.body_nodes["implementation"]["other"] = "node"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            result.diagnostics.append(diagnostic)  # type: ignore[attr-defined]
        with self.assertRaises(AttributeError):
            resolution.inputs = {}  # type: ignore[misc]
        self.assertEqual(result.index.declaration_order, ("nodes/plan",))
        self.assertEqual(result.diagnostics[0].phase, "registration")

    def test_contract_modules_do_not_import_runtime_layers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in ("scripts/workflow_models.py", "scripts/workflow_normalization.py"):
            tree = ast.parse((root / relative).read_text(encoding="utf-8"), filename=relative)
            imported = {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            imported.update(
                node.module.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            )
            self.assertNotIn("subprocess", imported)
            self.assertNotIn("pipeline_executor", imported)
            self.assertNotIn("run_issue_workflow", imported)

    def test_legacy_facade_delegates_to_normalization_module(self) -> None:
        payload = {
            "schema_version": "1.0",
            "id": "workflow",
            "profile": "repository_issue",
            "limits": {},
            "nodes": [
                {
                    "type": "step",
                    "id": "plan",
                    "lifecycle": "kelpie.phase.plan.v1",
                    "runner": "codex",
                    "prompt": "prompts/plan.md",
                    "skill": "skills/plan.md",
                    "inputs": [],
                    "outputs": [],
                    "depends_on": [],
                }
            ],
        }
        config = parse_workflow_config(payload)
        sentinel = object()
        with patch("scripts.workflow_normalization.normalize_declaration", return_value=sentinel):
            self.assertIs(normalize_workflow_config(config), sentinel)


if __name__ == "__main__":
    unittest.main()
