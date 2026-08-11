from __future__ import annotations

import unittest
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_issue_workflow import (
    InstructionStagingConfig,
    RunnerConfig,
    WorkflowRunner,
    diagnose_codex_failure,
)


class CodexFailureDiagnosisTests(unittest.TestCase):
    def test_capacity_message_is_provider_capacity_not_a_429_limit(self) -> None:
        diagnosis = diagnose_codex_failure(
            "",
            "ERROR: Selected model is at capacity. Please try a different model.\n",
        )

        self.assertEqual(diagnosis.category, "provider_capacity")
        self.assertTrue(diagnosis.retryable)
        self.assertEqual(diagnosis.error_code, "server_overloaded")
        self.assertIsNone(diagnosis.retry_after_seconds)
        self.assertIsNone(diagnosis.reset_at)
        self.assertEqual(diagnosis.evidence, "selected_model_at_capacity")

    def test_explicit_429_rate_limit_is_retryable_and_keeps_retry_after(self) -> None:
        diagnosis = diagnose_codex_failure(
            "",
            "HTTP 429 rate limit reached\nRetry-After: 42\n",
        )

        self.assertEqual(diagnosis.category, "request_rate_limited")
        self.assertTrue(diagnosis.retryable)
        self.assertEqual(diagnosis.retry_after_seconds, 42)
        self.assertEqual(diagnosis.evidence, "http_429_rate_limit")

    def test_usage_or_billing_limit_is_not_retryable(self) -> None:
        diagnosis = diagnose_codex_failure(
            "",
            "HTTP 429 insufficient_quota: weekly usage limit reached\n",
        )

        self.assertEqual(diagnosis.category, "usage_or_billing_limited")
        self.assertFalse(diagnosis.retryable)
        self.assertEqual(diagnosis.error_code, "insufficient_quota")

    def test_five_hour_usage_window_is_not_mistaken_for_request_rate_limit(self) -> None:
        diagnosis = diagnose_codex_failure("", "HTTP 429: 5-hour usage window exhausted\n")

        self.assertEqual(diagnosis.category, "usage_or_billing_limited")
        self.assertFalse(diagnosis.retryable)
        self.assertEqual(diagnosis.evidence, "usage_or_billing_limit")

    def test_generic_429_remains_unknown(self) -> None:
        diagnosis = diagnose_codex_failure("", "Request failed with HTTP 429\n")

        self.assertEqual(diagnosis.category, "unknown")
        self.assertFalse(diagnosis.retryable)
        self.assertEqual(diagnosis.evidence, "http_429_without_cause")

    def test_explicit_iso_reset_time_is_preserved_without_inference(self) -> None:
        diagnosis = diagnose_codex_failure(
            "",
            "usage limit reached; resets at 2026-08-12T20:00:00Z\n",
        )

        self.assertEqual(diagnosis.category, "usage_or_billing_limited")
        self.assertEqual(diagnosis.reset_at, "2026-08-12T20:00:00Z")


class _FakeTextStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = iter(lines)

    def readline(self) -> str:
        return next(self._lines, "")

    def close(self) -> None:
        pass


class _FakeStdin:
    def __init__(self) -> None:
        self.written = ""
        self.closed = False

    def write(self, text: str) -> None:
        self.written += text

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeTextStream([])
        self.stderr = _FakeTextStream(["ERROR: Selected model is at capacity. Please try a different model.\n"])
        self.returncode = 1

    def wait(self) -> int:
        return self.returncode


class WorkflowCodexFailureArtifactTests(unittest.TestCase):
    def test_codex_capacity_failure_writes_sanitized_diagnostic_artifact(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number=None,
                    runner_config=RunnerConfig(name="codex", command_template=["codex", "exec", "-"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="none",
                    task_label="codex-failure-diagnostic",
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            process = _FakeProcess()
            with patch("scripts.run_issue_workflow.subprocess.Popen", return_value=process) as mock_popen:
                with patch(
                    "scripts.run_issue_workflow.subprocess.run",
                    return_value=SimpleNamespace(returncode=1),
                ):
                    with self.assertRaisesRegex(SystemExit, "provider capacity"):
                        runner.invoke_cli(
                            phase="implementation",
                            prompt_text="implement this task",
                            prompt_file=workdir / "prompt.md",
                            runner_config=runner.runner_config,
                        )

            self.assertTrue(mock_popen.called)
            self.assertEqual(process.stdin.written, "implement this task")
            self.assertTrue(process.stdin.closed)
            artifact_path = runner.checks_dir / "06-runner-failure.json"
            artifact_text = artifact_path.read_text(encoding="utf-8")
            artifact = json.loads(artifact_text)
            self.assertEqual(artifact["diagnosis"]["category"], "provider_capacity")
            self.assertEqual(artifact["diagnosis"]["evidence"], "selected_model_at_capacity")
            self.assertNotIn("Selected model is at capacity", artifact_text)

    def test_non_codex_runner_keeps_legacy_subprocess_failure(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "target-repo"
            workdir.mkdir()
            old_config_home = os.environ.get("KELPIE_CONFIG_HOME")
            os.environ["KELPIE_CONFIG_HOME"] = str(Path(tmpdir) / "empty-config")
            try:
                runner = WorkflowRunner(
                    repo_root=repo_root,
                    workdir=workdir,
                    issue_number=None,
                    runner_config=RunnerConfig(name="other", command_template=["other-cli"]),
                    instruction_staging_config=InstructionStagingConfig(),
                    issue_source="none",
                    task_label="legacy-runner-failure",
                )
            finally:
                if old_config_home is None:
                    os.environ.pop("KELPIE_CONFIG_HOME", None)
                else:
                    os.environ["KELPIE_CONFIG_HOME"] = old_config_home

            with patch(
                "scripts.run_issue_workflow.subprocess.run",
                return_value=SimpleNamespace(returncode=7),
            ) as mock_run:
                with patch("scripts.run_issue_workflow.subprocess.Popen") as mock_popen:
                    with self.assertRaisesRegex(SystemExit, "failed with exit code 7"):
                        runner.invoke_cli(
                            phase="implementation",
                            prompt_text="implement this task",
                            prompt_file=workdir / "prompt.md",
                            runner_config=runner.runner_config,
                        )

            self.assertTrue(mock_run.called)
            self.assertFalse(mock_popen.called)
            self.assertFalse((runner.checks_dir / "06-runner-failure.json").exists())


if __name__ == "__main__":
    unittest.main()
