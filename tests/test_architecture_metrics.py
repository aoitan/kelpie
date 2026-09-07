from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.architecture_metrics import collect_metrics, metrics_json
from scripts import workflow_normalization


class ArchitectureMetricsTests(unittest.TestCase):
    def test_report_is_deterministic_and_marks_unresolved_imports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "pkg"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "a.py").write_text(
                "from . import b\n"
                "import importlib\n"
                "\n"
                "class A:\n"
                "    def method(self):\n"
                "        importlib.import_module('pkg.missing')\n",
                encoding="utf-8",
            )
            (package / "b.py").write_text(
                "def helper():\n"
                "    return 1\n",
                encoding="utf-8",
            )

            first = collect_metrics(root, patterns=("pkg/*.py",))
            second = collect_metrics(root, patterns=("pkg/*.py",))

            self.assertEqual(metrics_json(first), metrics_json(second))
            self.assertEqual(first["schema_version"], "architecture-metrics.v1")
            self.assertEqual(first["target_patterns"], ["pkg/*.py"])
            files = {item["path"]: item for item in first["files"]}
            self.assertEqual(files["pkg/a.py"]["physical_lines"], 6)
            definitions = files["pkg/a.py"]["definitions"]
            self.assertEqual(
                [item["qualified_name"] for item in definitions],
                ["A", "A.method"],
            )
            self.assertGreaterEqual(files["pkg/a.py"]["max_nesting_depth"], 2)

            edges = first["import_edges"]
            self.assertIn(
                {
                    "source": "pkg.a",
                    "target": "pkg.b",
                    "kind": "runtime",
                    "resolved": True,
                },
                edges,
            )
            unresolved = first["unresolved_imports"]
            self.assertTrue(
                any(item["source"] == "pkg.a" and item["kind"] == "dynamic" for item in unresolved)
            )

    def test_repository_report_contains_normalizer_and_runner_metrics(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = collect_metrics(root, patterns=("scripts/*.py",))

        files = {item["path"]: item for item in report["files"]}
        self.assertIn("scripts/workflow_config.py", files)
        self.assertIn("scripts/run_issue_workflow.py", files)
        normalizer = {
            item["qualified_name"]: item
            for item in files["scripts/workflow_config.py"]["definitions"]
        }["normalize_workflow_config"]
        self.assertLess(normalizer["end_lineno"] - normalizer["lineno"], 50)
        extracted = {
            item["qualified_name"]: item
            for item in files["scripts/workflow_normalization.py"]["definitions"]
        }
        self.assertIn("_ReferenceResolutionWorkspace", extracted)
        self.assertNotIn("_normalize_pipeline", extracted)
        self.assertLess(
            extracted["_ReferenceResolutionWorkspace.resolve_step"]["end_lineno"]
            - extracted["_ReferenceResolutionWorkspace.resolve_step"]["lineno"],
            100,
        )
        self.assertLess(
            extracted["build_workflow_plan"]["end_lineno"]
            - extracted["build_workflow_plan"]["lineno"],
            150,
        )
        self.assertLess(
            extracted["normalize_declaration"]["end_lineno"]
            - extracted["normalize_declaration"]["lineno"],
            50,
        )
        self.assertEqual(workflow_normalization.normalize_declaration.__module__, "scripts.workflow_normalization")
        runner_names = [
            item["qualified_name"]
            for item in files["scripts/run_issue_workflow.py"]["definitions"]
        ]
        self.assertIn("WorkflowRunner", runner_names)

    def test_metrics_json_is_canonical_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.py").write_text("value = 1\n", encoding="utf-8")
            report = collect_metrics(root, patterns=("one.py",))

            encoded = metrics_json(report)
            self.assertEqual(json.loads(encoded), report)
            self.assertTrue(encoded.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
