from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.run_issue_workflow import RunnerConfig, load_runner_config


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "run_opencode_with_config.sh"


class OpenCodeWrapperTests(unittest.TestCase):
    def make_fake_opencode(self, root: Path) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "opencode"
        executable.write_text(
            """#!/bin/sh
set -eu
printf 'args=%s\\n' "$*"
printf 'config=%s\\n' "$OPENCODE_CONFIG_CONTENT"
printf 'data=%s\\n' "$XDG_DATA_HOME"
printf 'cache=%s\\n' "$XDG_CACHE_HOME"
printf 'state=%s\\n' "$XDG_STATE_HOME"
printf 'autoupdate=%s\\n' "$OPENCODE_DISABLE_AUTOUPDATE"
IFS= read -r line
printf 'stdin=%s\\n' "$line"
exit "${FAKE_OPENCODE_EXIT:-0}"
""",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return bin_dir

    def run_wrapper(
        self,
        root: Path,
        config_path: Path,
        *,
        stdin: str = "stdin-marker\n",
        exit_code: str = "0",
    ) -> subprocess.CompletedProcess[str]:
        bin_dir = self.make_fake_opencode(root)
        state_dir = root / "opencode-state"
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "KELPIE_OPENCODE_CONFIG": str(config_path),
                "KELPIE_OPENCODE_STATE_DIR": str(state_dir),
                "FAKE_OPENCODE_EXIT": exit_code,
            }
        )
        return subprocess.run(
            ["sh", str(WRAPPER), "run", "--pure"],
            input=stdin,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_wrapper_passes_config_state_arguments_stdin_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = root / "opencode.json"
            config.write_text('{"marker":"SECRET_CONFIG_MARKER"}\n', encoding="utf-8")

            result = self.run_wrapper(root, config, exit_code="7")

            self.assertEqual(result.returncode, 7)
            self.assertIn("args=run --pure", result.stdout)
            self.assertIn('config={"marker":"SECRET_CONFIG_MARKER"}', result.stdout)
            self.assertIn(f"data={root / 'opencode-state' / 'data'}", result.stdout)
            self.assertIn(f"cache={root / 'opencode-state' / 'cache'}", result.stdout)
            self.assertIn(f"state={root / 'opencode-state' / 'state'}", result.stdout)
            self.assertIn("autoupdate=true", result.stdout)
            self.assertIn("stdin=stdin-marker", result.stdout)
            self.assertEqual(result.stderr, "")

    def test_wrapper_rejects_missing_config_without_leaking_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing = root / "SECRET_CONFIG_MARKER.json"

            result = self.run_wrapper(root, missing)

            self.assertEqual(result.returncode, 2)
            self.assertIn("config file is missing or unreadable", result.stderr)
            self.assertNotIn('{"marker"', result.stderr)

    def test_wrapper_rejects_non_regular_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "opencode.json"
            config_dir.mkdir()

            result = self.run_wrapper(root, config_dir)

            self.assertEqual(result.returncode, 2)


class OpenCodeConfigTests(unittest.TestCase):
    def test_example_config_has_matching_model_and_explicit_agents(self) -> None:
        payload = json.loads((REPO_ROOT / "examples" / "opencode.json").read_text(encoding="utf-8"))
        provider_id, model_id = payload["model"].split("/", 1)

        self.assertIn(model_id, payload["provider"][provider_id]["models"])
        self.assertGreaterEqual(
            payload["provider"][provider_id]["models"][model_id]["limit"]["context"],
            65536,
        )
        artifact = payload["agent"]["kelpie-artifact"]["permission"]
        workspace = payload["agent"]["kelpie-workspace"]["permission"]
        self.assertEqual(artifact["*"], "deny")
        self.assertEqual(artifact["edit"]["*"], "deny")
        self.assertEqual(artifact["edit"]["**/.kelpie/artifacts/**"], "allow")
        self.assertEqual(workspace["*"], "deny")
        self.assertEqual(workspace["bash"]["rm"], "deny")
        self.assertEqual(workspace["bash"]["rm *"], "deny")
        self.assertEqual(workspace["bash"]["git commit"], "deny")
        self.assertEqual(workspace["bash"]["git commit *"], "deny")
        self.assertEqual(workspace["bash"]["git push"], "deny")
        self.assertEqual(workspace["bash"]["git push *"], "deny")

    def test_example_config_contains_no_raw_secret_fields(self) -> None:
        text = (REPO_ROOT / "examples" / "opencode.json").read_text(encoding="utf-8").lower()

        self.assertNotIn('"apikey"', text)
        self.assertNotIn('"authorization"', text)
        self.assertNotIn("password", text)


class OpenCodeRunnerConfigTests(unittest.TestCase):
    def test_example_runner_uses_artifact_and_workspace_agents_by_phase(self) -> None:
        config = RunnerConfig.from_json(
            REPO_ROOT / "examples" / "runner_config.json",
            "opencode_ollama",
        )

        artifact = config.resolve_for_phase("solution_design")
        implementation = config.resolve_for_phase("implementation")
        review = config.resolve_for_phase("review_fix_loop")
        plan_check = config.resolve_for_phase("plan_comprehension_check")

        self.assertEqual(artifact.prompt_mode, "stdin")
        self.assertIn("kelpie-artifact", artifact.command_template)
        self.assertNotIn("--auto", artifact.command_template)
        self.assertIn("kelpie-workspace", implementation.command_template)
        self.assertIn("--auto", implementation.command_template)
        self.assertIn("kelpie-workspace", review.command_template)
        self.assertIn("--auto", review.command_template)
        self.assertEqual(plan_check.command_template[0], "codex")
        self.assertIn("gpt-5.4-mini", plan_check.command_template)

    def test_bundled_runner_is_fallback_for_existing_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            user_config = Path(tmpdir) / "runner_config.json"
            user_config.write_text(
                json.dumps(
                    {
                        "runners": {
                            "custom": {
                                "command_template": ["custom-cli"],
                                "prompt_mode": "stdin",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = load_runner_config(
                user_config,
                REPO_ROOT / "examples" / "runner_config.json",
                "opencode_ollama",
            )

            self.assertEqual(config.name, "opencode_ollama")
            self.assertEqual(config.command_template[0], "kelpie-opencode")

    def test_existing_user_runner_takes_precedence_over_bundled_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            user_config = Path(tmpdir) / "runner_config.json"
            user_config.write_text(
                json.dumps(
                    {
                        "runners": {
                            "opencode_ollama": {
                                "command_template": ["custom-opencode"],
                                "prompt_mode": "stdin",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = load_runner_config(
                user_config,
                REPO_ROOT / "examples" / "runner_config.json",
                "opencode_ollama",
            )

            self.assertEqual(config.command_template, ["custom-opencode"])

    def test_malformed_existing_user_runner_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            user_config = Path(tmpdir) / "runner_config.json"
            user_config.write_text(
                json.dumps({"runners": {"opencode_ollama": {"prompt_mode": "stdin"}}}),
                encoding="utf-8",
            )

            with self.assertRaises(KeyError):
                load_runner_config(
                    user_config,
                    REPO_ROOT / "examples" / "runner_config.json",
                    "opencode_ollama",
                )


class OpenCodeInstallTests(unittest.TestCase):
    def test_posix_install_provisions_config_and_preserves_user_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            install_home = root / "home"
            config_home = root / "config"
            bin_dir = root / "bin"
            env = os.environ.copy()
            env.update(
                {
                    "KELPIE_HOME": str(install_home),
                    "KELPIE_CONFIG_HOME": str(config_home),
                    "KELPIE_BIN_DIR": str(bin_dir),
                }
            )

            first = subprocess.run(
                ["sh", str(REPO_ROOT / "install.sh")],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            installed_config = config_home / "opencode.json"
            installed_wrapper = install_home / "scripts" / "run_opencode_with_config.sh"
            self.assertEqual(
                json.loads(installed_config.read_text(encoding="utf-8")),
                json.loads((REPO_ROOT / "examples" / "opencode.json").read_text(encoding="utf-8")),
            )
            self.assertTrue(os.access(installed_wrapper, os.X_OK))

            installed_config.write_text('{"user":"preserved"}\n', encoding="utf-8")
            second = subprocess.run(
                ["sh", str(REPO_ROOT / "install.sh")],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(installed_config.read_text(encoding="utf-8"), '{"user":"preserved"}\n')


if __name__ == "__main__":
    unittest.main()
