from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.plan_comprehension import (
    AdjudicationResult,
    ArtifactEntry,
    ArtifactInput,
    CapabilityProfile,
    InputManifest,
    PlanCheckSpec,
    ProbeResult,
    SectionEntry,
    build_data_envelope,
    build_findings,
    build_snapshot,
    build_structural_findings,
    capability_profile_from_command,
    evaluate_fixture_results,
    finding_fingerprint,
    finding_id,
    next_iteration_dir,
    parse_json_payload,
    render_advisory_report,
    run_plan_check,
    run_probe,
    sha256_text,
    validate_evidence,
    validate_reconstruction_shape,
)


def spec_for(*paths: str, classification: str = "external-safe") -> PlanCheckSpec:
    return PlanCheckSpec(
        schema_version="1.0",
        step_name="plan_comprehension_check",
        input_artifacts=tuple(
            ArtifactInput(
                artifact_id=Path(path).stem,
                relative_path=path,
                classification=classification,
            )
            for path in paths
        ),
        capability_profile="weak-plan-reader-v1",
        input_mode="prose_only",
        advisory_only=True,
    )


def sourced(value: object, manifest, section_index: int = 0) -> dict[str, object]:
    artifact = manifest.artifacts[0]
    section = artifact.sections[section_index]
    return {
        "value": value,
        "status": "explicit",
        "source_refs": [
            {
                "artifact_id": artifact.artifact_id,
                "section_id": section.section_id,
                "artifact_sha256": artifact.sha256,
                "evidence": section.heading,
            }
        ],
    }


def reconstruction(manifest) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "tasks": [
            {
                "id": sourced("T1", manifest),
                "summary": sourced("Implement task", manifest),
                "input_artifacts": [],
                "context_inputs": [],
                "files": [],
                "prerequisite_tasks": [],
                "outputs": [],
                "acceptance_criteria": [],
            }
        ],
        "non_goals": [],
        "assumptions": [],
        "decisions": [],
        "uncertainties": [],
    }


class PlanCheckSchemaTests(unittest.TestCase):
    def test_adjudication_requires_known_findings_and_matching_snapshot(self) -> None:
        payload = {
            "schema_version": "1.0",
            "input_snapshot_id": "snapshot-1",
            "findings": [
                {
                    "finding_id": "PC-0001",
                    "verdict": "rejected",
                    "evidence_refs": ["05-work-breakdown.md#task"],
                    "rationale": "The source already defines the dependency.",
                    "action": "no_change",
                }
            ],
            "plan_modified": False,
            "modified_artifacts": [],
            "unresolved_reasons": [],
        }

        result = AdjudicationResult.from_dict(
            payload,
            expected_snapshot_id="snapshot-1",
            expected_finding_ids={"PC-0001"},
        )

        self.assertFalse(result.plan_modified)
        self.assertEqual(result.findings[0].verdict, "rejected")

    def test_adjudication_rejects_unknown_finding(self) -> None:
        payload = {
            "schema_version": "1.0",
            "input_snapshot_id": "snapshot-1",
            "findings": [
                {
                    "finding_id": "PC-9999",
                    "verdict": "accepted",
                    "evidence_refs": [],
                    "rationale": "Invented finding.",
                    "action": "plan_modified",
                }
            ],
            "plan_modified": True,
            "modified_artifacts": ["05-work-breakdown.md"],
            "unresolved_reasons": [],
        }

        with self.assertRaisesRegex(ValueError, "unknown finding"):
            AdjudicationResult.from_dict(
                payload,
                expected_snapshot_id="snapshot-1",
                expected_finding_ids={"PC-0001"},
            )

    def test_finding_id_is_stable_for_same_finding(self) -> None:
        finding = {
            "classification": "missing",
            "observed": "Task T2 has no dependency.",
            "artifact_id": "05-work-breakdown",
            "section_id": "task-t2",
        }

        self.assertEqual(finding_id(finding), finding_id(dict(finding)))
        self.assertRegex(finding_id(finding), r"^PC-[0-9A-F]{12}$")

    def test_spec_rejects_unknown_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported keys"):
            PlanCheckSpec.from_dict(
                {
                    "schema_version": "1.0",
                    "step_name": "plan_comprehension_check",
                    "input_artifacts": [],
                    "capability_profile": "weak-plan-reader-v1",
                    "input_mode": "prose_only",
                    "advisory_only": True,
                    "inputs": [],
                }
            )

    def test_spec_rejects_non_object_artifact_entry(self) -> None:
        with self.assertRaisesRegex(ValueError, "every input_artifacts entry"):
            PlanCheckSpec.from_dict(
                {
                    "schema_version": "1.0",
                    "step_name": "plan_comprehension_check",
                    "input_artifacts": ["plan.md"],
                    "capability_profile": "weak-plan-reader-v1",
                    "input_mode": "prose_only",
                    "advisory_only": True,
                }
            )

    def test_reconstruction_rejects_ambiguous_task_inputs_field(self) -> None:
        payload = {
            "schema_version": "1.0",
            "tasks": [
                {
                    "id": {},
                    "summary": {},
                    "inputs": [],
                    "input_artifacts": [],
                    "context_inputs": [],
                    "files": [],
                    "prerequisite_tasks": [],
                    "outputs": [],
                    "acceptance_criteria": [],
                }
            ],
            "non_goals": [],
            "assumptions": [],
            "decisions": [],
            "uncertainties": [],
        }
        with self.assertRaisesRegex(ValueError, "unsupported keys"):
            validate_reconstruction_shape(payload)

    def test_reconstruction_requires_sourced_values(self) -> None:
        payload = {
            "schema_version": "1.0",
            "tasks": [
                {
                    "id": "T1",
                    "summary": "Implement",
                    "input_artifacts": [],
                    "context_inputs": [],
                    "files": [],
                    "prerequisite_tasks": [],
                    "outputs": [],
                    "acceptance_criteria": [],
                }
            ],
            "non_goals": [],
            "assumptions": [],
            "decisions": [],
            "uncertainties": [],
        }
        with self.assertRaisesRegex(ValueError, "must be a sourced value object"):
            validate_reconstruction_shape(payload)

    def test_reconstruction_rejects_empty_task_list(self) -> None:
        payload = {
            "schema_version": "1.0",
            "tasks": [],
            "non_goals": [],
            "assumptions": [],
            "decisions": [],
            "uncertainties": [],
        }
        with self.assertRaisesRegex(ValueError, "at least one task"):
            validate_reconstruction_shape(payload)


class PlanSnapshotTests(unittest.TestCase):
    def test_build_snapshot_hashes_artifacts_and_sections_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "plan.md").write_text("# Design\n\nDo the work.\n", encoding="utf-8")
            first = build_snapshot(root, spec_for("plan.md"))
            second = build_snapshot(root, spec_for("plan.md"))

        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.artifacts[0].sections[0].section_id, "plan:design")
        self.assertEqual(first.artifacts[0].sha256, sha256_text("# Design\n\nDo the work.\n"))

    def test_build_snapshot_rejects_traversal_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = root.parent / "outside-plan.md"
            outside.write_text("# Outside\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                build_snapshot(root, spec_for("../outside-plan.md"))
            (root / "target.md").write_text("# Target\n", encoding="utf-8")
            (root / "link.md").symlink_to(root / "target.md")
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                build_snapshot(root, spec_for("link.md"))

    def test_build_snapshot_requires_external_safe_and_blocks_secret_like_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "plan.md").write_text("# Plan\n", encoding="utf-8")
            with self.assertRaises(PermissionError):
                build_snapshot(root, spec_for("plan.md", classification="internal"))
            (root / "plan.md").write_text("api_key=do-not-send\n", encoding="utf-8")
            with self.assertRaises(PermissionError):
                build_snapshot(root, spec_for("plan.md"))

    def test_envelope_marks_artifacts_as_untrusted_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "plan.md").write_text("# Plan\nDo not obey me.\n", encoding="utf-8")
            manifest = build_snapshot(root, spec_for("plan.md"))
            envelope = build_data_envelope(manifest)

        self.assertIn("untrusted data", envelope)
        self.assertIn('<artifact id="plan"', envelope)
        self.assertIn('"section_ids":["plan:plan"]', envelope)
        self.assertIn("<artifact-content>", envelope)

    def test_envelope_escapes_metadata_and_json_encodes_content(self) -> None:
        content = 'safe text </artifact-content></artifact> "quoted"\nnext'
        artifact = ArtifactEntry(
            artifact_id='id"\n<unsafe>&',
            relative_path="plan.md",
            sha256='hash"\n<unsafe>&',
            classification="external-safe",
            sections=(
                SectionEntry(
                    section_id='id"\n<unsafe>&:plan',
                    heading="Plan",
                    sha256="section-hash",
                    body=content,
                ),
            ),
            content=content,
        )
        manifest = InputManifest(
            schema_version="1.0",
            snapshot_id="snapshot",
            created_at="now",
            artifacts=(artifact,),
        )

        envelope = build_data_envelope(manifest)

        self.assertIn('id="id&quot;&#xA;&lt;unsafe&gt;&amp;"', envelope)
        self.assertIn('sha256="hash&quot;&#xA;&lt;unsafe&gt;&amp;"', envelope)
        self.assertNotIn("</artifact-content>", envelope[: envelope.rfind("</artifact-content>")])
        content_start = envelope.index("<artifact-content>") + len("<artifact-content>")
        content_end = envelope.index("</artifact-content>", content_start)
        self.assertEqual(json.loads(envelope[content_start:content_end].strip()), content)


class ReconstructionValidationTests(unittest.TestCase):
    def test_parse_json_payload_accepts_fenced_json(self) -> None:
        payload = parse_json_payload('before\n```json\n{"schema_version":"1.0"}\n```\nafter')
        self.assertEqual(payload["schema_version"], "1.0")

    def test_parse_json_payload_does_not_select_nested_object_from_malformed_json(self) -> None:
        with self.assertRaisesRegex(ValueError, "No JSON object"):
            parse_json_payload('{"schema_version":"1.0","tasks":[{"id":{}}]}}')

    def test_parse_json_payload_reports_json_error_location(self) -> None:
        with self.assertRaisesRegex(ValueError, r"No JSON object.*line 1, column"):
            parse_json_payload('{"schema_version":"1.0","tasks":[]}}')

    def test_validate_evidence_accepts_valid_source_and_rejects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "plan.md").write_text("# Design\nImplement task.\n", encoding="utf-8")
            manifest = build_snapshot(root, spec_for("plan.md"))
            payload = reconstruction(manifest)
            valid = validate_evidence(payload, manifest)
            payload["tasks"][0]["id"]["source_refs"][0]["artifact_sha256"] = "bad"
            invalid = validate_evidence(payload, manifest)

        self.assertTrue(valid["valid"])
        self.assertFalse(invalid["valid"])
        self.assertIn("artifact hash mismatch", invalid["errors"][0])

    def test_validate_evidence_rejects_explicit_without_source_and_missing_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "plan.md").write_text("# Design\n", encoding="utf-8")
            manifest = build_snapshot(root, spec_for("plan.md"))
            payload = reconstruction(manifest)
            payload["tasks"][0]["id"] = {"value": "T1", "status": "explicit", "source_refs": []}
            payload["tasks"][0]["summary"] = {"value": "invented", "status": "missing", "source_refs": []}
            result = validate_evidence(payload, manifest)

        self.assertFalse(result["valid"])
        self.assertEqual(len(result["errors"]), 2)


class ProbeAndFindingTests(unittest.TestCase):
    def test_capability_profile_is_derived_from_effective_command(self) -> None:
        profile = capability_profile_from_command(
            [
                "/opt/bin/agy",
                "--model",
                "gemini-test",
                "--effort",
                "medium",
                "--mode",
                "plan",
                "--print-timeout",
                "90s",
            ]
        )
        self.assertEqual(profile.runner, "agy")
        self.assertEqual(profile.model, "gemini-test")
        self.assertEqual(profile.effort, "medium")
        self.assertEqual(profile.timeout_seconds, 90)

    def test_capability_profile_reads_codex_effort_and_sandbox(self) -> None:
        profile = capability_profile_from_command(
            [
                "codex",
                "exec",
                "--model",
                "gpt-5.4-mini",
                "-c",
                'model_reasoning_effort="low"',
                "--sandbox",
                "read-only",
                "-",
            ]
        )

        self.assertEqual(profile.runner, "codex")
        self.assertEqual(profile.model, "gpt-5.4-mini")
        self.assertEqual(profile.effort, "low")
        self.assertEqual(profile.mode, "read-only")

    def test_run_probe_uses_stdin_and_profile_arguments(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        runner = Mock(return_value=completed)
        result = run_probe(CapabilityProfile(), "prompt", "envelope", run=runner)

        args, kwargs = runner.call_args
        self.assertEqual(args[0][0], "codex")
        self.assertIn("gpt-5.4-mini", args[0])
        self.assertIn("read-only", args[0])
        self.assertEqual(kwargs["input"], "envelope")
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(result.returncode, 0)

    def test_run_probe_decodes_bytes_from_timeout(self) -> None:
        runner = Mock(
            side_effect=subprocess.TimeoutExpired(
                cmd=["codex"],
                timeout=180,
                output=b"partial output",
                stderr=b"timed out",
            )
        )

        result = run_probe(CapabilityProfile(), "prompt", "envelope", run=runner)

        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "partial output")
        self.assertEqual(result.stderr, "timed out")
        self.assertTrue(result.timed_out)

    def test_finding_fingerprint_is_deterministic(self) -> None:
        finding = {
            "classification": "ambiguous",
            "affected_artifacts": ["work-breakdown"],
            "observed": " Inputs are ambiguous ",
            "source_refs": [],
        }
        self.assertEqual(finding_fingerprint(finding), finding_fingerprint(dict(finding)))

    def test_semantic_findings_remain_unverified_without_valid_sources(self) -> None:
        payload = {
            "findings": [
                {
                    "classification": "ambiguous",
                    "severity": "medium",
                    "affected_artifacts": ["work-breakdown"],
                    "observed": "Inputs are ambiguous.",
                    "source_refs": [],
                }
            ]
        }
        findings = build_findings(payload, {"valid": True})
        self.assertEqual(findings[0]["verification"], "unverified")
        self.assertTrue(findings[0]["requires_human_approval"])

    def test_semantic_findings_remain_unverified_even_when_reconstruction_evidence_is_valid(self) -> None:
        payload = {
            "findings": [
                {
                    "classification": "ambiguous",
                    "severity": "medium",
                    "affected_artifacts": ["work-breakdown"],
                    "observed": "Inputs are ambiguous.",
                    "source_refs": [{"artifact_id": "work-breakdown"}],
                }
            ]
        }
        findings = build_findings(payload, {"valid": True})
        self.assertEqual(findings[0]["verification"], "unverified")

    def test_structural_findings_detect_missing_task_and_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "plan.md").write_text("# Plan\n", encoding="utf-8")
            (root / "work_items.json").write_text(
                json.dumps(
                    {
                        "version": "1.0",
                        "tasks": [
                            {"id": "t1", "dependencies": []},
                            {"id": "t2", "dependencies": ["t1"]},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = build_snapshot(root, spec_for("plan.md", "work_items.json"))
            plan_artifact = manifest.artifacts[0]
            plan_section = plan_artifact.sections[0]
            source_ref = {
                "artifact_id": plan_artifact.artifact_id,
                "section_id": plan_section.section_id,
                "artifact_sha256": plan_artifact.sha256,
                "evidence": plan_section.heading,
            }
            payload = {
                "tasks": [
                    {
                        "id": {"value": "t2", "status": "explicit", "source_refs": [source_ref]},
                        "prerequisite_tasks": [],
                    }
                ]
            }
            findings = build_structural_findings(payload, manifest)

        self.assertEqual(
            {(item["classification"], item["observed"]) for item in findings},
            {
                ("missing", "Task 't1' is missing from the reconstruction."),
                ("missing", "Task 't2' is missing dependency 't1'."),
            },
        )
        self.assertTrue(all(item["verification"] == "verified" for item in findings))

    def test_report_never_labels_no_findings_as_safe(self) -> None:
        report = render_advisory_report("completed_no_findings", [], "snapshot")
        self.assertIn("advisory comprehension signal", report)
        self.assertNotIn(" safe ", report.lower())
        self.assertIn("implementation readiness", report)


class PersistenceAndEvaluationTests(unittest.TestCase):
    def test_fixture_manifest_has_required_balanced_corpus(self) -> None:
        manifest_path = (
            Path(__file__).parent / "fixtures" / "plan_comprehension" / "manifest.json"
        )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        fixtures = payload["fixtures"]
        clean = [item for item in fixtures if item["expected_clean"]]
        defective = [item for item in fixtures if not item["expected_clean"]]
        prose_only = [item for item in fixtures if item["input_mode"] == "prose_only"]
        defect_types = {
            defect
            for item in defective
            for defect in item.get("defect_types", [])
        }

        self.assertEqual(len(clean), 10)
        self.assertEqual(len(defective), 10)
        self.assertGreaterEqual(len(prose_only), 10)
        self.assertEqual(
            defect_types,
            {
                "missing dependency",
                "contradictory scope",
                "unsupported assumption",
                "missing acceptance criteria",
                "ambiguous input/output",
                "unresolved blocking decision",
            },
        )
        self.assertEqual(payload["review_status"], "pending-independent-review")

    def test_next_iteration_dir_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            first = next_iteration_dir(Path(tmpdir))
            second = next_iteration_dir(Path(tmpdir))
        self.assertEqual(first.name, "0001")
        self.assertEqual(second.name, "0002")

    def test_dry_run_writes_prepared_status_without_invoking_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            result = run_plan_check(root, dry_run=True)
            status_files = list((root / "plan-check" / "iterations").glob("*/status.json"))

        self.assertEqual(result["status"], "prepared")
        self.assertEqual(len(status_files), 1)

    def test_dry_run_records_required_policy_in_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            run_plan_check(root, dry_run=True, advisory_only=False)
            spec = json.loads(
                (root / "plan-check" / "iterations" / "0001" / "spec.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertFalse(spec["advisory_only"])

    def test_operational_failure_has_no_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            (root / "05-work-breakdown.md").write_text("# Work Breakdown\n", encoding="utf-8")
            result = run_plan_check(root, command_template=["false"], allow_external_send=True)
            intent_path = next((root / "plan-check" / "iterations").glob("*/intent-record.json"))
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            summary = (root / "05a-plan-comprehension-check.md").read_text(encoding="utf-8")
        self.assertEqual(result["status"], "execution_error")
        self.assertEqual(result["findings"], [])
        self.assertEqual(intent["attempts"], 2)
        self.assertIn("execution_error", summary)

    def test_live_check_requires_explicit_external_send_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            result = run_plan_check(root, command_template=["false"])
        self.assertEqual(result["status"], "approval_required")
        self.assertEqual(result["findings"], [])

    def test_live_check_requires_design_and_work_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            result = run_plan_check(root, command_template=["false"], allow_external_send=True)
        self.assertEqual(result["status"], "spec_error")
        self.assertIn("05-work-breakdown.md", result["error"])

    def test_invalid_evidence_requires_human_review_instead_of_no_findings(self) -> None:
        probe_payload = {
            "schema_version": "1.0",
            "tasks": [
                {
                    "id": {"value": "T1", "status": "explicit", "source_refs": []},
                    "summary": {"value": "Implement", "status": "explicit", "source_refs": []},
                    "input_artifacts": [],
                    "context_inputs": [],
                    "files": [],
                    "prerequisite_tasks": [],
                    "outputs": [],
                    "acceptance_criteria": [],
                }
            ],
            "non_goals": [],
            "assumptions": [],
            "decisions": [],
            "uncertainties": [],
        }
        probe_result = Mock(
            returncode=0,
            stdout=json.dumps(probe_payload),
            stderr="",
            timed_out=False,
            command=("agy",),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            (root / "05-work-breakdown.md").write_text("# Work Breakdown\n", encoding="utf-8")
            with patch("scripts.plan_comprehension.run_probe", return_value=probe_result):
                result = run_plan_check(root, allow_external_send=True)
        self.assertEqual(result["status"], "needs_human_review")
        self.assertEqual(result["findings"], [])

    def test_schema_invalid_probe_is_retried_with_correction_prompt(self) -> None:
        invalid_payload = {
            "schema_version": "1.0",
            "tasks": [],
            "non_goals": [],
            "assumptions": [],
            "decisions": [],
            "uncertainties": [],
        }
        valid_payload = {
            "schema_version": "1.0",
            "tasks": [
                {
                    "id": {"value": "T1", "status": "missing", "source_refs": []},
                    "summary": {"value": "", "status": "missing", "source_refs": []},
                    "input_artifacts": [],
                    "context_inputs": [],
                    "files": [],
                    "prerequisite_tasks": [],
                    "outputs": [],
                    "acceptance_criteria": [],
                }
            ],
            "non_goals": [],
            "assumptions": [],
            "decisions": [],
            "uncertainties": [],
        }
        probe_results = [
            ProbeResult(
                returncode=0,
                stdout=json.dumps(invalid_payload),
                stderr="",
                timed_out=False,
                command=("copilot",),
            ),
            ProbeResult(
                returncode=0,
                stdout=json.dumps(valid_payload),
                stderr="",
                timed_out=False,
                command=("copilot",),
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            (root / "05-work-breakdown.md").write_text("# Work Breakdown\n", encoding="utf-8")
            with patch("scripts.plan_comprehension.run_probe", side_effect=probe_results) as probe:
                result = run_plan_check(root, allow_external_send=True)

        self.assertEqual(result["status"], "needs_human_review")
        self.assertEqual(probe.call_count, 2)
        self.assertIn("previous response was rejected", probe.call_args_list[1].args[1])
        self.assertIn("required reconstruction schema", probe.call_args_list[1].args[1])

    def test_malformed_probe_retry_records_diagnostic_and_each_attempt(self) -> None:
        malformed_output = '{"schema_version":"1.0","tasks":[]}}'
        valid_payload = {
            "schema_version": "1.0",
            "tasks": [
                {
                    "id": {"value": "T1", "status": "missing", "source_refs": []},
                    "summary": {"value": "", "status": "missing", "source_refs": []},
                    "input_artifacts": [],
                    "context_inputs": [],
                    "files": [],
                    "prerequisite_tasks": [],
                    "outputs": [],
                    "acceptance_criteria": [],
                }
            ],
            "non_goals": [],
            "assumptions": [],
            "decisions": [],
            "uncertainties": [],
        }
        probe_results = [
            ProbeResult(
                returncode=0,
                stdout=malformed_output,
                stderr="",
                timed_out=False,
                command=("copilot",),
            ),
            ProbeResult(
                returncode=0,
                stdout=json.dumps(valid_payload),
                stderr="",
                timed_out=False,
                command=("copilot",),
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            (root / "05-work-breakdown.md").write_text("# Work Breakdown\n", encoding="utf-8")
            with patch("scripts.plan_comprehension.run_probe", side_effect=probe_results) as probe:
                result = run_plan_check(root, allow_external_send=True)
            first_attempt = json.loads(
                (
                    root
                    / "plan-check"
                    / "iterations"
                    / "0001"
                    / "attempts"
                    / "0001"
                    / "result.json"
                ).read_text(encoding="utf-8")
            )
            second_attempt_exists = (
                root
                / "plan-check"
                / "iterations"
                / "0001"
                / "attempts"
                / "0002"
                / "result.json"
            ).exists()
            correction_prompt = probe.call_args_list[1].args[1]

        self.assertEqual(result["status"], "needs_human_review")
        self.assertEqual(first_attempt["status"], "invalid_output")
        self.assertIn("line 1, column", first_attempt["validation_error"])
        self.assertTrue(second_attempt_exists)
        self.assertIn("line 1, column", correction_prompt)
        self.assertNotIn(malformed_output, correction_prompt)

    def test_schema_invalid_probe_is_persisted_as_invalid_output_with_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            (root / "05-work-breakdown.md").write_text("# Work Breakdown\n", encoding="utf-8")
            probe_result = ProbeResult(
                returncode=0,
                stdout="not-json",
                stderr="",
                timed_out=False,
                command=("copilot",),
            )
            with patch("scripts.plan_comprehension.run_probe", return_value=probe_result):
                result = run_plan_check(root, allow_external_send=True)
            iteration = root / "plan-check" / "iterations" / "0001"
            status = json.loads((iteration / "status.json").read_text(encoding="utf-8"))
            raw_output = (iteration / "raw-output.txt").read_text(encoding="utf-8")

        self.assertEqual(result["status"], "invalid_output")
        self.assertEqual(status["status"], "invalid_output")
        self.assertEqual(raw_output, "not-json")

    def test_input_mutation_during_probe_marks_result_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            design = root / "04-solution-design.md"
            design.write_text("# Design\n", encoding="utf-8")
            (root / "05-work-breakdown.md").write_text("# Work Breakdown\n", encoding="utf-8")

            def mutate_input(*args, **kwargs):
                design.write_text("api_key=became-sensitive\n", encoding="utf-8")
                return ProbeResult(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "schema_version": "1.0",
                            "tasks": [
                                {
                                    "id": {"value": "T1", "status": "explicit", "source_refs": []},
                                    "summary": {"value": "Implement", "status": "explicit", "source_refs": []},
                                    "input_artifacts": [],
                                    "context_inputs": [],
                                    "files": [],
                                    "prerequisite_tasks": [],
                                    "outputs": [],
                                    "acceptance_criteria": [],
                                }
                            ],
                            "non_goals": [],
                            "assumptions": [],
                            "decisions": [],
                            "uncertainties": [],
                        }
                    ),
                    stderr="",
                    timed_out=False,
                    command=("agy",),
                )

            with patch("scripts.plan_comprehension.run_probe", side_effect=mutate_input):
                result = run_plan_check(root, allow_external_send=True)
        self.assertEqual(result["status"], "stale_input")

    def test_timeout_result_is_persisted_as_execution_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "04-solution-design.md").write_text("# Design\n", encoding="utf-8")
            (root / "05-work-breakdown.md").write_text("# Work Breakdown\n", encoding="utf-8")
            timed_out = ProbeResult(
                returncode=124,
                stdout=b"partial output",  # type: ignore[arg-type]
                stderr=b"timed out",  # type: ignore[arg-type]
                timed_out=True,
                command=("codex",),
            )
            with patch("scripts.plan_comprehension.run_probe", return_value=timed_out):
                result = run_plan_check(root, allow_external_send=True)
            status = json.loads(
                (root / "plan-check" / "iterations" / "0001" / "status.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "execution_error")
        self.assertTrue(result["timed_out"])
        self.assertEqual(status["status"], "execution_error")
        self.assertTrue(status["timed_out"])

    def test_evaluation_metrics_and_baseline_invalidation(self) -> None:
        fixtures = [
            {
                "id": "clean",
                "input_mode": "prose_only",
                "expected_findings": [],
                "actual_findings": [],
            },
            {
                "id": "defect",
                "input_mode": "copy_assisted",
                "expected_findings": [{"classification": "missing"}],
                "actual_findings": [{"classification": "missing", "severity": "high"}],
            },
        ]
        result = evaluate_fixture_results(fixtures, "profile", "schema", "prompt")
        stale = evaluate_fixture_results(
            fixtures,
            "profile",
            "schema",
            "new-prompt",
            baseline={"profile_hash": "profile", "schema_hash": "schema", "prompt_hash": "old-prompt"},
        )
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(result["fixture_count"], 2)
        self.assertFalse(stale["baseline_valid"])

    def test_evaluation_does_not_report_perfect_precision_when_expected_findings_are_missed(self) -> None:
        result = evaluate_fixture_results(
            [
                {
                    "id": "defect",
                    "input_mode": "prose_only",
                    "expected_findings": [{"classification": "missing"}],
                    "actual_findings": [],
                }
            ],
            "profile",
            "schema",
            "prompt",
        )
        self.assertEqual(result["precision"], 0.0)
        self.assertEqual(result["recall"], 0.0)


if __name__ == "__main__":
    unittest.main()
