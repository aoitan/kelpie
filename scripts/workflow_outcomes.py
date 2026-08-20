from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


DECISIONS = {"advance", "pause", "fail", "complete"}
PHASE_REASON_CODES = {
    "prototype_planning": {
        "plan_ready",
        "scope_undefined",
        "success_criteria_undefined",
        "artifact_invalid",
    },
    "prototyping": {
        "evidence_collected",
        "experiment_not_executable",
        "required_input_unavailable",
        "artifact_invalid",
    },
    "red_team_review": {
        "risks_recorded",
        "critical_risk_requires_decision",
        "authority_required",
        "artifact_invalid",
    },
    "solution_design": {
        "design_ready",
        "architectural_decision_required",
        "destructive_change_approval_required",
        "dependency_approval_required",
        "permission_change_required",
        "artifact_invalid",
    },
    "work_breakdown": {
        "work_items_ready",
        "unresolved_design_dependency",
        "artifact_invalid",
    },
    "plan_comprehension_check": {
        "completed_no_change",
        "completed_refined",
        "advisory_check_unavailable",
        "plan_check_waived",
        "unresolved_findings",
        "non_convergent",
        "invalid_output",
        "external_send_approval_required",
        "execution_error",
    },
    "implementation": {
        "implementation_ready_for_review",
        "material_plan_deviation",
        "required_permission_unavailable",
        "required_tests_unresolved",
        "artifact_invalid",
    },
    "review_fix_loop": {
        "review_converged",
        "high_severity_unresolved",
        "max_iterations_reached",
        "required_checks_unresolved",
        "artifact_invalid",
    },
    "pull_request": {
        "pr_draft_ready",
        "validation_information_missing",
        "external_publish_approval_required",
        "artifact_invalid",
        "external_operation_failed",
    },
}
REASON_DECISIONS = {
    "plan_ready": {"advance"},
    "scope_undefined": {"pause"},
    "success_criteria_undefined": {"pause"},
    "evidence_collected": {"advance"},
    "experiment_not_executable": {"pause"},
    "required_input_unavailable": {"pause"},
    "risks_recorded": {"advance"},
    "critical_risk_requires_decision": {"pause"},
    "authority_required": {"pause"},
    "design_ready": {"advance"},
    "architectural_decision_required": {"pause"},
    "destructive_change_approval_required": {"pause"},
    "dependency_approval_required": {"pause"},
    "permission_change_required": {"pause"},
    "work_items_ready": {"advance"},
    "unresolved_design_dependency": {"pause"},
    "completed_no_change": {"advance"},
    "completed_refined": {"advance"},
    "advisory_check_unavailable": {"advance"},
    "plan_check_waived": {"advance"},
    "unresolved_findings": {"pause"},
    "non_convergent": {"pause"},
    "invalid_output": {"pause"},
    "external_send_approval_required": {"pause"},
    "execution_error": {"fail"},
    "implementation_ready_for_review": {"advance"},
    "material_plan_deviation": {"pause"},
    "required_permission_unavailable": {"pause"},
    "required_tests_unresolved": {"pause"},
    "review_converged": {"advance"},
    "high_severity_unresolved": {"pause"},
    "max_iterations_reached": {"pause"},
    "required_checks_unresolved": {"pause"},
    "pr_draft_ready": {"complete"},
    "validation_information_missing": {"pause"},
    "external_publish_approval_required": {"pause"},
    "external_operation_failed": {"fail"},
    "artifact_invalid": {"fail"},
}
PHASE_REQUIRED_ARTIFACTS = {
    "prototype_planning": ("01-prototype-planning.md",),
    "prototyping": ("02-prototype-summary.md",),
    "red_team_review": ("03-red-team-review.md",),
    "solution_design": ("04-solution-design.md",),
    "work_breakdown": ("05-work-breakdown.md", "work_items.json"),
    "plan_comprehension_check": ("05a-plan-comprehension-check.md",),
    "implementation": ("06-implementation-notes.md",),
    "review_fix_loop": ("07-review-fix-loop.md",),
    "pull_request": ("08-pr-draft.md",),
}


@dataclass(frozen=True)
class PhaseOutcome:
    schema_version: str
    phase: str
    decision: str
    reason_code: str
    summary: str
    evidence_refs: tuple[str, ...]
    resume_condition: str | None
    artifact_digests: dict[str, str]

    @staticmethod
    def from_dict(raw: dict[str, object], *, expected_phase: str) -> "PhaseOutcome":
        required = {
            "schema_version",
            "phase",
            "decision",
            "reason_code",
            "summary",
            "evidence_refs",
            "resume_condition",
            "artifact_digests",
        }
        unknown = set(raw) - required
        missing = required - set(raw)
        if unknown:
            raise ValueError(f"phase outcome has unsupported keys: {', '.join(sorted(unknown))}")
        if missing:
            raise ValueError(f"phase outcome is missing keys: {', '.join(sorted(missing))}")
        if raw["schema_version"] != "1.0":
            raise ValueError(f"unsupported phase outcome schema: {raw['schema_version']}")
        if raw["phase"] != expected_phase:
            raise ValueError(f"phase outcome phase does not match {expected_phase}")
        decision = raw["decision"]
        if decision not in DECISIONS:
            raise ValueError(f"unsupported phase outcome decision: {decision}")
        reason_code = raw["reason_code"]
        if reason_code not in PHASE_REASON_CODES.get(expected_phase, set()):
            raise ValueError(f"unsupported reason code for {expected_phase}: {reason_code}")
        if decision not in REASON_DECISIONS.get(str(reason_code), set()):
            raise ValueError(
                f"decision {decision} is not allowed for reason code {reason_code}"
            )
        summary = raw["summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("phase outcome summary must be a non-empty string")
        evidence_refs = raw["evidence_refs"]
        if not isinstance(evidence_refs, list) or any(not isinstance(item, str) for item in evidence_refs):
            raise ValueError("phase outcome evidence_refs must be a list[str]")
        resume_condition = raw["resume_condition"]
        if resume_condition is not None and (
            not isinstance(resume_condition, str) or not resume_condition.strip()
        ):
            raise ValueError("phase outcome resume_condition must be null or a non-empty string")
        if decision == "pause" and resume_condition is None:
            raise ValueError("paused phase outcome requires a resume_condition")
        artifact_digests = raw["artifact_digests"]
        if not isinstance(artifact_digests, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in artifact_digests.items()
        ):
            raise ValueError("phase outcome artifact_digests must be a string mapping")
        return PhaseOutcome(
            schema_version="1.0",
            phase=expected_phase,
            decision=str(decision),
            reason_code=str(reason_code),
            summary=summary,
            evidence_refs=tuple(evidence_refs),
            resume_condition=resume_condition,
            artifact_digests=dict(artifact_digests),
        )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_artifact_path(artifact_root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"unsafe phase outcome artifact path: {relative_path}")
    path = artifact_root.joinpath(*candidate.parts)
    if path.is_symlink():
        raise ValueError(f"phase outcome artifact must not be a symlink: {relative_path}")
    try:
        path.resolve(strict=False).relative_to(artifact_root.resolve())
    except ValueError as exc:
        raise ValueError(f"unsafe phase outcome artifact path: {relative_path}") from exc
    return path


def validate_outcome_artifacts(artifact_root: Path, outcome: PhaseOutcome) -> None:
    if outcome.decision != "fail":
        for relative_path in PHASE_REQUIRED_ARTIFACTS[outcome.phase]:
            path = safe_artifact_path(artifact_root, relative_path)
            if not path.is_file():
                raise ValueError(
                    f"required phase artifact does not exist for {outcome.phase}: {relative_path}"
                )
    for relative_path in outcome.evidence_refs:
        path_text = relative_path.split("#", 1)[0]
        path = safe_artifact_path(artifact_root, path_text)
        if not path.is_file():
            raise ValueError(f"phase outcome evidence does not exist: {path_text}")
    for relative_path, expected_digest in outcome.artifact_digests.items():
        path = safe_artifact_path(artifact_root, relative_path)
        if not path.is_file():
            raise ValueError(f"phase outcome artifact does not exist: {relative_path}")
        if sha256_file(path) != expected_digest:
            raise ValueError(f"phase outcome artifact digest mismatch: {relative_path}")


def effective_decision(outcome: PhaseOutcome, machine_failure: str | None = None) -> str:
    if machine_failure is not None:
        return "fail"
    return outcome.decision


def persist_phase_outcome(
    artifact_root: Path,
    outcome: PhaseOutcome,
    *,
    state_metadata: dict[str, object] | None = None,
) -> None:
    payload = asdict(outcome)
    payload["evidence_refs"] = list(outcome.evidence_refs)
    history_dir = artifact_root / "phase-outcomes" / outcome.phase
    history_dir.mkdir(parents=True, exist_ok=True)
    index = 1
    while (history_dir / f"{index:04d}.json").exists():
        index += 1
    history_path = history_dir / f"{index:04d}.json"
    history_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    workflow_status = {
        "schema_version": "1.0",
        "status": {
            "pause": "paused",
            "fail": "failed",
            "complete": "completed",
            "advance": "running",
        }[outcome.decision],
        "phase": outcome.phase,
        "decision": outcome.decision,
        "reason_code": outcome.reason_code,
        "resume_condition": outcome.resume_condition,
        "outcome_path": str(history_path.relative_to(artifact_root)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if state_metadata:
        workflow_status.update(state_metadata)
    (artifact_root / "workflow-state.json").write_text(
        json.dumps(workflow_status, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
