from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from scripts import workflow_config
from scripts.workflow_normalization import register_declarations


class WorkflowNormalizationRegistrationTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def read_config(self):
        payload = json.loads(self.fixture.read_text(encoding="utf-8"))
        return workflow_config.parse_workflow_config(payload)

    def test_registration_returns_read_only_indexes_without_resolution(self) -> None:
        result = register_declarations(
            self.read_config(),
            deps=workflow_config._normalization_dependencies(),  # type: ignore[attr-defined]
        )

        self.assertEqual(result.index.declaration_order, ("nodes/plan", "nodes/implementation"))
        self.assertIn(("plan", "plan"), result.index.top_outputs)
        self.assertIn(("implementation", "coder", "notes"), result.index.body_outputs)
        self.assertEqual(result.index.top_nodes["plan"].canonical_id, "nodes/plan")
        with self.assertRaises(TypeError):
            result.index.top_nodes["other"] = object()  # type: ignore[index]
        self.assertEqual(result.diagnostics, ())

    def test_duplicate_registration_diagnostic_preserves_order_and_path(self) -> None:
        payload = json.loads(self.fixture.read_text(encoding="utf-8"))
        payload["nodes"].append(deepcopy(payload["nodes"][0]))
        config = workflow_config.parse_workflow_config(payload)
        result = register_declarations(
            config,
            deps=workflow_config._normalization_dependencies(),  # type: ignore[attr-defined]
        )

        self.assertEqual(
            [(item.code, item.path, item.ordinal) for item in result.diagnostics],
            [("duplicate_id", "/nodes/2/id", 0)],
        )


if __name__ == "__main__":
    unittest.main()
