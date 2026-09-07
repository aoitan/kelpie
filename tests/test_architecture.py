from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.check_architecture import check_architecture


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def rules(*, forbidden: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": "architecture-rules.v1",
        "target_patterns": ["pkg/*.py"],
        "fail_on_cycles": True,
        "fail_on_parse_errors": True,
        "fail_on_unresolved_imports": False,
        "forbidden_imports": forbidden or [],
        "regression": {"file_lines": True, "definition_spans": True},
    }


class ArchitectureLintTests(unittest.TestCase):
    def project(self) -> tuple[Path, Path, Path, Path]:
        directory = Path(tempfile.mkdtemp())
        (directory / "pkg").mkdir()
        (directory / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        rules_path = directory / "rules.json"
        baseline_path = directory / "baseline.json"
        exceptions_path = directory / "exceptions.json"
        write_json(rules_path, rules())
        write_json(
            baseline_path,
            {
                "schema_version": "architecture-baseline.v1",
                "files": {},
                "definitions": {},
                "cycles": [],
            },
        )
        write_json(
            exceptions_path,
            {"schema_version": "architecture-exceptions.v1", "exceptions": []},
        )
        return directory, rules_path, baseline_path, exceptions_path

    def test_clean_project_returns_zero(self) -> None:
        root, rules_path, baseline_path, exceptions_path = self.project()
        (root / "pkg" / "a.py").write_text("def ok():\n    return 1\n", encoding="utf-8")

        result = check_architecture(root, rules_path, baseline_path, exceptions_path)

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["violations"], [])

    def test_forbidden_import_and_cycle_return_one(self) -> None:
        root, rules_path, baseline_path, exceptions_path = self.project()
        (root / "pkg" / "a.py").write_text("from . import b\n", encoding="utf-8")
        (root / "pkg" / "b.py").write_text("from . import a\n", encoding="utf-8")
        write_json(
            rules_path,
            rules(
                forbidden=[
                    {"source": "pkg.a", "target_prefix": "pkg.b"},
                ]
            ),
        )

        result = check_architecture(root, rules_path, baseline_path, exceptions_path)

        self.assertEqual(result["exit_code"], 1)
        ids = {item["id"] for item in result["violations"]}
        self.assertIn("architecture.forbidden_import:pkg.a->pkg.b", ids)
        self.assertIn("architecture.cycle:pkg.a,pkg.b", ids)

    def test_file_and_definition_regressions_return_one(self) -> None:
        root, rules_path, baseline_path, exceptions_path = self.project()
        (root / "pkg" / "a.py").write_text(
            "class A:\n"
            "    def method(self):\n"
            "        return 1\n",
            encoding="utf-8",
        )
        write_json(
            baseline_path,
            {
                "schema_version": "architecture-baseline.v1",
                "files": {"pkg/a.py": {"physical_lines": 2}},
                "definitions": {"pkg/a.py": {"A": 1}},
                "cycles": [],
            },
        )

        result = check_architecture(root, rules_path, baseline_path, exceptions_path)

        self.assertEqual(result["exit_code"], 1)
        ids = {item["id"] for item in result["violations"]}
        self.assertIn("architecture.file_lines:pkg/a.py", ids)
        self.assertIn("architecture.definition_span:pkg/a.py:A", ids)

    def test_invalid_configuration_returns_two(self) -> None:
        root, rules_path, baseline_path, exceptions_path = self.project()
        rules_path.write_text("{}", encoding="utf-8")

        result = check_architecture(root, rules_path, baseline_path, exceptions_path)

        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["violations"], [])
        self.assertIn("error", result)

    def test_parse_errors_return_one(self) -> None:
        root, rules_path, baseline_path, exceptions_path = self.project()
        (root / "pkg" / "broken.py").write_text("def broken(:\n", encoding="utf-8")

        result = check_architecture(root, rules_path, baseline_path, exceptions_path)

        self.assertEqual(result["exit_code"], 1)
        ids = {item["id"] for item in result["violations"]}
        self.assertIn("architecture.parse_error:pkg/broken.py", ids)

    def test_expired_and_unused_exceptions_are_configuration_violations(self) -> None:
        root, rules_path, baseline_path, exceptions_path = self.project()
        (root / "pkg" / "a.py").write_text("from . import missing\n", encoding="utf-8")
        write_json(
            exceptions_path,
            {
                "schema_version": "architecture-exceptions.v1",
                "exceptions": [
                    {
                        "id": "architecture.unused",
                        "reason": "test",
                        "expires_on": "2000-01-01",
                        "task": "T01",
                    }
                ],
            },
        )

        result = check_architecture(root, rules_path, baseline_path, exceptions_path)

        self.assertEqual(result["exit_code"], 1)
        ids = {item["id"] for item in result["violations"]}
        self.assertIn("architecture.expired_exception:architecture.unused", ids)
        self.assertIn("architecture.unused_exception:architecture.unused", ids)


if __name__ == "__main__":
    unittest.main()
