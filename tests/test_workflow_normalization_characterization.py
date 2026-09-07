from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from scripts.workflow_config import (
    InputBinding,
    LoopConfig,
    LoopSource,
    WorkflowConfig,
    WorkflowConfigError,
    WorkflowLimits,
    build_workflow_plan,
    canonical_sha256,
    normalize_workflow_config,
    normalise_workflow_config,
    parse_workflow_config,
    workflow_plan_payload,
    workflow_plan_digest,
)


ROOT = Path(__file__).resolve().parent
CASE_INDEX = ROOT / "fixtures" / "workflows" / "characterization" / "cases.json"
BASE_FIXTURE = ROOT / "fixtures" / "workflows" / "valid-v1.json"
BASELINE = Path(__file__).resolve().parents[1] / "architecture" / "normalization-baseline.json"


def _step(step_id: str, *, inputs: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "type": "step",
        "id": step_id,
        "lifecycle": "kelpie.phase.implementation.v1",
        "runner": "codex",
        "prompt": "prompts/code.md",
        "skill": "skills/code.md",
        "inputs": inputs or [],
        "outputs": [],
        "depends_on": [],
    }


def _mutate(payload: dict[str, object], mutation: str) -> dict[str, object]:
    value = deepcopy(payload)
    nodes = value["nodes"]
    assert isinstance(nodes, list)
    if mutation == "none":
        return value
    if mutation == "duplicate-node":
        nodes.append(deepcopy(nodes[0]))
    elif mutation == "undefined-reference":
        nodes[1]["source"]["from"] = "artifact:missing"  # type: ignore[index]
    elif mutation == "forward-dependency":
        value["nodes"] = [nodes[1], nodes[0]]
    elif mutation == "cycle":
        nodes[0]["depends_on"] = ["implementation"]  # type: ignore[index]
    elif mutation == "cross-scope":
        nodes[0]["inputs"].append(  # type: ignore[index]
            {"name": "body_notes", "from": "artifact:coder.notes"}
        )
    elif mutation == "cardinality":
        nodes[1]["source"]["from"] = "artifact:plan[*]"  # type: ignore[index]
    elif mutation == "nested-body":
        nodes[1]["body"].append(  # type: ignore[index]
            _step(
                "review",
                inputs=[{"name": "notes", "from": "item-artifact:coder.notes"}],
            )
        )
    elif mutation == "export":
        nodes[1]["exports"] = [  # type: ignore[index]
            {"id": "notes", "from": "coder.notes", "cardinality": "collection"}
        ]
    elif mutation == "hand-built-dto":
        return value
    else:
        raise AssertionError(f"unknown characterization mutation: {mutation}")
    return value


def _hand_built_dto(payload: dict[str, object]) -> WorkflowConfig:
    parsed = parse_workflow_config(payload)
    loop = parsed.nodes[1]
    assert isinstance(loop, LoopConfig)
    rebuilt_loop = LoopConfig(
        type=loop.type,
        id=loop.id,
        source=LoopSource(source=loop.source.source, provider=loop.source.provider),
        max_items=loop.max_items,
        controller=loop.controller,
        body=tuple(loop.body),
        exports=tuple(loop.exports),
    )
    return WorkflowConfig(
        schema_version=parsed.schema_version,
        id=parsed.id,
        profile=parsed.profile,
        limits=WorkflowLimits(**parsed.limits.__dict__) if hasattr(parsed.limits, "__dict__") else parsed.limits,
        nodes=(parsed.nodes[0], rebuilt_loop),
    )


def _capture(name: str, payload: dict[str, object]) -> dict[str, object]:
    before = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    input_digest = hashlib.sha256(before.encode("utf-8")).hexdigest()
    config = parse_workflow_config(payload)
    try:
        plan = normalize_workflow_config(config)
    except WorkflowConfigError as error:
        result: dict[str, object] = {
            "kind": "error",
            "exception_type": f"{type(error).__module__}.{type(error).__qualname__}",
            "diagnostics": [item.as_dict() for item in error.diagnostics],
        }
    else:
        result = {
            "kind": "plan",
            "digest": workflow_plan_digest(plan),
            "payload": workflow_plan_payload(plan),
        }
    after = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "name": name,
        "input_sha256": input_digest,
        "input_unchanged": before == after,
        "result": result,
    }


def _compact(observations: list[dict[str, object]]) -> list[dict[str, object]]:
    compact: list[dict[str, object]] = []
    for observation in observations:
        item: dict[str, object] = {"name": observation["name"]}
        if "input_sha256" in observation:
            item["input_sha256"] = observation["input_sha256"]
            item["input_unchanged"] = observation["input_unchanged"]
        result = observation["result"]
        assert isinstance(result, dict)
        compact_result: dict[str, object] = {
            "kind": result["kind"],
        }
        if result["kind"] == "plan":
            payload = result["payload"]
            assert isinstance(payload, dict)
            compact_result["digest"] = result["digest"]
            compact_result["payload_sha256"] = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
        else:
            compact_result["exception_type"] = result["exception_type"]
            compact_result["diagnostics"] = result["diagnostics"]
        item["result"] = compact_result
        compact.append(item)
    return compact


class WorkflowNormalizationCharacterizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_payload = json.loads(BASE_FIXTURE.read_text(encoding="utf-8"))
        cls.case_index = json.loads(CASE_INDEX.read_text(encoding="utf-8"))

    def observations(self) -> list[dict[str, object]]:
        observations: list[dict[str, object]] = []
        for case in self.case_index["cases"]:
            payload = _mutate(self.base_payload, case["mutation"])
            if case["mutation"] == "hand-built-dto":
                config = _hand_built_dto(payload)
                try:
                    plan = normalize_workflow_config(config)
                except WorkflowConfigError as error:
                    result: dict[str, object] = {
                        "kind": "error",
                        "exception_type": f"{type(error).__module__}.{type(error).__qualname__}",
                        "diagnostics": [item.as_dict() for item in error.diagnostics],
                    }
                else:
                    result = {
                        "kind": "plan",
                        "digest": workflow_plan_digest(plan),
                        "payload": workflow_plan_payload(plan),
                    }
                observations.append({"name": case["name"], "result": result})
            else:
                observations.append(_capture(case["name"], payload))
        return observations

    def test_fixture_index_covers_required_boundary_cases(self) -> None:
        names = {case["name"] for case in self.case_index["cases"]}
        self.assertEqual(
            names,
            {
                "valid",
                "duplicate-node",
                "undefined-reference",
                "forward-dependency",
                "cycle",
                "cross-scope",
                "cardinality",
                "nested-body",
                "export",
                "virtual-input",
                "hand-built-dto",
            },
        )

    def test_characterization_matches_fixed_baseline(self) -> None:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(baseline["schema_version"], "normalization-characterization.v1")
        self.assertEqual(_compact(self.observations()), baseline["cases"])

    def test_aliases_and_diagnostics_keep_identity(self) -> None:
        self.assertIs(normalize_workflow_config, normalise_workflow_config)
        self.assertIs(normalize_workflow_config, build_workflow_plan)
        error_case = _mutate(self.base_payload, "undefined-reference")
        with self.assertRaises(WorkflowConfigError) as context:
            normalize_workflow_config(parse_workflow_config(error_case))
        self.assertEqual(context.exception.code, "undefined_reference")
        self.assertEqual(context.exception.diagnostics[0].path, "/nodes/1/source/from")


if __name__ == "__main__":
    unittest.main()
