from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


SCHEMA_VERSION = "1.0"
INTERVENTION_ACTIONS = (
    "request-changes",
    "provide-input",
    "approve",
    "retry",
    "reopen",
    "abort",
)
ACTIONS_REQUIRING_PROMPT = frozenset(
    {"request-changes", "provide-input", "approve", "reopen"}
)
MAX_INTERVENTION_PROMPT_CHARS = 16_000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_ACTION_ALIASES = {
    "request_changes": "request-changes",
    "provide_input": "provide-input",
}

_REASON_ACTIONS = {
    "high_severity_unresolved": ("request-changes", "reopen", "abort"),
    "max_iterations_reached": ("request-changes", "reopen", "abort"),
    "required_checks_unresolved": ("request-changes", "retry", "abort"),
    "required_tests_unresolved": ("request-changes", "retry", "abort"),
    "required_input_unavailable": ("provide-input", "abort"),
    "unresolved_findings": ("request-changes", "provide-input", "reopen", "abort"),
    "non_convergent": ("request-changes", "provide-input", "reopen", "abort"),
    "critical_risk_requires_decision": ("provide-input", "approve", "abort"),
    "architectural_decision_required": ("provide-input", "approve", "abort"),
    "destructive_change_approval_required": ("approve", "provide-input", "abort"),
    "dependency_approval_required": ("approve", "provide-input", "abort"),
    "permission_change_required": ("approve", "provide-input", "abort"),
    "authority_required": ("approve", "provide-input", "abort"),
    "external_send_approval_required": ("approve", "abort"),
    "external_publish_approval_required": ("approve", "abort"),
    "material_plan_deviation": ("request-changes", "reopen", "abort"),
    "validation_information_missing": ("provide-input", "request-changes", "abort"),
    "artifact_invalid": ("retry", "reopen", "abort"),
    "execution_error": ("retry", "reopen", "abort"),
    "experiment_not_executable": ("provide-input", "retry", "abort"),
    "scope_undefined": ("provide-input", "reopen", "abort"),
    "success_criteria_undefined": ("provide-input", "reopen", "abort"),
    "unresolved_design_dependency": ("provide-input", "reopen", "abort"),
}


@dataclass(frozen=True)
class HumanIntervention:
    request_id: str
    phase: str
    owner_phase: str
    action: str
    prompt: str | None
    prompt_ref: str | None
    response_ref: str
    loop_from_item: str | None = None


def normalize_action(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("human intervention action must be a string")
    normalized = value.strip().lower()
    normalized = _ACTION_ALIASES.get(normalized, normalized)
    if normalized not in INTERVENTION_ACTIONS:
        choices = ", ".join(INTERVENTION_ACTIONS)
        raise ValueError(f"unsupported human intervention action: {value!r}; choose one of {choices}")
    return normalized


def validate_prompt(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("human intervention prompt must be a non-empty string")
    if len(value) > MAX_INTERVENTION_PROMPT_CHARS:
        raise ValueError(
            "human intervention prompt is too long: "
            f"{len(value)} characters (maximum {MAX_INTERVENTION_PROMPT_CHARS})"
        )
    return value.strip()


def available_actions(reason_code: str, decision: str) -> tuple[str, ...]:
    if decision not in {"pause", "fail"}:
        raise ValueError("human intervention requests require a pause or fail decision")
    actions = _REASON_ACTIONS.get(reason_code)
    if actions is not None:
        return actions
    return ("retry", "reopen", "abort") if decision == "fail" else ("provide-input", "abort")


def intervention_kind(reason_code: str) -> str:
    if reason_code in {
        "high_severity_unresolved",
        "max_iterations_reached",
        "required_checks_unresolved",
        "required_tests_unresolved",
        "unresolved_findings",
        "non_convergent",
        "material_plan_deviation",
    }:
        return "finding"
    if reason_code in {
        "critical_risk_requires_decision",
        "architectural_decision_required",
        "destructive_change_approval_required",
        "dependency_approval_required",
        "permission_change_required",
        "authority_required",
        "external_send_approval_required",
        "external_publish_approval_required",
    }:
        return "authority"
    if reason_code in {"artifact_invalid", "execution_error"}:
        return "operational"
    return "input"


def build_request_payload(
    *,
    request_id: str,
    phase: str,
    decision: str,
    reason_code: str,
    summary: str,
    resume_condition: str | None,
    outcome_path: str,
    outcome_sha256: str,
    evidence_refs: list[str],
    created_at: str,
    owner_phase: str | None = None,
) -> dict[str, object]:
    actions = available_actions(reason_code, decision)
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "phase": phase,
        "decision": decision,
        "reason_code": reason_code,
        "intervention_kind": intervention_kind(reason_code),
        "owner_phase": owner_phase or phase,
        "available_actions": list(actions),
        "summary": summary,
        "resume_condition": resume_condition
        or "Correct the blocking condition and rerun the affected phase.",
        "outcome_path": outcome_path,
        "outcome_sha256": outcome_sha256,
        "evidence_refs": list(evidence_refs),
        "created_at": created_at,
    }


def build_response_payload(
    *,
    response_id: str,
    request_id: str,
    phase: str,
    target_phase: str,
    action: str,
    prompt_ref: str | None,
    prompt_sha256: str | None,
    request_sha256: str,
    actor: str,
    created_at: str,
    loop_from_item: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "response_id": response_id,
        "request_id": request_id,
        "phase": phase,
        "target_phase": target_phase,
        "action": normalize_action(action),
        "prompt_ref": prompt_ref,
        "prompt_sha256": prompt_sha256,
        "request_sha256": request_sha256,
        "actor": actor,
        "created_at": created_at,
    }
    if loop_from_item is not None:
        payload["loop_from_item"] = loop_from_item
    return payload


def validate_request_payload(raw: Mapping[str, object]) -> dict[str, object]:
    required = {
        "schema_version",
        "request_id",
        "phase",
        "decision",
        "reason_code",
        "intervention_kind",
        "owner_phase",
        "available_actions",
        "summary",
        "resume_condition",
        "outcome_path",
        "outcome_sha256",
        "evidence_refs",
        "created_at",
    }
    unknown = set(raw) - required
    missing = required - set(raw)
    if unknown:
        raise ValueError(
            "human intervention request has unsupported keys: "
            + ", ".join(sorted(unknown))
        )
    if missing:
        raise ValueError(
            "human intervention request is missing keys: " + ", ".join(sorted(missing))
        )
    if raw["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported human intervention schema: {raw['schema_version']}")
    for field in ("request_id", "phase", "reason_code", "intervention_kind", "owner_phase", "created_at"):
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ValueError(f"human intervention request {field} must be a non-empty string")
    if raw["decision"] not in {"pause", "fail"}:
        raise ValueError("human intervention request decision must be pause or fail")
    actions = raw["available_actions"]
    if not isinstance(actions, list) or not actions:
        raise ValueError("human intervention request available_actions must be a non-empty list")
    normalized_actions = [normalize_action(action) for action in actions]
    if len(set(normalized_actions)) != len(normalized_actions):
        raise ValueError("human intervention request available_actions must be unique")
    if not isinstance(raw["summary"], str) or not raw["summary"].strip():
        raise ValueError("human intervention request summary must be a non-empty string")
    if not isinstance(raw["resume_condition"], str) or not raw["resume_condition"].strip():
        raise ValueError("human intervention request resume_condition must be a non-empty string")
    if not isinstance(raw["outcome_path"], str):
        raise ValueError("human intervention request outcome_path must be a string")
    outcome_path = Path(raw["outcome_path"])
    if outcome_path.is_absolute() or not outcome_path.parts or ".." in outcome_path.parts:
        raise ValueError("human intervention request outcome_path must stay within the artifact scope")
    if not isinstance(raw["outcome_sha256"], str) or not _SHA256_PATTERN.fullmatch(raw["outcome_sha256"]):
        raise ValueError("human intervention request outcome_sha256 must be lowercase SHA-256")
    evidence_refs = raw["evidence_refs"]
    if not isinstance(evidence_refs, list) or any(not isinstance(item, str) for item in evidence_refs):
        raise ValueError("human intervention request evidence_refs must be a list of strings")
    normalized = dict(raw)
    normalized["available_actions"] = normalized_actions
    return normalized


def validate_action_for_request(request: Mapping[str, object], action: str) -> str:
    normalized = normalize_action(action)
    actions = request.get("available_actions")
    if not isinstance(actions, list) or normalized not in actions:
        allowed = ", ".join(str(item) for item in actions or ())
        raise ValueError(f"action {normalized!r} is not allowed for this request; choose one of {allowed}")
    return normalized


def dump_payload(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
