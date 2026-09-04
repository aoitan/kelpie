from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.pipeline_executor import prepare_workflow_run
from scripts.workflow_config import (
    ArtifactManifest,
    CapabilityRegistry,
    RunStateStore,
    StaleResumeIdentityError,
    WorkflowConfigError,
    build_run_identity,
    normalize_workflow_config,
    parse_workflow_config,
)


class WorkflowResumeTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def read_payload(self) -> dict[str, object]:
        return json.loads(self.fixture.read_text(encoding="utf-8"))

    @staticmethod
    def write_resources(root: Path) -> None:
        for relative in (
            "prompts/plan.md",
            "prompts/code.md",
            "skills/plan.md",
            "skills/code.md",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {relative}\n", encoding="utf-8")

    def prepare(
        self,
        root: Path,
        payload: dict[str, object] | None = None,
        *,
        registry: CapabilityRegistry | None = None,
        items: list[dict[str, object]] | None = None,
        issue: object = None,
        instructions: object = None,
        runner_config: object = None,
    ):
        config = parse_workflow_config(payload or self.read_payload())
        return prepare_workflow_run(
            config,
            repo_root=root,
            artifact_root=root / "artifacts",
            registry=registry,
            providers={
                "kelpie.work_items.v1": items
                if items is not None
                else [{"id": "item-a", "title": "A"}],
            },
            issue_snapshot=issue,
            repo_instructions_snapshot=instructions,
            effective_runner_config=runner_config,
        )

    def test_matching_identity_resumes_and_skips_completed_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            prepared = self.prepare(
                root,
                issue={"number": 20, "body": "keep"},
                instructions="# rules\n",
            )
            state = prepared.persist_initial_state()
            store = RunStateStore(root / "artifacts")
            state = store.record_completed(state, "nodes/plan")

            resumed = prepared.load_resume_state(store)

        self.assertEqual(resumed.run_identity, prepared.identity.digest)
        self.assertTrue(resumed.is_completed("nodes/plan"))
        self.assertFalse(resumed.should_execute("nodes/plan"))
        self.assertEqual(
            resumed.pending_instances(("nodes/plan", "nodes/implementation@item-a")),
            ("nodes/implementation@item-a",),
        )

    def test_state_round_trip_preserves_completed_instances_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            prepared = self.prepare(root)
            store = RunStateStore(root / "artifacts")
            manifest = ArtifactManifest(run_identity=prepared.identity.digest, entries=())
            state = prepared.initial_state().with_completed_instance(
                "nodes/plan",
                manifest,
            )
            store.save(state, expected_identity=prepared.identity)
            loaded = store.load_for_resume(
                prepared.identity,
                workflow_id=prepared.plan.workflow_id,
            )

        self.assertEqual(loaded.completed_instances, ("nodes/plan",))
        self.assertEqual(loaded.output_manifest, manifest)

    def test_config_change_rejects_stale_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            original = self.prepare(root)
            original.persist_initial_state()

            changed = self.read_payload()
            changed["nodes"][0]["outputs"][0]["path"] = "changed.md"  # type: ignore[index]
            current = self.prepare(root, changed)

            with self.assertRaises(StaleResumeIdentityError):
                current.load_resume_state(RunStateStore(root / "artifacts"))

    def test_registry_resource_and_runner_changes_reject_stale_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            original = self.prepare(root, runner_config={"command": ["codex"]})

            changed_registry = CapabilityRegistry(
                runners=original.registry_snapshot.runners,
                lifecycles=original.registry_snapshot.lifecycles,
                controllers=original.registry_snapshot.controllers,
                virtual_inputs=original.registry_snapshot.virtual_inputs,
                loop_sources=original.registry_snapshot.loop_sources,
                version="2.0",
            )
            changed_resource = self.prepare(
                root,
                registry=changed_registry,
                runner_config={"command": ["codex"]},
            )
            changed_runner = self.prepare(
                root,
                runner_config={"command": ["different-codex"]},
            )
            original.persist_initial_state()
            store = RunStateStore(root / "artifacts")

            with self.assertRaises(StaleResumeIdentityError):
                changed_resource.load_resume_state(store)
            with self.assertRaises(StaleResumeIdentityError):
                changed_runner.load_resume_state(store)

            (root / "prompts/plan.md").write_text("# changed\n", encoding="utf-8")
            changed_prompt = self.prepare(root, runner_config={"command": ["codex"]})
            with self.assertRaises(StaleResumeIdentityError):
                changed_prompt.load_resume_state(store)

    def test_issue_instruction_and_source_snapshot_changes_reject_stale_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            original = self.prepare(
                root,
                items=[{"id": "item-a", "title": "A"}],
                issue={"number": 20, "body": "first"},
                instructions="# first\n",
            )
            original.persist_initial_state()
            store = RunStateStore(root / "artifacts")

            changed_issue = self.prepare(
                root,
                items=[{"id": "item-a", "title": "A"}],
                issue={"number": 20, "body": "changed"},
                instructions="# first\n",
            )
            changed_instructions = self.prepare(
                root,
                items=[{"id": "item-a", "title": "A"}],
                issue={"number": 20, "body": "first"},
                instructions="# changed\n",
            )
            changed_source = self.prepare(
                root,
                items=[{"id": "item-a", "title": "B"}],
                issue={"number": 20, "body": "first"},
                instructions="# first\n",
            )

            for candidate in (changed_issue, changed_instructions, changed_source):
                with self.subTest(identity=candidate.identity.digest):
                    with self.assertRaises(StaleResumeIdentityError):
                        candidate.load_resume_state(store)

    def test_invalid_preflight_does_not_create_state_or_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            invalid = self.read_payload()
            invalid["schema_version"] = "2.0"
            artifact_root = root / "artifacts"

            with self.assertRaises(WorkflowConfigError):
                self.prepare(root, invalid)

            self.assertFalse(artifact_root.exists())
            self.assertFalse((artifact_root / "workflow-state.json").exists())

    def test_identity_is_deterministic_for_equivalent_mapping_order(self) -> None:
        payload = self.read_payload()
        first = normalize_workflow_config(parse_workflow_config(payload))
        reordered = deepcopy(payload)
        reordered["limits"] = {
            "max_total_steps": 50,
            "max_loop_items": 10,
        }
        second = normalize_workflow_config(parse_workflow_config(reordered))

        self.assertEqual(
            build_run_identity(first).digest,
            build_run_identity(second).digest,
        )


if __name__ == "__main__":
    unittest.main()
