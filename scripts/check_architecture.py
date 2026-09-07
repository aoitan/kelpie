"""Architecture lint gate backed by the deterministic metrics report."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys
from typing import Any

try:
    from scripts.architecture_metrics import collect_metrics, metrics_json
except ModuleNotFoundError:  # pragma: no cover - direct script-style imports
    from architecture_metrics import collect_metrics, metrics_json  # type: ignore


RULES_SCHEMA_VERSION = "architecture-rules.v1"
BASELINE_SCHEMA_VERSION = "architecture-baseline.v1"
EXCEPTIONS_SCHEMA_VERSION = "architecture-exceptions.v1"
CHECK_SCHEMA_VERSION = "architecture-check.v1"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _require_schema(value: dict[str, Any], expected: str, path: Path) -> None:
    if value.get("schema_version") != expected:
        raise ValueError(f"{path} has unsupported schema_version")


def _violation(identifier: str, message: str, **details: object) -> dict[str, object]:
    return {"id": identifier, "message": message, **details}


def _exception_ids(exceptions: list[dict[str, Any]]) -> set[str]:
    return {str(item["id"]) for item in exceptions}


def check_architecture(
    root: Path,
    rules_path: Path,
    baseline_path: Path,
    exceptions_path: Path,
) -> dict[str, Any]:
    try:
        rules = _read_json(rules_path)
        baseline = _read_json(baseline_path)
        exception_document = _read_json(exceptions_path)
        _require_schema(rules, RULES_SCHEMA_VERSION, rules_path)
        _require_schema(baseline, BASELINE_SCHEMA_VERSION, baseline_path)
        _require_schema(exception_document, EXCEPTIONS_SCHEMA_VERSION, exceptions_path)
        target_patterns = rules.get("target_patterns")
        if not isinstance(target_patterns, list) or any(not isinstance(item, str) for item in target_patterns):
            raise ValueError("rules.target_patterns must be a list[str]")
        forbidden = rules.get("forbidden_imports", [])
        if not isinstance(forbidden, list) or any(not isinstance(item, dict) for item in forbidden):
            raise ValueError("rules.forbidden_imports must be a list[object]")
        exceptions = exception_document.get("exceptions", [])
        if not isinstance(exceptions, list) or any(not isinstance(item, dict) for item in exceptions):
            raise ValueError("exceptions.exceptions must be a list[object]")
        for item in exceptions:
            for field in ("id", "reason", "expires_on", "task"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise ValueError(f"exception requires non-empty {field}")
            date.fromisoformat(item["expires_on"])
        metrics = collect_metrics(root, patterns=target_patterns)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "schema_version": CHECK_SCHEMA_VERSION,
            "exit_code": 2,
            "violations": [],
            "error": str(exc),
        }

    violations: list[dict[str, object]] = []
    if rules.get("fail_on_parse_errors", True):
        for file_report in metrics["files"]:
            parse_error = file_report.get("parse_error")
            if isinstance(parse_error, str):
                path = str(file_report["path"])
                violations.append(
                    _violation(
                        f"architecture.parse_error:{path}",
                        f"could not parse {path}: {parse_error}",
                        path=path,
                        parse_error=parse_error,
                    )
                )
    for edge in metrics["import_edges"]:
        for rule in forbidden:
            source = rule.get("source")
            target_prefix = rule.get("target_prefix")
            target_prefixes = rule.get("target_prefixes", [])
            if not isinstance(source, str):
                continue
            prefixes = [target_prefix] if isinstance(target_prefix, str) else []
            if isinstance(target_prefixes, list):
                prefixes.extend(item for item in target_prefixes if isinstance(item, str))
            if edge["source"] == source and any(str(edge["target"]).startswith(prefix) for prefix in prefixes):
                identifier = f"architecture.forbidden_import:{edge['source']}->{edge['target']}"
                violations.append(
                    _violation(
                        identifier,
                        f"forbidden import from {edge['source']} to {edge['target']}",
                        source=edge["source"],
                        target=edge["target"],
                    )
                )

    if rules.get("fail_on_cycles", True):
        baseline_cycles = {
            tuple(str(item) for item in cycle)
            for cycle in baseline.get("cycles", [])
            if isinstance(cycle, list)
        }
        for cycle in metrics["cycles"]:
            signature = tuple(str(item) for item in cycle)
            if signature not in baseline_cycles:
                identifier = f"architecture.cycle:{','.join(signature)}"
                violations.append(_violation(identifier, f"local import cycle: {', '.join(signature)}"))

    regression = rules.get("regression", {})
    if not isinstance(regression, dict):
        return {
            "schema_version": CHECK_SCHEMA_VERSION,
            "exit_code": 2,
            "violations": [],
            "error": "rules.regression must be an object",
        }
    files_baseline = baseline.get("files", {})
    if regression.get("file_lines", True) and isinstance(files_baseline, dict):
        current_files = {str(item["path"]): item for item in metrics["files"]}
        for path, limits in files_baseline.items():
            if not isinstance(limits, dict) or path not in current_files:
                continue
            limit = limits.get("physical_lines")
            current = current_files[path].get("physical_lines")
            if isinstance(limit, int) and isinstance(current, int) and current > limit:
                violations.append(
                    _violation(
                        f"architecture.file_lines:{path}",
                        f"{path} grew from baseline {limit} to {current} lines",
                        path=path,
                        baseline=limit,
                        current=current,
                    )
                )

    definitions_baseline = baseline.get("definitions", {})
    if regression.get("definition_spans", True) and isinstance(definitions_baseline, dict):
        current_definitions = {
            str(file["path"]): {
                str(item["qualified_name"]): int(item["end_lineno"]) - int(item["lineno"]) + 1
                for item in file["definitions"]
            }
            for file in metrics["files"]
        }
        for path, limits in definitions_baseline.items():
            if not isinstance(limits, dict):
                continue
            for name, limit in limits.items():
                current = current_definitions.get(path, {}).get(name)
                if isinstance(limit, int) and isinstance(current, int) and current > limit:
                    violations.append(
                        _violation(
                            f"architecture.definition_span:{path}:{name}",
                            f"{path}:{name} grew from baseline {limit} to {current} lines",
                            path=path,
                            qualified_name=name,
                            baseline=limit,
                            current=current,
                        )
                    )

    if rules.get("fail_on_unresolved_imports", False):
        for edge in metrics["unresolved_imports"]:
            identifier = f"architecture.unresolved_import:{edge['source']}->{edge['target']}"
            violations.append(_violation(identifier, f"unresolved import: {edge['source']} -> {edge['target']}"))

    violations.sort(key=lambda item: str(item["id"]))
    ids = _exception_ids(exceptions)
    used: set[str] = set()
    filtered: list[dict[str, object]] = []
    for item in violations:
        identifier = str(item["id"])
        if identifier in ids:
            used.add(identifier)
        else:
            filtered.append(item)
    today = date.today()
    for item in exceptions:
        identifier = str(item["id"])
        expires = date.fromisoformat(str(item["expires_on"]))
        if expires < today:
            filtered.append(
                _violation(
                    f"architecture.expired_exception:{identifier}",
                    f"architecture exception {identifier} expired on {expires.isoformat()}",
                )
            )
        if identifier not in used:
            filtered.append(
                _violation(
                    f"architecture.unused_exception:{identifier}",
                    f"architecture exception {identifier} does not match a violation",
                )
            )
    filtered.sort(key=lambda item: str(item["id"]))
    return {
        "schema_version": CHECK_SCHEMA_VERSION,
        "exit_code": 1 if filtered else 0,
        "violations": filtered,
        "metrics": metrics,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--exceptions", type=Path)
    parser.add_argument("--format", choices=("json",), default="json")
    parser.add_argument("--metrics-output", type=Path)
    parser.add_argument("--diff-output", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    result = check_architecture(
        root,
        args.rules or root / "architecture" / "rules.json",
        args.baseline or root / "architecture" / "baseline.json",
        args.exceptions or root / "architecture" / "exceptions.json",
    )
    if args.metrics_output is not None and isinstance(result.get("metrics"), dict):
        args.metrics_output.write_text(metrics_json(result["metrics"]), encoding="utf-8")
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.diff_output is not None:
        diff_lines = [str(item["id"]) + ": " + str(item["message"]) for item in result["violations"]]
        args.diff_output.write_text("\n".join(diff_lines) + ("\n" if diff_lines else ""), encoding="utf-8")
    print(rendered, end="")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
