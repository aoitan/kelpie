"""Deterministic AST and import metrics for the local architecture.

The report intentionally uses only the standard library.  It describes source
structure and local import relationships; it does not claim to be a complete
Python dependency graph (dynamic imports are reported as unresolved).
"""

from __future__ import annotations

import json
import ast
import hashlib
from pathlib import Path
import platform
import subprocess
import sys
from typing import Iterable, Iterator


SCHEMA_VERSION = "architecture-metrics.v1"
TOOL_VERSION = "1.0"
_NESTING_NODES = (
    ast.AsyncFor,
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.ClassDef,
    ast.For,
    ast.FunctionDef,
    ast.If,
    ast.Match,
    ast.Try,
    ast.While,
    ast.With,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_value(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if completed.returncode != 0:
        return "unavailable"
    return completed.stdout.strip() or "unavailable"


def _relative_files(root: Path, patterns: Iterable[str]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(
            path.relative_to(root)
            for path in root.glob(pattern)
            if path.is_file() and not path.is_symlink()
        )
    return sorted(paths, key=lambda path: path.as_posix())


def _module_name(relative_path: Path) -> str:
    parts = list(relative_path.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = relative_path.stem
    return ".".join(parts)


def _qualified_definitions(tree: ast.AST) -> tuple[list[dict[str, object]], int]:
    definitions: list[dict[str, object]] = []
    max_depth = 0

    def visit(node: ast.AST, prefix: tuple[str, ...], structural_depth: int) -> None:
        nonlocal max_depth
        if isinstance(node, _NESTING_NODES):
            structural_depth += 1
            max_depth = max(max_depth, structural_depth)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = ".".join((*prefix, node.name))
            definitions.append(
                {
                    "qualified_name": name,
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                    "lineno": node.lineno,
                    "end_lineno": getattr(node, "end_lineno", node.lineno),
                    "col_offset": node.col_offset,
                    "end_col_offset": getattr(node, "end_col_offset", node.col_offset),
                }
            )
            child_prefix = (*prefix, node.name)
        else:
            child_prefix = prefix
        for child in ast.iter_child_nodes(node):
            visit(child, child_prefix, structural_depth)

    visit(tree, (), 0)
    definitions.sort(key=lambda item: (int(item["lineno"]), str(item["qualified_name"])))
    return definitions, max_depth


def _under_type_checking(node: ast.AST, parents: tuple[ast.AST, ...]) -> bool:
    for parent in parents:
        if isinstance(parent, ast.If):
            test = parent.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                return True
            if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
                return True
    return False


def _under_import_error_handler(parents: tuple[ast.AST, ...]) -> bool:
    for parent in parents:
        if isinstance(parent, ast.Try):
            for handler in parent.handlers:
                if handler.type is None:
                    return True
                if isinstance(handler.type, ast.Name) and handler.type.id in {
                    "ImportError",
                    "ModuleNotFoundError",
                }:
                    return True
                if isinstance(handler.type, ast.Tuple) and any(
                    isinstance(item, ast.Name)
                    and item.id in {"ImportError", "ModuleNotFoundError"}
                    for item in handler.type.elts
                ):
                    return True
    return False


def _local_import_name(
    module: str,
    imported: str,
    level: int,
    available: set[str],
) -> str:
    if level:
        package_parts = module.split(".")[:-1]
        if level > len(package_parts) + 1:
            candidate = imported
        else:
            base = package_parts[: len(package_parts) - level + 1]
            candidate = ".".join((*base, imported)) if imported else ".".join(base)
    else:
        candidate = imported
    if candidate in available:
        return candidate
    parts = candidate.split(".")
    for index in range(len(parts), 0, -1):
        prefix = ".".join(parts[:index])
        if prefix in available:
            return prefix
    return candidate


def _string_argument(node: ast.Call) -> str | None:
    if not node.args:
        return None
    value = node.args[0]
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _iter_imports(
    tree: ast.AST,
    module: str,
    available: set[str],
) -> Iterator[dict[str, object]]:
    def visit(node: ast.AST, parents: tuple[ast.AST, ...]) -> Iterator[dict[str, object]]:
        kind = "runtime"
        if _under_type_checking(node, parents):
            kind = "type_checking"
        elif _under_import_error_handler(parents):
            kind = "fallback"
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _local_import_name(module, alias.name, 0, available)
                resolved = target in available
                yield {
                    "source": module,
                    "target": target,
                    "kind": kind,
                    "resolved": resolved,
                }
        elif isinstance(node, ast.ImportFrom):
            imported = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    target_name = imported
                else:
                    target_name = ".".join(part for part in (imported, alias.name) if part)
                target = _local_import_name(module, target_name, node.level, available)
                resolved = target in available
                yield {
                    "source": module,
                    "target": target,
                    "kind": kind,
                    "resolved": resolved,
                }
        elif isinstance(node, ast.Call):
            function = node.func
            dynamic = (
                isinstance(function, ast.Name) and function.id == "__import__"
            ) or (
                isinstance(function, ast.Attribute)
                and function.attr == "import_module"
                and isinstance(function.value, ast.Name)
                and function.value.id == "importlib"
            )
            if dynamic:
                target = _string_argument(node) or "<dynamic>"
                yield {
                    "source": module,
                    "target": target,
                    "kind": "dynamic",
                    "resolved": target in available,
                }
        for child in ast.iter_child_nodes(node):
            yield from visit(child, (*parents, node))

    yield from visit(tree, ())


def _strongly_connected_components(
    modules: list[str], edges: list[dict[str, object]]
) -> list[list[str]]:
    graph = {module: set() for module in modules}
    for edge in edges:
        if edge["resolved"] and edge["source"] in graph and edge["target"] in graph:
            graph[str(edge["source"])].add(str(edge["target"]))
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = index
        lowlinks[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)
        for target in sorted(graph[module]):
            if target not in indices:
                visit(target)
                lowlinks[module] = min(lowlinks[module], lowlinks[target])
            elif target in on_stack:
                lowlinks[module] = min(lowlinks[module], indices[target])
        if lowlinks[module] == indices[module]:
            component: list[str] = []
            while True:
                target = stack.pop()
                on_stack.remove(target)
                component.append(target)
                if target == module:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    for module in modules:
        if module not in indices:
            visit(module)
    return sorted(result)


def collect_metrics(root: Path, *, patterns: Iterable[str] = ("scripts/*.py",)) -> dict[str, object]:
    """Return the architecture metrics report for ``root``."""

    root = root.resolve()
    target_patterns = [str(pattern) for pattern in patterns]
    relative_files = _relative_files(root, target_patterns)
    modules = {_module_name(path): path for path in relative_files}
    available = set(modules)
    file_reports: list[dict[str, object]] = []
    import_edges: set[tuple[str, str, str, bool]] = set()
    unresolved: set[tuple[str, str, str, bool]] = set()
    for relative_path in relative_files:
        source = (root / relative_path).read_bytes()
        module = _module_name(relative_path)
        try:
            tree = ast.parse(source.decode("utf-8"), filename=relative_path.as_posix())
        except (SyntaxError, UnicodeDecodeError) as exc:
            file_reports.append(
                {
                    "path": relative_path.as_posix(),
                    "sha256": _sha256(source),
                    "physical_lines": source.count(b"\n"),
                    "definitions": [],
                    "max_nesting_depth": 0,
                    "parse_error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        definitions, max_depth = _qualified_definitions(tree)
        file_reports.append(
            {
                "path": relative_path.as_posix(),
                "sha256": _sha256(source),
                "physical_lines": source.count(b"\n"),
                "definitions": definitions,
                "max_nesting_depth": max_depth,
            }
        )
        for edge in _iter_imports(tree, module, available):
            edge_key = (
                str(edge["source"]),
                str(edge["target"]),
                str(edge["kind"]),
                bool(edge["resolved"]),
            )
            if edge["resolved"]:
                import_edges.add(edge_key)
            else:
                unresolved.add(edge_key)
    sorted_edges = [
        {
            "source": source,
            "target": target,
            "kind": kind,
            "resolved": resolved,
        }
        for source, target, kind, resolved in sorted(import_edges)
    ]
    unresolved_imports = [
        {
            "source": source,
            "target": target,
            "kind": kind,
            "resolved": resolved,
        }
        for source, target, kind, resolved in sorted(unresolved)
    ]
    fan_in = {module: 0 for module in sorted(available)}
    fan_out = {module: 0 for module in sorted(available)}
    for edge in sorted_edges:
        source = str(edge["source"])
        target = str(edge["target"])
        if source in fan_out and target in fan_in:
            fan_out[source] += 1
            fan_in[target] += 1
    diff = _git_value(root, "diff", "--no-ext-diff", "--no-color")
    staged = _git_value(root, "diff", "--cached", "--no-ext-diff", "--no-color")
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "commit_sha": _git_value(root, "rev-parse", "HEAD"),
        "dirty_diff_sha256": _sha256(f"{diff}\n{staged}".encode("utf-8")),
        "environment": {
            "python": platform.python_version(),
            "os": platform.platform(),
        },
        "target_patterns": target_patterns,
        "excluded_patterns": [],
        "files": file_reports,
        "import_edges": sorted_edges,
        "unresolved_imports": unresolved_imports,
        "fan_in": fan_in,
        "fan_out": fan_out,
        "cycles": _strongly_connected_components(sorted(available), sorted_edges),
    }


def metrics_json(report: dict[str, object]) -> str:
    """Render a metrics report in canonical JSON form."""

    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--pattern", action="append", dest="patterns")
    parser.add_argument("--format", choices=("json",), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = collect_metrics(args.root, patterns=args.patterns or ("scripts/*.py",))
    rendered = metrics_json(report)
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
