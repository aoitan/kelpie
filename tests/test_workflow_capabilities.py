from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.run_issue_workflow import (
    RunnerConfig,
    RunnerResolver,
    RunnerResolverCapabilityAdapter,
)
from scripts.workflow_config import (
    CapabilityRegistry,
    CapabilityResourceLimits,
    CapabilitySpec,
    WorkflowConfigError,
    default_capability_registry,
    parse_workflow_config,
    validate_workflow_capabilities,
)


class WorkflowCapabilityTests(unittest.TestCase):
    fixture = Path(__file__).parent / "fixtures" / "workflows" / "valid-v1.json"

    def read_payload(self) -> dict[str, object]:
        return json.loads(self.fixture.read_text(encoding="utf-8"))

    def write_resources(self, root: Path) -> None:
        for relative in (
            "prompts/plan.md",
            "prompts/code.md",
            "skills/plan.md",
            "skills/code.md",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {relative}\n", encoding="utf-8")

    def validate(self, payload: dict[str, object], root: Path, registry=None):
        return validate_workflow_capabilities(
            parse_workflow_config(payload),
            registry or default_capability_registry(),
            repo_root=root,
        )

    def test_default_registry_authorizes_resources_and_records_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            result = self.validate(self.read_payload(), root)
            prompt_bytes = (root / "prompts/plan.md").read_bytes()
        self.assertEqual(
            result.resource_digests["prompts/plan.md"],
            hashlib.sha256(prompt_bytes).hexdigest(),
        )
        self.assertEqual(result.resource_metadata["prompts/plan.md"].roles, {"prompt"})
        self.assertIn("codex", result.snapshot.runners)
        self.assertEqual(result.external_send_capabilities, ())

    def test_snapshot_and_capability_metadata_are_read_only(self) -> None:
        snapshot = default_capability_registry().snapshot("repository_issue")

        with self.assertRaises(TypeError):
            snapshot.runners["new"] = CapabilitySpec("new", "runner")  # type: ignore[index]
        with self.assertRaises(TypeError):
            snapshot.runners["codex"].metadata["changed"] = True  # type: ignore[index]

        self.assertEqual(snapshot.runners["codex"].external_send, False)
        self.assertEqual(
            snapshot.runners["codex"].resource_limits.max_prompt_bytes,
            1024 * 1024,
        )

    def test_unknown_capability_is_distinct_from_unauthorized_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            unknown = self.read_payload()
            unknown["nodes"][0]["runner"] = "not-registered"  # type: ignore[index]

            with self.assertRaises(WorkflowConfigError) as unknown_context:
                self.validate(unknown, root)

            unauthorized_registry = CapabilityRegistry(
                runners={"codex": {"allowed_profiles": ["other-profile"]}},
                lifecycles={
                    "kelpie.phase.plan.v1": {},
                    "kelpie.phase.implementation.v1": {},
                },
                controllers={"fixed_sequence.v1": {}},
                virtual_inputs={"$issue": {}, "$loop_item": {}},
                loop_sources={"kelpie.work_items.v1": {}},
            )
            unauthorized = self.read_payload()
            with self.assertRaises(WorkflowConfigError) as unauthorized_context:
                self.validate(unauthorized, root, unauthorized_registry)

        self.assertIn(
            "unknown_capability",
            {item.code for item in unknown_context.exception.diagnostics},
        )
        self.assertNotIn(
            "unauthorized_capability",
            {item.code for item in unknown_context.exception.diagnostics},
        )
        self.assertIn(
            "unauthorized_capability",
            {item.code for item in unauthorized_context.exception.diagnostics},
        )
        self.assertNotIn(
            "unknown_capability",
            {item.code for item in unauthorized_context.exception.diagnostics},
        )

    def test_runner_and_lifecycle_pair_must_be_authorized(self) -> None:
        registry = CapabilityRegistry(
            runners={
                "codex": {
                    "allowed_lifecycles": ["kelpie.phase.other.v1"],
                }
            },
            lifecycles={"kelpie.phase.plan.v1": {}},
            controllers={"fixed_sequence.v1": {}},
            virtual_inputs={"$issue": {}, "$loop_item": {}},
            loop_sources={"kelpie.work_items.v1": {}},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            with self.assertRaises(WorkflowConfigError) as context:
                self.validate(self.read_payload(), root, registry)

        diagnostics = context.exception.diagnostics
        self.assertIn("unauthorized_capability", {item.code for item in diagnostics})
        self.assertTrue(any(item.path == "/nodes/0/runner" for item in diagnostics))

    def test_prompt_and_skill_allowlists_are_enforced(self) -> None:
        registry = CapabilityRegistry(
            runners={
                "codex": {
                    "allowed_prompt_paths": ["prompts/approved/"],
                    "allowed_skill_paths": ["skills/approved/"],
                }
            },
            lifecycles={"kelpie.phase.plan.v1": {}},
            controllers={"fixed_sequence.v1": {}},
            virtual_inputs={"$issue": {}, "$loop_item": {}},
            loop_sources={"kelpie.work_items.v1": {}},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            with self.assertRaises(WorkflowConfigError) as context:
                self.validate(self.read_payload(), root, registry)

        paths = {
            item.path
            for item in context.exception.diagnostics
            if item.code == "unauthorized_capability"
        }
        self.assertIn("/nodes/0/prompt", paths)
        self.assertIn("/nodes/0/skill", paths)

    def test_prompt_and_skill_must_be_regular_non_symlink_markdown_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            directory_payload = self.read_payload()
            directory = root / "prompts/directory.md"
            directory.mkdir(parents=True)
            directory_payload["nodes"][0]["prompt"] = "prompts/directory.md"  # type: ignore[index]

            with self.assertRaises(WorkflowConfigError) as directory_context:
                self.validate(directory_payload, root)

            outside = root / "outside.md"
            outside.write_text("outside\n", encoding="utf-8")
            symlink_payload = self.read_payload()
            symlink = root / "prompts/link.md"
            symlink.symlink_to(outside)
            symlink_payload["nodes"][0]["prompt"] = "prompts/link.md"  # type: ignore[index]

            with self.assertRaises(WorkflowConfigError) as symlink_context:
                self.validate(symlink_payload, root)

        self.assertIn(
            "invalid_resource",
            {item.code for item in directory_context.exception.diagnostics},
        )
        self.assertIn(
            "unsafe_path",
            {item.code for item in symlink_context.exception.diagnostics},
        )

    def test_registry_resource_limit_and_external_send_are_metadata(self) -> None:
        registry = CapabilityRegistry(
            runners={
                "codex": CapabilitySpec(
                    "codex",
                    "runner",
                    external_send=True,
                    resource_limits=CapabilityResourceLimits(max_prompt_bytes=2),
                )
            },
            lifecycles={"kelpie.phase.plan.v1": {}},
            controllers={"fixed_sequence.v1": {}},
            virtual_inputs={"$issue": {}, "$loop_item": {}},
            loop_sources={"kelpie.work_items.v1": {}},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_resources(root)
            with self.assertRaises(WorkflowConfigError) as context:
                self.validate(self.read_payload(), root, registry)

        self.assertIn(
            "resource_limit_exceeded",
            {item.code for item in context.exception.diagnostics},
        )

    def test_registry_definition_rejects_executable_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "executable fields"):
            CapabilityRegistry.from_mapping(
                {"runners": {"unsafe": {"command": ["rm", "-rf"]}}}
            )

    def test_runner_resolver_adapter_delegates_commands_without_snapshot_leak(self) -> None:
        runner = RunnerConfig(name="codex", command_template=["codex-cli"])
        alternate = RunnerConfig(name="alternate", command_template=["alternate-cli"])
        resolver = RunnerResolver(
            {runner.name: runner, alternate.name: alternate},
            default_name=runner.name,
        )
        adapter = RunnerResolverCapabilityAdapter(resolver)

        snapshot = adapter.snapshot("repository_issue")
        resolved = adapter.resolve(None, phase="implementation", step_name="coder")

        self.assertIn("alternate", snapshot.runners)
        self.assertFalse(hasattr(snapshot.runners["alternate"], "command_template"))
        self.assertEqual(resolved.command_template, ["codex-cli"])


if __name__ == "__main__":
    unittest.main()
