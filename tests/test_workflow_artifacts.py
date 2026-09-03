from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from scripts.workflow_config import (
    ArtifactManifestStore,
    ArtifactManifest,
    ArtifactOutputValidationError,
    ArtifactOutputValidator,
    ArtifactPathSafetyError,
    WorkflowConfigError,
    build_artifact_namespace_plan,
    fingerprint_artifact,
    normalize_workflow_config,
    parse_workflow_config,
    preflight_workflow_bounds,
    validate_artifact_manifest,
)


class WorkflowArtifactTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def read_payload(self) -> dict[str, object]:
        return json.loads(self.fixture.read_text(encoding="utf-8"))

    def plan_and_bounds(self, payload: dict[str, object] | None = None):
        plan = normalize_workflow_config(parse_workflow_config(payload or self.read_payload()))
        bounds = preflight_workflow_bounds(
            plan,
            {"kelpie.work_items.v1": [{"id": "api"}, {"id": "cli"}]},
        )
        return plan, bounds

    def test_loop_namespace_uses_stable_item_id_not_position(self) -> None:
        plan, bounds = self.plan_and_bounds()
        first = build_artifact_namespace_plan(plan, bounds)
        first_by_item = {
            item.item_id: item.relative_path
            for item in first.entries
            if item.item_id is not None
        }

        reordered = preflight_workflow_bounds(
            plan,
            {"kelpie.work_items.v1": [{"id": "cli"}, {"id": "api"}]},
        )
        second = build_artifact_namespace_plan(plan, reordered)
        second_by_item = {
            item.item_id: item.relative_path
            for item in second.entries
            if item.item_id is not None
        }

        self.assertEqual(first_by_item, second_by_item)
        self.assertEqual(
            [item.position for item in first.entries if item.item_id is not None],
            [0, 1],
        )
        self.assertEqual(
            [item.position for item in second.entries if item.item_id is not None],
            [0, 1],
        )
        self.assertNotIn("0000", " ".join(first.paths))
        self.assertNotIn("0001", " ".join(first.paths))

    def test_normalized_casefold_collision_is_rejected(self) -> None:
        payload = self.read_payload()
        second = deepcopy(payload["nodes"][0])  # type: ignore[index]
        second["id"] = "publish"  # type: ignore[index]
        second["outputs"][0]["id"] = "published"  # type: ignore[index]
        second["outputs"][0]["path"] = "RESULT.md"  # type: ignore[index]
        payload["nodes"][0]["outputs"][0]["path"] = "result.md"  # type: ignore[index]
        payload["nodes"].append(second)  # type: ignore[index]

        plan, bounds = self.plan_and_bounds(payload)
        with self.assertRaises(WorkflowConfigError) as context:
            build_artifact_namespace_plan(plan, bounds)
        self.assertIn("namespace_collision", {item.code for item in context.exception.diagnostics})

    def test_artifact_root_symlink_is_rejected_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            outside = base / "outside"
            outside.mkdir()
            root = base / "artifact-root"
            root.symlink_to(outside, target_is_directory=True)
            plan, bounds = self.plan_and_bounds()

            with self.assertRaises(WorkflowConfigError) as context:
                build_artifact_namespace_plan(plan, bounds, artifact_root=root)

            self.assertIn("unsafe_path", {item.code for item in context.exception.diagnostics})
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_runtime_recheck_rejects_post_validation_symlink_replacement(self) -> None:
        plan, bounds = self.plan_and_bounds()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts"
            namespaces = build_artifact_namespace_plan(plan, bounds, artifact_root=root)
            first = namespaces.entries[1]
            scope = root / first.scope_relative_path
            outside = Path(tmpdir) / "outside"
            outside.mkdir()
            scope.parent.mkdir(parents=True)
            scope.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ArtifactPathSafetyError):
                namespaces.recheck(root)

    def test_scope_lock_is_exclusive_and_removed_after_release(self) -> None:
        plan, bounds = self.plan_and_bounds()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts"
            namespaces = build_artifact_namespace_plan(plan, bounds, artifact_root=root)
            scope = namespaces.entries[1]
            store = ArtifactManifestStore(root)
            with store.locked_scope(scope, owner="test") as scope_path:
                self.assertTrue((scope_path / ".artifact-scope.lock").is_file())
                with self.assertRaises(RuntimeError):
                    with store.scope_lock(scope, owner="nested"):
                        pass
            self.assertFalse((root / scope.scope_relative_path / ".artifact-scope.lock").exists())

    def test_required_output_manifest_records_identity_and_rejects_stale_output(self) -> None:
        plan, bounds = self.plan_and_bounds()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts"
            namespaces = build_artifact_namespace_plan(plan, bounds, artifact_root=root)
            validator = ArtifactOutputValidator(
                root,
                namespaces.entries[:1],
                run_identity="run-1",
            )
            target = root / namespaces.entries[0].relative_path
            target.parent.mkdir(parents=True)
            target.write_text("fresh\n", encoding="utf-8")

            manifest = validator.validate_all()
            entry = manifest.entries[0]
            self.assertEqual(entry.run_identity, "run-1")
            self.assertEqual(entry.node_instance_id, namespaces.entries[0].node_instance_id)
            self.assertEqual(entry.producer_node_id, namespaces.entries[0].producer_node_id)
            self.assertEqual(entry.item_id, namespaces.entries[0].item_id)
            self.assertEqual(entry.kind, "file")
            self.assertEqual(entry.freshness, entry.freshness.lower())

            store = ArtifactManifestStore(root)
            manifest_path = store.write(manifest)
            self.assertTrue(manifest_path.is_file())
            validate_artifact_manifest(root, store.read(), expected_namespaces=namespaces.entries[:1])

            target.write_text("changed\n", encoding="utf-8")
            with self.assertRaises(ArtifactOutputValidationError):
                validate_artifact_manifest(root, manifest, expected_namespaces=namespaces.entries[:1])

    def test_missing_and_wrong_kind_outputs_fail_closed(self) -> None:
        plan, bounds = self.plan_and_bounds()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts"
            namespaces = build_artifact_namespace_plan(plan, bounds, artifact_root=root)
            validator = ArtifactOutputValidator(
                root,
                namespaces.entries[:1],
                run_identity="run-1",
            )
            with self.assertRaises(ArtifactOutputValidationError):
                validator.validate_all()

            target = root / namespaces.entries[0].relative_path
            target.parent.mkdir(parents=True)
            target.mkdir()
            with self.assertRaises(ArtifactOutputValidationError):
                validator.validate_all()

    def test_fingerprint_rejects_symlink_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts"
            root.mkdir()
            outside = Path(tmpdir) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (root / "output.txt").symlink_to(outside)
            with self.assertRaises(ArtifactPathSafetyError):
                fingerprint_artifact(root, "output.txt")

    def test_manifest_rejects_normalized_or_overlapping_paths(self) -> None:
        plan, bounds = self.plan_and_bounds()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "artifacts"
            namespaces = build_artifact_namespace_plan(plan, bounds, artifact_root=root)
            target = root / namespaces.entries[0].relative_path
            validator = ArtifactOutputValidator(
                root,
                namespaces.entries[:1],
                run_identity="run-1",
            )
            target.parent.mkdir(parents=True)
            target.write_text("fresh\n", encoding="utf-8")
            entry = validator.validate_all().entries[0]
            collision = replace(
                entry,
                node_instance_id="nodes/other",
                output_id="other",
                relative_path=entry.relative_path.upper(),
            )

            with self.assertRaises(ValueError):
                ArtifactManifest(run_identity="run-1", entries=(entry, collision))

    def test_runtime_guard_rejects_drive_and_traversal_syntax(self) -> None:
        guard = ArtifactManifestStore(Path(tempfile.gettempdir()) / "kelpie-artifact-test").guard
        with self.assertRaises(ArtifactPathSafetyError):
            guard.validate("C:/outside")
        with self.assertRaises(ArtifactPathSafetyError):
            guard.validate("nested/../outside")


if __name__ == "__main__":
    unittest.main()
