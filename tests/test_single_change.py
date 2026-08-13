from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_issue_workflow import (
    ActiveTarget,
    CheckSpec,
    InstructionStagingConfig,
    RunnerConfig,
    SingleChangeRequest,
    StepSpec,
    WorkflowRunner,
)
from scripts.single_change import (
    GitStateCapture,
    IterationStore,
    SingleChangeRequestValidator,
    run_single_change,
)


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _make_git_repo(root: Path) -> None:
    root.mkdir()
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "single-change@example.com")
    _run_git(root, "config", "user.name", "Single Change Tests")
    (root / "target.txt").write_text("base\n", encoding="utf-8")
    (root / "preexisting.txt").write_text("user baseline\n", encoding="utf-8")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "baseline")


def _request(
    *,
    allowed_paths: tuple[str, ...] = ("target.txt",),
    checks: tuple[CheckSpec, ...] = (),
) -> SingleChangeRequest:
    return SingleChangeRequest(
        work_item_id="wi-1",
        active_targets=(
            ActiveTarget(
                kind="work_item",
                id="wi-1",
                source_ref="05-work-breakdown.md#wi-1",
            ),
        ),
        change_intent="apply the smallest target change",
        allowed_paths=allowed_paths,
        checks=checks,
    )


class SingleChangeValidationTests(unittest.TestCase):
    def test_exactly_one_target_is_required_without_artifact_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"
            workdir.mkdir()
            validator = SingleChangeRequestValidator(workdir)
            request = _request()

            with self.assertRaisesRegex(ValueError, "exactly one"):
                validator.validate(
                    SingleChangeRequest(
                        request.work_item_id,
                        (),
                        request.change_intent,
                        request.allowed_paths,
                    )
                )
            with self.assertRaisesRegex(ValueError, "exactly one"):
                validator.validate(
                    SingleChangeRequest(
                        request.work_item_id,
                        (request.active_targets[0], request.active_targets[0]),
                        request.change_intent,
                        request.allowed_paths,
                    )
                )
            self.assertFalse((workdir / ".kelpie").exists())

    def test_path_traversal_and_symlink_components_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"
            workdir.mkdir()
            outside = Path(tmpdir) / "outside"
            outside.mkdir()
            (workdir / "link").symlink_to(outside, target_is_directory=True)
            validator = SingleChangeRequestValidator(workdir)

            with self.assertRaisesRegex(ValueError, "Invalid allowed path"):
                validator.validate(_request(allowed_paths=("../outside.txt",)))
            with self.assertRaisesRegex(ValueError, "symlink"):
                validator.validate(_request(allowed_paths=("link/file.txt",)))

    def test_path_names_with_spaces_are_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"
            workdir.mkdir()
            target = workdir / "target with space.txt"
            target.write_text("before\n", encoding="utf-8")
            validator = SingleChangeRequestValidator(workdir)

            validated = validator.validate(
                _request(allowed_paths=("target with space.txt",))
            )

            self.assertEqual(validated.allowed_paths, ("target with space.txt",))

    def test_reserved_and_glob_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"
            workdir.mkdir()
            validator = SingleChangeRequestValidator(workdir)

            for path in (".git/config", ".kelpie/artifacts.json", "src/*.py"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    validator.validate(_request(allowed_paths=(path,)))

    def test_malformed_target_kind_is_rejected_as_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"
            workdir.mkdir()
            validator = SingleChangeRequestValidator(workdir)
            request = _request()
            malformed = SingleChangeRequest(
                work_item_id=request.work_item_id,
                active_targets=(
                    ActiveTarget(
                        kind=[],  # type: ignore[arg-type]
                        id="wi-1",
                        source_ref="source",
                    ),
                ),
                change_intent=request.change_intent,
                allowed_paths=request.allowed_paths,
            )

            with self.assertRaises(ValueError):
                validator.validate(malformed)


class SingleChangeFixtureTests(unittest.TestCase):
    def test_dirty_tree_provenance_and_bounded_check_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            (root / "preexisting.txt").write_text("user edit before iteration\n", encoding="utf-8")
            request = _request(
                checks=(
                    CheckSpec(
                        argv=(sys.executable, "-c", "print('check-output')"),
                        timeout_seconds=10,
                    ),
                )
            )

            def executor(_request: SingleChangeRequest, _scope: object) -> None:
                (root / "target.txt").write_text("base\niteration change\n", encoding="utf-8")

            result = run_single_change(
                request,
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=executor,
            )
            iteration = result.iteration_dir

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.outcome["changed_paths"], ["target.txt"])
            self.assertEqual(result.outcome["unplanned_paths"], [])
            self.assertTrue(result.outcome["required_artifacts_complete"])
            self.assertEqual(json.loads((iteration / "lifecycle.json").read_text())["state"], "terminal")
            intent = json.loads((iteration / "intent.json").read_text())
            self.assertEqual(intent["target"]["id"], "wi-1")
            self.assertEqual(intent["change_intent"], request.change_intent)
            self.assertEqual(intent["allowed_paths"], ["target.txt"])
            diff = (iteration / "diff.patch").read_text(encoding="utf-8")
            self.assertIn("--- a/target.txt\n+++ b/target.txt\n", diff)
            self.assertNotIn("preexisting.txt", diff)
            check = json.loads((iteration / "checks" / "0001.json").read_text())
            self.assertEqual(check["status"], "passed")
            self.assertEqual(check["exit_code"], 0)
            self.assertEqual(
                (iteration / check["stdout_ref"]).read_text(encoding="utf-8"),
                "check-output\n",
            )
            self.assertIn("Potential hitchhiking changes", (iteration / "summary.md").read_text())

    def test_unplanned_change_is_failed_and_next_run_uses_new_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            request = _request()

            def executor(_request: SingleChangeRequest, _scope: object) -> None:
                (root / "target.txt").write_text("changed\n", encoding="utf-8")
                (root / "surprise.txt").write_text("not planned\n", encoding="utf-8")

            first = run_single_change(
                request,
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=executor,
            )
            first_intent = (first.iteration_dir / "intent.json").read_bytes()
            self.assertEqual(first.status, "failed")
            self.assertIn("unplanned_path", first.outcome["reason_codes"])
            self.assertEqual(first.outcome["unplanned_paths"], ["surprise.txt"])
            self.assertIn("surprise.txt", (first.iteration_dir / "summary.md").read_text())

            second = run_single_change(
                request,
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=executor,
            )
            self.assertEqual(second.iteration_id, "0002")
            self.assertEqual(first_intent, (first.iteration_dir / "intent.json").read_bytes())
            self.assertTrue((root / ".kelpie" / "artifacts" / "work-items" / "wi-1" / "iterations" / "0001").is_dir())
            self.assertTrue((root / ".kelpie" / "artifacts" / "work-items" / "wi-1" / "iterations" / "0002").is_dir())

    def test_additional_change_to_preexisting_dirty_path_is_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            (root / "preexisting.txt").write_text("user edit before iteration\n", encoding="utf-8")

            def executor(_request: SingleChangeRequest, _scope: object) -> None:
                (root / "target.txt").write_text("changed\n", encoding="utf-8")
                with (root / "preexisting.txt").open("a", encoding="utf-8") as stream:
                    stream.write("iteration accidentally appended\n")

            result = run_single_change(
                _request(),
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=executor,
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.outcome["unplanned_paths"], ["preexisting.txt"])
            self.assertIn("preexisting.txt", (result.iteration_dir / "diff.patch").read_text())

    def test_incomplete_scope_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            iteration_root = (
                root / ".kelpie" / "artifacts" / "work-items" / "wi-1" / "iterations"
            )
            first = iteration_root / "0001"
            first.mkdir(parents=True)
            (first / "lifecycle.json").write_text(
                json.dumps({"state": "in_progress"}), encoding="utf-8"
            )

            result = run_single_change(
                _request(),
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=lambda _request, _scope: None,
            )

            self.assertEqual(result.iteration_id, "0002")
            self.assertEqual(
                (first / "lifecycle.json").read_text(encoding="utf-8"),
                json.dumps({"state": "in_progress"}),
            )

    def test_exclusive_intent_write_does_not_delete_existing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            artifact_root = root / ".kelpie" / "artifacts"
            store = IterationStore(artifact_root, "wi-1")
            scope = store.reserve()
            payload = {"change_intent": "first"}
            store.write_intent(scope, payload)
            original = (scope.path / "intent.json").read_bytes()

            with self.assertRaises(FileExistsError):
                store.write_intent(scope, {"change_intent": "second"})

            self.assertEqual((scope.path / "intent.json").read_bytes(), original)

    def test_executor_failure_keeps_after_provenance_and_cannot_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)

            def executor(_request: SingleChangeRequest, _scope: object) -> None:
                (root / "target.txt").write_text("partially changed\n", encoding="utf-8")
                raise RuntimeError("executor stopped after mutation")

            result = run_single_change(
                _request(),
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=executor,
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("executor_failed", result.outcome["reason_codes"])
            self.assertTrue((result.iteration_dir / "git-after-change" / "paths.json").is_file())
            self.assertIn("target.txt", (result.iteration_dir / "diff.patch").read_text())
            lifecycle = json.loads((result.iteration_dir / "lifecycle.json").read_text())
            self.assertEqual(lifecycle["status"], "failed")

    def test_missing_capture_boundary_is_reported_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            original_capture = GitStateCapture.capture

            def fail_after_change(capture, boundary_directory, *, known_paths=()):
                if boundary_directory.name == "git-after-change":
                    raise OSError("injected after-change capture failure")
                return original_capture(
                    capture,
                    boundary_directory,
                    known_paths=known_paths,
                )

            with patch.object(GitStateCapture, "capture", fail_after_change):
                result = run_single_change(
                    _request(),
                    workdir=root,
                    artifact_root=root / ".kelpie" / "artifacts",
                    executor=lambda _request, _scope: (
                        root / "target.txt"
                    ).write_text("changed\n", encoding="utf-8"),
                )

            self.assertEqual(result.status, "failed")
            self.assertIn("capture_failed", result.outcome["reason_codes"])
            self.assertIn("artifact_incomplete", result.outcome["reason_codes"])
            self.assertFalse(result.outcome["required_artifacts_complete"])
            self.assertFalse(
                (result.iteration_dir / "git-after-change" / "repository.json").exists()
            )

    def test_head_drift_is_failed_even_when_source_diff_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)

            def executor(_request: SingleChangeRequest, _scope: object) -> None:
                (root / "target.txt").write_text("committed during iteration\n", encoding="utf-8")
                _run_git(root, "add", "target.txt")
                _run_git(root, "commit", "-qm", "unexpected iteration commit")

            result = run_single_change(
                _request(),
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=executor,
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("git_identity_drift", result.outcome["reason_codes"])

    def test_runtime_symlink_is_recorded_as_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            outside = Path(tmpdir) / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")

            def executor(_request: SingleChangeRequest, _scope: object) -> None:
                (root / "target.txt").unlink()
                (root / "target.txt").symlink_to(outside)

            result = run_single_change(
                _request(),
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=executor,
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("unsupported_state", result.outcome["reason_codes"])
            self.assertEqual(
                json.loads(
                    (result.iteration_dir / "git-after-change" / "paths.json").read_text()
                )["paths"][0]["kind"],
                "symlink",
            )

    def test_check_failure_is_recorded_with_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            request = _request(
                checks=(
                    CheckSpec(
                        argv=(
                            sys.executable,
                            "-c",
                            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
                        ),
                        timeout_seconds=10,
                    ),
                )
            )
            result = run_single_change(
                request,
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=lambda _request, _scope: (root / "target.txt").write_text("changed\n"),
            )
            self.assertEqual(result.status, "failed")
            self.assertIn("check_failed", result.outcome["reason_codes"])
            check = json.loads((result.iteration_dir / "checks" / "0001.json").read_text())
            self.assertEqual(check["exit_code"], 3)
            self.assertEqual((result.iteration_dir / check["stderr_ref"]).read_text(), "err\n")

    def test_check_timeout_and_output_limit_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            request = _request(
                checks=(
                    CheckSpec(
                        argv=(
                            sys.executable,
                            "-c",
                            "import sys, time; sys.stdout.write('x' * 1000); sys.stdout.flush(); time.sleep(5)",
                        ),
                        timeout_seconds=1,
                        output_limit_bytes=32,
                    ),
                )
            )
            result = run_single_change(
                request,
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=lambda _request, _scope: (root / "target.txt").write_text("changed\n"),
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("check_timeout", result.outcome["reason_codes"])
            check = json.loads((result.iteration_dir / "checks" / "0001.json").read_text())
            self.assertEqual(check["status"], "timeout")
            self.assertTrue(check["stdout_truncated"])
            self.assertLessEqual(check["stdout_stored_bytes"], 32)
            self.assertIsNotNone(check["signal"])

    @unittest.skipUnless(sys.platform != "win32", "process groups are POSIX-specific")
    def test_timeout_covers_child_holding_output_pipe_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            request = _request(
                checks=(
                    CheckSpec(
                        argv=(
                            sys.executable,
                            "-c",
                            (
                                "import subprocess, sys; "
                                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); "
                                "print('parent done', flush=True)"
                            ),
                        ),
                        timeout_seconds=1,
                    ),
                )
            )
            started = time.monotonic()
            result = run_single_change(
                request,
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=lambda _request, _scope: (root / "target.txt").write_text("changed\n"),
            )

            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(result.status, "failed")
            check = json.loads((result.iteration_dir / "checks" / "0001.json").read_text())
            self.assertEqual(check["status"], "timeout")

    def test_check_induced_unplanned_change_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            request = _request(
                checks=(
                    CheckSpec(
                        argv=(
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('surprise.txt').write_text('check change\\n')",
                        ),
                        timeout_seconds=10,
                    ),
                )
            )
            result = run_single_change(
                request,
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=lambda _request, _scope: (root / "target.txt").write_text("changed\n"),
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("unplanned_path", result.outcome["reason_codes"])
            self.assertEqual(result.outcome["check_changed_paths"], ["surprise.txt"])

    def test_index_drift_is_recorded_without_comparing_index_stat_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)

            def executor(_request: SingleChangeRequest, _scope: object) -> None:
                (root / "target.txt").write_text("changed\n", encoding="utf-8")
                _run_git(root, "add", "target.txt")

            result = run_single_change(
                _request(),
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=executor,
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("git_identity_drift", result.outcome["reason_codes"])

    def test_declared_binary_path_fails_closed_without_invoking_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            _make_git_repo(root)
            binary_path = root / "binary.dat"
            binary_path.write_bytes(b"\x00\x01\x02")
            request = _request(allowed_paths=("binary.dat",))
            invoked = False

            def executor(_request: SingleChangeRequest, _scope: object) -> None:
                nonlocal invoked
                invoked = True

            result = run_single_change(
                request,
                workdir=root,
                artifact_root=root / ".kelpie" / "artifacts",
                executor=executor,
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("unsupported_state", result.outcome["reason_codes"])
            self.assertFalse(invoked)

    def test_workflow_runner_uses_run_step_once_for_opt_in_entry_point(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"
            _make_git_repo(workdir)
            runner = WorkflowRunner(
                repo_root=repo_root,
                workdir=workdir,
                issue_number=None,
                runner_config=RunnerConfig(name="codex", command_template=["true"]),
                instruction_staging_config=InstructionStagingConfig(),
                issue_source="none",
                task_label="single-change",
                dry_run=True,
            )
            request = _request()
            with patch.object(runner, "run_step") as run_step:
                result = runner.run_single_change(request)

            self.assertEqual(result.status, "succeeded")
            run_step.assert_called_once()
            step = run_step.call_args.args[0]
            self.assertIsInstance(step, StepSpec)
            self.assertEqual(step.name, "single-change")
            self.assertEqual(step.context_id, "work-items")
            self.assertEqual(step.artifact_subdir, "wi-1/iterations/0001")


if __name__ == "__main__":
    unittest.main()
