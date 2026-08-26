from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_issue_workflow import (
    InstructionStagingConfig,
    RunnerConfig,
    StepSpec,
    WorkflowRunner,
)


class ImplementationItemLoopTests(unittest.TestCase):
    repo_root = Path(__file__).resolve().parents[1]

    def make_runner(self, tmpdir: str, *, dry_run: bool = False) -> WorkflowRunner:
        workdir = Path(tmpdir) / "target-repo"
        workdir.mkdir()
        old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
        os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
        try:
            return WorkflowRunner(
                repo_root=self.repo_root,
                workdir=workdir,
                issue_number=None,
                runner_config=RunnerConfig(name="codex", command_template=["true"]),
                instruction_staging_config=InstructionStagingConfig(),
                issue_source="none",
                task_label="implementation-loop",
                dry_run=dry_run,
            )
        finally:
            if old_config_home is None:
                os.environ.pop("KELPIE_CONFIG_HOME", None)
            else:
                os.environ["KELPIE_CONFIG_HOME"] = old_config_home

    @staticmethod
    def write_work_items(runner: WorkflowRunner, tasks: list[dict[str, object]]) -> None:
        runner.work_items_json_path().write_text(
            json.dumps({"version": "1.0", "tasks": tasks}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def task(item_id: str, description: str = "Description") -> dict[str, object]:
        return {
            "id": item_id,
            "title": f"Title {item_id}",
            "description": description,
            "dependencies": [],
            "files": ["scripts/run_issue_workflow.py"],
            "acceptance_criteria": ["It works"],
        }

    def test_valid_items_run_in_order_with_fixed_step_metadata_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            tasks = [self.task("wi-1"), self.task("wi-2"), self.task("wi-3")]
            self.write_work_items(runner, tasks)
            calls: list[tuple[StepSpec, dict[str, str] | None]] = []

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                calls.append((step, virtual_context))

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                runner.implementation()

            self.assertEqual([step.name for step, _ in calls], ["implementation_coding"] * 3)
            self.assertEqual(
                [step.artifact_subdir for step, _ in calls],
                ["wi-1", "wi-2", "wi-3"],
            )
            self.assertEqual(
                [step.context_id for step, _ in calls],
                ["work-items"] * 3,
            )
            self.assertEqual(
                [json.loads(context["$loop_item"]) for _, context in calls if context],
                tasks,
            )

            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["overall_status"], "succeeded")
            self.assertEqual(
                [item["status"] for item in status["items"]],
                ["succeeded", "succeeded", "succeeded"],
            )
            self.assertEqual(
                [item["artifact_scope"] for item in status["items"]],
                ["work-items/wi-1", "work-items/wi-2", "work-items/wi-3"],
            )
            self.assertFalse(runner.implementation_loop_lock_path().exists())

    def test_explicit_loop_context_is_complete_and_does_not_use_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir, dry_run=True)
            task = self.task("long-item", "x" * 2100)
            self.write_work_items(runner, [task])

            with patch.dict(os.environ, {"KELPIE_LOOP_ITEM": "wrong legacy value"}):
                runner.implementation()

            scope = runner.artifact_dir / "work-items" / "long-item"
            prompt = (scope / ".generated-prompts" / "implementation_coding.prompt.md").read_text(
                encoding="utf-8"
            )
            intent = json.loads(
                (scope / "intent-records" / "implementation_coding-intent-record.json").read_text(
                    encoding="utf-8"
                )
            )
            canonical = json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            self.assertIn(canonical, prompt)
            self.assertIn('truncated="false"', prompt)
            self.assertEqual(intent["inputs"][0]["original_length"], len(canonical))
            self.assertFalse(intent["inputs"][0]["truncated"])

            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["mode"], "dry-run")
            self.assertEqual(status["overall_status"], "planned")
            self.assertEqual(status["items"][0]["status"], "planned")

    def test_explicit_context_requires_supported_keys_and_loop_item_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir, dry_run=True)
            with self.assertRaisesRegex(ValueError, "Unsupported virtual context keys"):
                runner.resolve_step_inputs(
                    ["$loop_item"],
                    virtual_context={"$unsupported": "value"},
                )
            with self.assertRaisesRegex(ValueError, "no loop item context"):
                runner.resolve_step_inputs(["$loop_item"], virtual_context={})

            self.assertFalse((runner.artifact_dir / "work-items").exists())

    def test_dry_run_keeps_step_lifecycle_artifacts_in_each_item_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir, dry_run=True)
            self.write_work_items(runner, [self.task("wi-a"), self.task("wi-b")])

            runner.implementation()

            for item_id in ("wi-a", "wi-b"):
                scope = runner.artifact_dir / "work-items" / item_id
                self.assertTrue(
                    (scope / ".generated-prompts" / "implementation_coding.prompt.md").is_file()
                )
                self.assertTrue(
                    (scope / "intent-records" / "implementation_coding-intent-record.json").is_file()
                )
                self.assertTrue((scope / "checks" / "implementation_coding-pre-check.txt").is_file())
                self.assertTrue((scope / "checks" / "implementation_coding-post-check.txt").is_file())

            self.assertFalse(
                (runner.artifact_dir / ".generated-prompts" / "implementation_coding.prompt.md").exists()
            )

    def test_non_dry_run_uses_real_step_lifecycle_for_each_item_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-a"), self.task("wi-b")])
            invoked: list[str] = []

            def fake_cli(command: list[str], **kwargs: object) -> SimpleNamespace:
                prompt = str(kwargs["input"])
                item_id = next(item_id for item_id in ("wi-a", "wi-b") if f'"id":"{item_id}"' in prompt)
                invoked.append(item_id)
                scope = runner.artifact_dir / "work-items" / item_id
                (scope / "06-implementation-notes.md").write_text(
                    f"# Implementation {item_id}\n",
                    encoding="utf-8",
                )
                (scope / "implementation_coding-phase-outcome.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "phase": "implementation",
                            "decision": "advance",
                            "reason_code": "implementation_ready_for_review",
                            "summary": "Item completed.",
                            "evidence_refs": ["06-implementation-notes.md"],
                            "resume_condition": None,
                            "artifact_digests": {},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("scripts.run_issue_workflow.subprocess.run", side_effect=fake_cli):
                runner.implementation()

            self.assertEqual(invoked, ["wi-a", "wi-b"])
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["overall_status"], "succeeded")
            for item_id in invoked:
                scope = runner.artifact_dir / "work-items" / item_id
                self.assertTrue((scope / "implementation_coding-phase-outcome.json").is_file())
                self.assertTrue((scope / "phase-outcomes" / "implementation" / "0001.json").is_file())

    def test_preflight_rejects_invalid_items_before_status_or_scope_creation(self) -> None:
        cases = [
            ("missing required field", {"id": "wi-1", "title": "Title"}, "Invalid implementation"),
            ("duplicate id", [self.task("same"), self.task("same")], "Duplicate"),
            ("unsafe id", [self.task("../escape")], "Invalid implementation work item id"),
        ]
        for name, raw_tasks, message in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmpdir:
                runner = self.make_runner(tmpdir)
                tasks = raw_tasks if isinstance(raw_tasks, list) else [raw_tasks]
                self.write_work_items(runner, tasks)
                with patch.object(runner, "run_step") as run_step:
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        runner.implementation()
                    run_step.assert_not_called()
                self.assertFalse(runner.implementation_loop_status_path().exists())
                self.assertFalse((runner.artifact_dir / "work-items").exists())

    def test_preflight_rejects_duplicate_and_stale_targets_without_calling_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            stale_scope = runner.artifact_dir / "work-items" / "wi-2"
            stale_scope.mkdir(parents=True)

            with patch.object(runner, "run_step") as run_step:
                with self.assertRaisesRegex(RuntimeError, "artifact scope already exists"):
                    runner.implementation()
                run_step.assert_not_called()

            self.assertFalse(runner.implementation_loop_status_path().exists())

    def test_item_failure_stops_loop_and_preserves_not_run_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2"), self.task("wi-3")])
            calls: list[str] = []
            failure = RuntimeError("item failed")

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                assert virtual_context is not None
                item_id = json.loads(virtual_context["$loop_item"])["id"]
                calls.append(item_id)
                if item_id == "wi-2":
                    raise failure

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                with self.assertRaises(RuntimeError) as raised:
                    runner.implementation()

            self.assertIs(raised.exception, failure)
            self.assertEqual(calls, ["wi-1", "wi-2"])
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["overall_status"], "failed")
            self.assertEqual(
                [item["status"] for item in status["items"]],
                ["succeeded", "failed", "not_run"],
            )
            self.assertEqual(status["items"][1]["error"], {"type": "RuntimeError", "message": "item failed"})

    def test_control_exception_is_recorded_then_reraised(self) -> None:
        for control_exception in (SystemExit("stop"), KeyboardInterrupt()):
            with self.subTest(exception=type(control_exception).__name__), tempfile.TemporaryDirectory() as tmpdir:
                runner = self.make_runner(tmpdir)
                self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])

                def fake_run_step(
                    step: StepSpec,
                    *,
                    virtual_context: dict[str, str] | None = None,
                ) -> None:
                    _ = step, virtual_context
                    raise control_exception

                with patch.object(runner, "run_step", side_effect=fake_run_step):
                    with self.assertRaises(type(control_exception)):
                        runner.implementation()

                status = json.loads(
                    runner.implementation_loop_status_path().read_text(encoding="utf-8")
                )
                self.assertEqual([item["status"] for item in status["items"]], ["failed", "not_run"])

    def test_status_recording_failure_does_not_replace_executor_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            primary = ValueError("executor failure")

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                _ = step, virtual_context
                raise primary

            real_writer = runner.write_implementation_loop_status

            def failing_writer(status: dict[str, object]) -> None:
                if status.get("overall_status") == "failed":
                    raise OSError("disk full")
                real_writer(status)

            with (
                patch.object(runner, "run_step", side_effect=fake_run_step),
                patch.object(runner, "write_implementation_loop_status", side_effect=failing_writer),
            ):
                with self.assertRaises(ValueError) as raised:
                    runner.implementation()

            self.assertIs(raised.exception, primary)
            self.assertTrue(any("status recording failed" in note for note in raised.exception.__notes__))

    def test_success_status_persistence_failure_stops_before_next_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1"), self.task("wi-2")])
            calls: list[str] = []

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                assert virtual_context is not None
                calls.append(json.loads(virtual_context["$loop_item"])["id"])

            real_writer = runner.write_implementation_loop_status

            def failing_writer(status: dict[str, object]) -> None:
                items = status["items"]
                assert isinstance(items, list)
                if items[0]["status"] == "succeeded":
                    raise OSError("status write failed")
                real_writer(status)

            with (
                patch.object(runner, "run_step", side_effect=fake_run_step),
                patch.object(runner, "write_implementation_loop_status", side_effect=failing_writer),
            ):
                with self.assertRaisesRegex(OSError, "status write failed"):
                    runner.implementation()

            self.assertEqual(calls, ["wi-1"])

    def test_source_is_snapshotted_before_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            initial_tasks = [self.task("wi-1"), self.task("wi-2")]
            self.write_work_items(runner, initial_tasks)
            calls: list[str] = []

            def fake_run_step(
                step: StepSpec,
                *,
                virtual_context: dict[str, str] | None = None,
            ) -> None:
                assert virtual_context is not None
                calls.append(json.loads(virtual_context["$loop_item"])["id"])
                if len(calls) == 1:
                    self.write_work_items(runner, [self.task("replacement")])

            with patch.object(runner, "run_step", side_effect=fake_run_step):
                runner.implementation()

            self.assertEqual(calls, ["wi-1", "wi-2"])
            status = json.loads(
                runner.implementation_loop_status_path().read_text(encoding="utf-8")
            )
            self.assertEqual(status["order"], ["wi-1", "wi-2"])

    def test_existing_loop_lock_is_rejected_without_status_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = self.make_runner(tmpdir)
            self.write_work_items(runner, [self.task("wi-1")])
            runner.implementation_loop_lock_path().write_text("pid=existing\n", encoding="utf-8")

            with patch.object(runner, "run_step") as run_step:
                with self.assertRaisesRegex(RuntimeError, "already locked"):
                    runner.implementation()
                run_step.assert_not_called()

            self.assertTrue(runner.implementation_loop_lock_path().exists())
            self.assertFalse(runner.implementation_loop_status_path().exists())


if __name__ == "__main__":
    unittest.main()
