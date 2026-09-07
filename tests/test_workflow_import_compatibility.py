from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import sys
import unittest

import scripts.workflow_config as workflow_config


class WorkflowImportCompatibilityTests(unittest.TestCase):
    repository_root = Path(__file__).resolve().parents[1]

    def test_new_normalization_module_reuses_legacy_public_types(self) -> None:
        normalization = importlib.import_module("scripts.workflow_normalization")
        deps = workflow_config._normalization_dependencies()
        self.assertIs(deps.values["ArtifactKey"], workflow_config.ArtifactKey)
        self.assertIs(deps.values["WorkflowPlan"], workflow_config.WorkflowPlan)
        self.assertIs(deps.values["WorkflowConfigError"], workflow_config.WorkflowConfigError)
        self.assertTrue(callable(normalization.register_declarations))
        self.assertTrue(callable(normalization.resolve_references))
        self.assertTrue(callable(normalization.build_workflow_plan))

    def test_direct_and_module_cli_entrypoints_still_load(self) -> None:
        for command in (
            [sys.executable, "scripts/run_issue_workflow.py", "--help"],
            [sys.executable, "-m", "scripts.run_issue_workflow", "--help"],
        ):
            completed = subprocess.run(
                command,
                cwd=self.repository_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Run multi-phase issue workflow", completed.stdout)


if __name__ == "__main__":
    unittest.main()
