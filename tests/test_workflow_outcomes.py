from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.workflow_outcomes import (
    PhaseOutcome,
    effective_decision,
    persist_phase_outcome,
    validate_outcome_artifacts,
)


class PhaseOutcomeTests(unittest.TestCase):
    def test_prototyping_negative_result_can_advance(self) -> None:
        outcome = PhaseOutcome.from_dict(
            {
                "schema_version": "1.0",
                "phase": "prototyping",
                "decision": "advance",
                "reason_code": "evidence_collected",
                "summary": "The hypothesis was falsified with reproducible evidence.",
                "evidence_refs": ["02-prototype-summary.md"],
                "resume_condition": None,
                "artifact_digests": {},
            },
            expected_phase="prototyping",
        )

        self.assertEqual(effective_decision(outcome), "advance")

    def test_unknown_phase_reason_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "reason code"):
            PhaseOutcome.from_dict(
                {
                    "schema_version": "1.0",
                    "phase": "red_team_review",
                    "decision": "pause",
                    "reason_code": "risk_found",
                    "summary": "A normal review finding exists.",
                    "evidence_refs": ["03-red-team-review.md"],
                    "resume_condition": "Remove every risk.",
                    "artifact_digests": {},
                },
                expected_phase="red_team_review",
            )

    def test_machine_failure_overrides_advance(self) -> None:
        outcome = PhaseOutcome.from_dict(
            {
                "schema_version": "1.0",
                "phase": "work_breakdown",
                "decision": "advance",
                "reason_code": "work_items_ready",
                "summary": "Tasks are ready.",
                "evidence_refs": ["05-work-breakdown.md"],
                "resume_condition": None,
                "artifact_digests": {},
            },
            expected_phase="work_breakdown",
        )

        self.assertEqual(effective_decision(outcome, machine_failure="invalid work items"), "fail")

    def test_artifact_validation_rejects_path_traversal(self) -> None:
        outcome = PhaseOutcome.from_dict(
            {
                "schema_version": "1.0",
                "phase": "implementation",
                "decision": "advance",
                "reason_code": "implementation_ready_for_review",
                "summary": "Implementation is ready.",
                "evidence_refs": ["../outside.txt"],
                "resume_condition": None,
                "artifact_digests": {},
            },
            expected_phase="implementation",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts"
            root.mkdir()
            (root / "06-implementation-notes.md").write_text("# Notes\n", encoding="utf-8")
            (Path(tmpdir) / "outside.txt").write_text("outside", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                validate_outcome_artifacts(root, outcome)

    def test_artifact_validation_requires_phase_output(self) -> None:
        outcome = PhaseOutcome.from_dict(
            {
                "schema_version": "1.0",
                "phase": "prototype_planning",
                "decision": "advance",
                "reason_code": "plan_ready",
                "summary": "Planning is complete.",
                "evidence_refs": [],
                "resume_condition": None,
                "artifact_digests": {},
            },
            expected_phase="prototype_planning",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "required phase artifact"):
                validate_outcome_artifacts(Path(tmpdir), outcome)

    def test_failed_outcome_can_record_missing_phase_artifact(self) -> None:
        outcome = PhaseOutcome.from_dict(
            {
                "schema_version": "1.0",
                "phase": "prototype_planning",
                "decision": "fail",
                "reason_code": "artifact_invalid",
                "summary": "The required artifact could not be produced.",
                "evidence_refs": [],
                "resume_condition": None,
                "artifact_digests": {},
            },
            expected_phase="prototype_planning",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            validate_outcome_artifacts(Path(tmpdir), outcome)

    def test_artifact_validation_rejects_symlink(self) -> None:
        outcome = PhaseOutcome.from_dict(
            {
                "schema_version": "1.0",
                "phase": "implementation",
                "decision": "advance",
                "reason_code": "implementation_ready_for_review",
                "summary": "Implementation is ready.",
                "evidence_refs": ["evidence.md"],
                "resume_condition": None,
                "artifact_digests": {},
            },
            expected_phase="implementation",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts"
            root.mkdir()
            (root / "06-implementation-notes.md").write_text("# Notes\n", encoding="utf-8")
            outside = Path(tmpdir) / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            (root / "evidence.md").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                validate_outcome_artifacts(root, outcome)

    def test_persisted_pause_records_state_and_history(self) -> None:
        outcome = PhaseOutcome.from_dict(
            {
                "schema_version": "1.0",
                "phase": "solution_design",
                "decision": "pause",
                "reason_code": "architectural_decision_required",
                "summary": "A high-impact decision is unresolved.",
                "evidence_refs": ["04-solution-design.md"],
                "resume_condition": "Select the persistence model.",
                "artifact_digests": {},
            },
            expected_phase="solution_design",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            persist_phase_outcome(root, outcome)
            state = json.loads((root / "workflow-state.json").read_text(encoding="utf-8"))
            history = list((root / "phase-outcomes" / "solution_design").glob("*.json"))

        self.assertEqual(state["status"], "paused")
        self.assertEqual(len(history), 1)


if __name__ == "__main__":
    unittest.main()
