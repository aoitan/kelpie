from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from scripts.workflow_config import (
    CapabilityRegistry,
    WorkflowConfigError,
    WorkflowHardLimits,
    load_workflow_config,
    normalize_workflow_config,
    parse_workflow_config,
    preflight_workflow_bounds,
)


class _CountingProvider:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls = 0

    def snapshot(self, _binding: object, _limits: object) -> object:
        self.calls += 1
        return self.payload


class WorkflowBoundsTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def read_payload(self) -> dict[str, object]:
        return json.loads(self.fixture.read_text(encoding="utf-8"))

    def plan(self, payload: dict[str, object] | None = None):
        return normalize_workflow_config(parse_workflow_config(payload or self.read_payload()))

    def assert_codes(self, error: WorkflowConfigError, *codes: str) -> None:
        actual = {diagnostic.code for diagnostic in error.diagnostics}
        self.assertTrue(set(codes).issubset(actual), msg=str(error))

    def test_provider_is_read_once_and_snapshot_is_immutable(self) -> None:
        provider = _CountingProvider(
            [
                {"id": "item-a", "title": "A"},
                {"id": "item-b", "title": "B"},
            ]
        )
        result = preflight_workflow_bounds(
            self.plan(),
            {"kelpie.work_items.v1": provider},
            registry=CapabilityRegistry.default(),
        )

        self.assertEqual(provider.calls, 1)
        snapshot = result.snapshots["implementation"]
        self.assertEqual(snapshot.item_ids, ("item-a", "item-b"))
        self.assertEqual(tuple(item.position for item in snapshot.items), (0, 1))
        self.assertEqual(result.loop_item_count, 2)
        self.assertEqual(result.potential_step_executions, 3)
        with self.assertRaises(TypeError):
            snapshot.items[0].payload["title"] = "changed"  # type: ignore[index]
        with self.assertRaises(TypeError):
            result.snapshots["other"] = snapshot  # type: ignore[index]

    def test_effective_limits_are_the_lower_of_config_and_system_caps(self) -> None:
        payload = self.read_payload()
        payload["limits"] = {
            "max_loop_items": 2,
            "max_total_steps": 5,
        }
        provider = _CountingProvider([{"id": "one"}, {"id": "two"}])
        result = preflight_workflow_bounds(
            self.plan(payload),
            {"kelpie.work_items.v1": provider},
            hard_limits=WorkflowHardLimits(max_loop_items=3, max_total_steps=4),
        )

        self.assertEqual(result.effective_limits.max_loop_items, 2)
        self.assertEqual(result.effective_limits.max_total_steps, 4)

    def test_payload_and_order_changes_snapshot_digest(self) -> None:
        first = preflight_workflow_bounds(
            self.plan(),
            {"kelpie.work_items.v1": [{"id": "a", "value": 1}, {"id": "b", "value": 2}]},
        )
        changed_payload = preflight_workflow_bounds(
            self.plan(),
            {"kelpie.work_items.v1": [{"id": "a", "value": 9}, {"id": "b", "value": 2}]},
        )
        changed_order = preflight_workflow_bounds(
            self.plan(),
            {"kelpie.work_items.v1": [{"id": "b", "value": 2}, {"id": "a", "value": 1}]},
        )

        first_snapshot = first.snapshots["implementation"]
        self.assertNotEqual(
            first_snapshot.digest,
            changed_payload.snapshots["implementation"].digest,
        )
        self.assertNotEqual(
            first_snapshot.digest,
            changed_order.snapshots["implementation"].digest,
        )
        self.assertNotEqual(first.snapshot_digest, changed_order.snapshot_digest)

    def test_duplicate_and_unsafe_item_ids_are_rejected(self) -> None:
        cases = (
            ([{"id": "same"}, {"id": "same"}], "duplicate_item_id"),
            ([{"id": "../escape"}], "unsafe_item_id"),
            ([{"id": ""}], "unsafe_item_id"),
            ([{"id": 1}], "unsafe_item_id"),
            ([{"title": "missing"}], "invalid_loop_item"),
        )
        for items, code in cases:
            with self.subTest(code=code, items=items):
                with self.assertRaises(WorkflowConfigError) as context:
                    preflight_workflow_bounds(
                        self.plan(),
                        {"kelpie.work_items.v1": items},
                    )
                self.assert_codes(context.exception, code)

    def test_source_must_be_finite_and_within_item_cap(self) -> None:
        def infinite_items():
            index = 0
            while True:
                yield {"id": f"item-{index}"}
                index += 1

        provider = _CountingProvider(infinite_items())
        with self.assertRaises(WorkflowConfigError) as context:
            preflight_workflow_bounds(
                self.plan(),
                {"kelpie.work_items.v1": provider},
                hard_limits=WorkflowHardLimits(max_loop_items=3),
            )
        self.assert_codes(context.exception, "resource_limit_exceeded")
        self.assertEqual(provider.calls, 1)

    def test_item_snapshot_and_total_step_caps_fail_before_execution(self) -> None:
        item_provider = _CountingProvider([{"id": "large", "value": "x" * 100}])
        with self.assertRaises(WorkflowConfigError) as item_context:
            preflight_workflow_bounds(
                self.plan(),
                {"kelpie.work_items.v1": item_provider},
                hard_limits=WorkflowHardLimits(max_item_bytes=32),
            )
        self.assert_codes(item_context.exception, "resource_limit_exceeded")
        self.assertEqual(item_provider.calls, 1)

        over_nodes = _CountingProvider([{"id": "one"}])
        with self.assertRaises(WorkflowConfigError) as structural_context:
            preflight_workflow_bounds(
                self.plan(),
                {"kelpie.work_items.v1": over_nodes},
                hard_limits=WorkflowHardLimits(max_nodes=2),
            )
        self.assert_codes(structural_context.exception, "resource_limit_exceeded")
        self.assertEqual(over_nodes.calls, 0)

        over_total = _CountingProvider([{"id": "one"}, {"id": "two"}])
        with self.assertRaises(WorkflowConfigError) as total_context:
            preflight_workflow_bounds(
                self.plan(),
                {"kelpie.work_items.v1": over_total},
                hard_limits=WorkflowHardLimits(max_total_steps=2),
            )
        self.assert_codes(total_context.exception, "resource_limit_exceeded")
        self.assertEqual(over_total.calls, 1)

    def test_snapshot_byte_cap_and_input_byte_cap_are_enforced(self) -> None:
        payload = self.read_payload()
        payload["limits"] = {
            "max_snapshot_bytes": 32,
            "max_prompt_input_bytes": 32,
        }
        with self.assertRaises(WorkflowConfigError) as context:
            preflight_workflow_bounds(
                self.plan(payload),
                {"kelpie.work_items.v1": [{"id": "item", "value": "x" * 20}]},
            )
        self.assert_codes(context.exception, "resource_limit_exceeded")

    def test_nested_loop_remains_a_schema_error(self) -> None:
        payload = self.read_payload()
        nested = deepcopy(payload)
        nested["nodes"][1]["body"].append(  # type: ignore[index]
            {
                "type": "loop",
                "id": "nested",
                "source": {"from": "$issue", "provider": "kelpie.work_items.v1"},
                "max_items": 1,
                "controller": "fixed_sequence.v1",
                "body": [],
                "exports": [],
            }
        )
        with self.assertRaises(WorkflowConfigError) as context:
            parse_workflow_config(nested)
        self.assert_codes(context.exception, "nested_loop")

    def test_unregistered_source_is_rejected_without_reading_provider(self) -> None:
        provider = _CountingProvider([{"id": "one"}])
        registry = CapabilityRegistry(
            runners={"codex": {}},
            lifecycles={
                "kelpie.phase.plan.v1": {},
                "kelpie.phase.implementation.v1": {},
            },
            controllers={"fixed_sequence.v1": {}},
            virtual_inputs={"$issue": {}, "$loop_item": {}},
            loop_sources={},
        )
        with self.assertRaises(WorkflowConfigError) as context:
            preflight_workflow_bounds(
                self.plan(),
                {"kelpie.work_items.v1": provider},
                registry=registry,
            )
        self.assert_codes(context.exception, "unknown_capability")
        self.assertEqual(provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
