from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.workflow_config import (
    LoopConfig,
    MAX_WORKFLOW_CONFIG_BYTES,
    StepConfig,
    WorkflowConfigError,
    WorkflowConfigLoader,
    load_workflow_config,
)


class WorkflowConfigLoaderTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def read_fixture(self) -> dict[str, object]:
        return json.loads(self.fixture.read_text(encoding="utf-8"))

    def assert_codes(self, error: WorkflowConfigError, *codes: str) -> None:
        self.assertTrue(
            set(codes).issubset({diagnostic.code for diagnostic in error.diagnostics}),
            msg=str(error),
        )

    def test_valid_v1_config_produces_immutable_dtos(self) -> None:
        config = load_workflow_config(self.fixture)

        self.assertEqual(config.schema_version, "1.0")
        self.assertEqual(config.workflow_id, "issue-workflow")
        self.assertEqual(config.limits.max_loop_items, 10)
        self.assertIsInstance(config.nodes[0], StepConfig)
        self.assertIsInstance(config.nodes[1], LoopConfig)
        self.assertIsInstance(config.nodes, tuple)
        self.assertIsInstance(config.nodes[1].body, tuple)
        self.assertEqual(config.nodes[1].body[0].inputs[0].from_, "$loop_item")

        with self.assertRaises(AttributeError):
            config.profile = "changed"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            config.nodes += ()  # type: ignore[misc]

    def test_unknown_version_and_fields_are_rejected_with_pointers(self) -> None:
        payload = self.read_fixture()
        payload["schema_version"] = "2.0"
        payload["unknown"] = True
        step = payload["nodes"][0]
        step["typo"] = "ignored"  # type: ignore[index]

        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader().parse(payload)

        error = context.exception
        self.assert_codes(error, "unknown_schema_version", "unknown_field")
        paths = {diagnostic.path for diagnostic in error.diagnostics}
        self.assertIn("/schema_version", paths)
        self.assertIn("/unknown", paths)
        self.assertIn("/nodes/0/typo", paths)

    def test_duplicate_keys_are_rejected_at_nested_json_pointer(self) -> None:
        source = json.dumps(self.read_fixture()).replace(
            '"max_loop_items": 10, "max_total_steps": 50',
            '"max_loop_items": 10, "max_loop_items": 11, "max_total_steps": 50',
        )

        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader().loads(source)

        error = context.exception
        self.assert_codes(error, "duplicate_key")
        self.assertIn("/limits/max_loop_items", {item.path for item in error.diagnostics})

    def test_wrong_types_reject_bool_as_integer(self) -> None:
        payload = self.read_fixture()
        payload["limits"]["max_loop_items"] = True  # type: ignore[index]
        payload["nodes"][1]["max_items"] = "10"  # type: ignore[index]
        payload["nodes"][0]["outputs"] = {}  # type: ignore[index]

        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader().parse(payload)

        self.assert_codes(context.exception, "wrong_type")
        self.assertIn("/limits/max_loop_items", {item.path for item in context.exception.diagnostics})
        self.assertIn("/nodes/1/max_items", {item.path for item in context.exception.diagnostics})

    def test_config_size_and_json_depth_are_bounded_before_dto_creation(self) -> None:
        source = json.dumps(self.read_fixture())
        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader(max_bytes=len(source.encode("utf-8")) - 1).loads(source)
        self.assert_codes(context.exception, "config_too_large")

        deeply_nested = "{" * 4 + '"schema_version":"1.0"' + "}" * 4
        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader(max_depth=3).loads(deeply_nested)
        self.assert_codes(context.exception, "json_depth_exceeded")

        self.assertLess(MAX_WORKFLOW_CONFIG_BYTES, 16 * 1024 * 1024)

    def test_invalid_utf8_and_invalid_json_are_structured(self) -> None:
        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader().loads(b"\xff")
        self.assert_codes(context.exception, "invalid_utf8")

        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader().loads("{not-json")
        self.assert_codes(context.exception, "invalid_json")
        self.assertIsNotNone(context.exception.diagnostics[0].line)

    def test_nested_loop_is_rejected(self) -> None:
        payload = self.read_fixture()
        payload["nodes"][1]["body"].append(  # type: ignore[index]
            {
                "type": "loop",
                "id": "nested",
                "source": {"from": "$items", "provider": "items.v1"},
                "max_items": 1,
                "controller": "fixed_sequence.v1",
                "body": [],
                "exports": [],
            }
        )

        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader().parse(payload)
        self.assert_codes(context.exception, "nested_loop")

    def test_policy_and_executable_fields_are_closed_schema_errors(self) -> None:
        payload = self.read_fixture()
        payload["nodes"][0]["retry"] = {"max": 3}  # type: ignore[index]
        payload["nodes"][0]["command"] = ["echo", "unsafe"]  # type: ignore[index]

        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader().parse(payload)
        self.assert_codes(context.exception, "unknown_field")
        paths = {item.path for item in context.exception.diagnostics}
        self.assertIn("/nodes/0/retry", paths)
        self.assertIn("/nodes/0/command", paths)

    def test_lexically_unsafe_resource_path_is_rejected(self) -> None:
        payload = self.read_fixture()
        payload["nodes"][0]["prompt"] = "../outside.md"  # type: ignore[index]
        payload["nodes"][0]["skill"] = "skills\\unsafe.md"  # type: ignore[index]

        with self.assertRaises(WorkflowConfigError) as context:
            WorkflowConfigLoader().parse(payload)
        self.assert_codes(context.exception, "unsafe_path")

    def test_invalid_file_does_not_create_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "workflow.json"
            path.write_text('{"schema_version":"2.0"}', encoding="utf-8")
            before = sorted(item.name for item in Path(tmpdir).iterdir())

            with self.assertRaises(WorkflowConfigError):
                load_workflow_config(path)

            self.assertEqual(before, sorted(item.name for item in Path(tmpdir).iterdir()))


if __name__ == "__main__":
    unittest.main()
