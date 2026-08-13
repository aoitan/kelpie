"""Single-change iteration orchestration and provenance support.

The regular phase runner deliberately remains separate from this module.  A
single-change iteration reserves an immutable artifact scope, records intent
and repository state before invoking its executor, runs bounded checks, and
returns a structured terminal result without selecting another iteration.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = "1.0"
TARGET_KINDS = frozenset({"work_item", "acceptance_criterion", "review_finding"})
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ITERATION_PATTERN = re.compile(r"^[0-9]{4}$")
MAX_INTENT_LENGTH = 4096
MAX_SOURCE_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_CHECK_TIMEOUT_SECONDS = 600
DEFAULT_CHECK_OUTPUT_LIMIT_BYTES = 1024 * 1024
MAX_CHECK_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        ) if os.name != "nt" else None
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(value)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
            temporary_path = None
            if descriptor is not None:
                os.fsync(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _exclusive_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    _atomic_write_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode("utf-8"),
    )


def _exclusive_write_json(path: Path, payload: Mapping[str, object]) -> None:
    _exclusive_write_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode("utf-8"),
    )


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _has_symlink_component(root: Path, path: Path) -> bool:
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _has_symlink_parent_component(root: Path, path: Path) -> bool:
    """Return whether a path parent traverses a symlink below ``root``."""

    return _has_symlink_component(root, path.parent)


def _safe_relative_path(
    value: str,
    field_name: str,
    *,
    allow_dot: bool = False,
    allow_glob: bool = False,
) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty repository-relative path")
    if allow_dot and value == ".":
        return value
    if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
        raise ValueError(f"Invalid {field_name}: {value}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Invalid {field_name}: {value}")
    if not allow_glob and any(character in value for character in "*?["):
        raise ValueError(f"Invalid {field_name}: glob patterns are not allowed")
    # Git permits spaces, Unicode, and punctuation in path components.  Keep
    # those names usable while rejecting control characters that would make
    # status records, patch headers, or markdown summaries ambiguous.
    if any(any(ord(character) < 0x20 or ord(character) == 0x7F for character in part)
           for part in parts):
        raise ValueError(f"Invalid {field_name}: {value}")
    return "/".join(parts)


def _safe_repo_path(
    workdir: Path,
    relative_path: str,
    field_name: str,
    *,
    allow_final_symlink: bool = False,
) -> Path:
    relative = _safe_relative_path(relative_path, field_name)
    candidate = workdir.joinpath(*relative.split("/"))
    root = workdir.resolve()
    # Check symlinks before resolving the candidate so callers can distinguish
    # a rejected symlink from an ordinary path traversal.
    if _has_symlink_component(root, candidate):
        if not (
            allow_final_symlink
            and candidate.is_symlink()
            and not _has_symlink_parent_component(root, candidate)
        ):
            raise ValueError(f"Invalid {field_name}: symlink components are not allowed")
    if allow_final_symlink and candidate.is_symlink():
        return candidate
    canonical = candidate.resolve(strict=False)
    if not _path_is_relative_to(canonical, root):
        raise ValueError(f"Invalid {field_name}: path escapes repository root")
    return candidate


def _assert_safe_artifact_path(root: Path, path: Path) -> None:
    """Reject artifact paths that escape or traverse a symlink.

    Iteration directories are immutable namespaces.  A malicious or stale
    symlink below one of them must not turn an atomic write into a write to an
    unrelated location.
    """

    if root.is_symlink():
        raise ValueError(f"Artifact root must not be a symlink: {root}")
    root_canonical = root.resolve(strict=False)
    candidate = path if path.is_absolute() else root / path
    if not _path_is_relative_to(candidate.resolve(strict=False), root_canonical):
        raise ValueError(f"Artifact path escapes artifact root: {path}")
    if _has_symlink_component(root_canonical, candidate):
        raise ValueError(f"Artifact path contains a symlink component: {path}")


@dataclass(frozen=True)
class ActiveTarget:
    kind: str
    id: str
    source_ref: str

    def to_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "source_ref": self.source_ref}


@dataclass(frozen=True)
class CheckSpec:
    argv: tuple[str, ...] | list[str]
    cwd: str = "."
    timeout_seconds: int = 60
    env_allowlist: tuple[str, ...] | list[str] = ()
    output_limit_bytes: int = DEFAULT_CHECK_OUTPUT_LIMIT_BYTES

    def __post_init__(self) -> None:
        if isinstance(self.argv, list):
            object.__setattr__(self, "argv", tuple(self.argv))
        if isinstance(self.env_allowlist, list):
            object.__setattr__(self, "env_allowlist", tuple(self.env_allowlist))

    def to_payload(self) -> dict[str, object]:
        return {
            "argv": list(self.argv) if isinstance(self.argv, tuple) else self.argv,
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "env_allowlist": list(self.env_allowlist)
            if isinstance(self.env_allowlist, tuple)
            else self.env_allowlist,
            "output_limit_bytes": self.output_limit_bytes,
        }


@dataclass(frozen=True)
class SingleChangeRequest:
    work_item_id: str
    active_targets: tuple[ActiveTarget, ...] | list[ActiveTarget]
    change_intent: str
    allowed_paths: tuple[str, ...] | list[str]
    checks: tuple[CheckSpec, ...] | list[CheckSpec] = ()

    def __post_init__(self) -> None:
        if isinstance(self.active_targets, list):
            object.__setattr__(self, "active_targets", tuple(self.active_targets))
        if isinstance(self.allowed_paths, list):
            object.__setattr__(self, "allowed_paths", tuple(self.allowed_paths))
        if isinstance(self.checks, list):
            object.__setattr__(self, "checks", tuple(self.checks))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "SingleChangeRequest":
        if not isinstance(raw, Mapping):
            raise ValueError("single-change request must be an object")
        raw_targets = raw.get("active_targets", ())
        if not isinstance(raw_targets, (tuple, list)):
            raise ValueError("active_targets must be a sequence")
        targets: list[ActiveTarget] = []
        for item in raw_targets:
            if not isinstance(item, Mapping):
                raise ValueError("active_targets entries must be objects")
            targets.append(
                ActiveTarget(
                    kind=item.get("kind", ""),
                    id=item.get("id", ""),
                    source_ref=item.get("source_ref", ""),
                )
            )
        raw_checks = raw.get("checks", ())
        if not isinstance(raw_checks, (tuple, list)):
            raise ValueError("checks must be a sequence")
        checks: list[CheckSpec] = []
        for item in raw_checks:
            if not isinstance(item, Mapping):
                raise ValueError("checks entries must be objects")
            checks.append(
                CheckSpec(
                    argv=item.get("argv", ()),
                    cwd=item.get("cwd", "."),
                    timeout_seconds=item.get("timeout_seconds", 60),
                    env_allowlist=item.get("env_allowlist", ()),
                    output_limit_bytes=item.get(
                        "output_limit_bytes", DEFAULT_CHECK_OUTPUT_LIMIT_BYTES
                    ),
                )
            )
        return cls(
            work_item_id=raw.get("work_item_id", ""),
            active_targets=targets,
            change_intent=raw.get("change_intent", ""),
            allowed_paths=raw.get("allowed_paths", ()),
            checks=checks,
        )


@dataclass(frozen=True)
class IterationScope:
    iteration_id: str
    path: Path
    work_item_id: str


@dataclass(frozen=True)
class IterationResult:
    iteration_id: str
    iteration_dir: Path
    status: str
    outcome: dict[str, object]
    error: str | None = None


class SingleChangeRequestValidator:
    """Validate and canonicalize requests without creating artifacts."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir.resolve()

    def validate(self, request: SingleChangeRequest) -> SingleChangeRequest:
        if not isinstance(request, SingleChangeRequest):
            raise TypeError("request must be a SingleChangeRequest")
        work_item_id = self._validate_id(request.work_item_id, "work_item_id")

        if not isinstance(request.active_targets, (tuple, list)):
            raise ValueError("active_targets must be a sequence")
        if len(request.active_targets) != 1:
            raise ValueError("active_targets must contain exactly one target")
        target = self._validate_target(request.active_targets[0])

        if not isinstance(request.change_intent, str) or not request.change_intent.strip():
            raise ValueError("change_intent must be a non-empty string")
        change_intent = request.change_intent.strip()
        if len(change_intent) > MAX_INTENT_LENGTH:
            raise ValueError(f"change_intent exceeds {MAX_INTENT_LENGTH} characters")

        if not isinstance(request.allowed_paths, (tuple, list)) or not request.allowed_paths:
            raise ValueError("allowed_paths must contain at least one exact path")
        allowed_paths: list[str] = []
        seen_paths: set[str] = set()
        for raw_path in request.allowed_paths:
            if not isinstance(raw_path, str):
                raise ValueError("allowed_paths must be a sequence of strings")
            canonical = _safe_repo_path(self.workdir, raw_path, "allowed path")
            relative = canonical.relative_to(self.workdir).as_posix()
            if self._is_reserved_repository_path(relative):
                raise ValueError(
                    "allowed path must not target repository metadata or Kelpie artifacts"
                )
            if canonical.exists() and canonical.is_dir():
                raise ValueError(f"allowed path must identify a file: {raw_path}")
            if relative in seen_paths:
                raise ValueError(f"allowed_paths contains a duplicate: {relative}")
            seen_paths.add(relative)
            allowed_paths.append(relative)

        if not isinstance(request.checks, (tuple, list)):
            raise ValueError("checks must be a sequence")
        checks = tuple(self._validate_check(check) for check in request.checks)
        return SingleChangeRequest(
            work_item_id=work_item_id,
            active_targets=(target,),
            change_intent=change_intent,
            allowed_paths=tuple(allowed_paths),
            checks=checks,
        )

    def _validate_target(self, target: object) -> ActiveTarget:
        if isinstance(target, Mapping):
            target = ActiveTarget(
                kind=target.get("kind", ""),
                id=target.get("id", ""),
                source_ref=target.get("source_ref", ""),
            )
        if not isinstance(target, ActiveTarget):
            raise ValueError("active target must be an ActiveTarget")
        if not isinstance(target.kind, str) or target.kind not in TARGET_KINDS:
            raise ValueError(f"unsupported active target kind: {target.kind}")
        target_id = self._validate_id(target.id, "active target id")
        if not isinstance(target.source_ref, str) or not target.source_ref.strip():
            raise ValueError("active target source_ref must be a non-empty string")
        if len(target.source_ref) > MAX_INTENT_LENGTH:
            raise ValueError("active target source_ref is too long")
        return ActiveTarget(target.kind, target_id, target.source_ref.strip())

    @staticmethod
    def _is_reserved_repository_path(relative: str) -> bool:
        return (
            relative == ".git"
            or relative.startswith(".git/")
            or relative == ".kelpie"
            or relative.startswith(".kelpie/")
        )

    @staticmethod
    def _validate_id(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"{field_name} must match [A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}"
            )
        return value

    def _validate_check(self, check: object) -> CheckSpec:
        if isinstance(check, Mapping):
            check = CheckSpec(
                argv=check.get("argv", ()),
                cwd=check.get("cwd", "."),
                timeout_seconds=check.get("timeout_seconds", 60),
                env_allowlist=check.get("env_allowlist", ()),
                output_limit_bytes=check.get(
                    "output_limit_bytes", DEFAULT_CHECK_OUTPUT_LIMIT_BYTES
                ),
            )
        if not isinstance(check, CheckSpec):
            raise ValueError("checks must contain CheckSpec values")
        if isinstance(check.argv, str) or not isinstance(check.argv, (tuple, list)):
            raise ValueError("check argv must be a list/tuple, not a shell string")
        argv = tuple(check.argv)
        if (
            not argv
            or not all(isinstance(part, str) for part in argv)
            or not argv[0]
            or any("\x00" in part for part in argv)
        ):
            raise ValueError("check argv must be a non-empty sequence of strings")

        cwd = check.cwd
        if not isinstance(cwd, str):
            raise ValueError("check cwd must be a repository-relative directory")
        if cwd == ".":
            canonical_cwd = "."
            cwd_path = self.workdir
        else:
            cwd_path = _safe_repo_path(self.workdir, cwd, "check cwd")
            canonical_cwd = cwd_path.relative_to(self.workdir).as_posix()
        if not cwd_path.is_dir():
            raise ValueError(f"check cwd must be an existing directory: {cwd}")

        timeout = check.timeout_seconds
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("check timeout_seconds must be a positive integer")
        if timeout > MAX_CHECK_TIMEOUT_SECONDS:
            raise ValueError(
                f"check timeout_seconds must be <= {MAX_CHECK_TIMEOUT_SECONDS}"
            )

        if not isinstance(check.env_allowlist, (tuple, list)):
            raise ValueError("check env_allowlist must be a sequence of names")
        env_names: list[str] = []
        for name in check.env_allowlist:
            if not isinstance(name, str) or not ENV_NAME_PATTERN.fullmatch(name):
                raise ValueError(f"invalid check environment name: {name}")
            if name not in env_names:
                env_names.append(name)

        output_limit = check.output_limit_bytes
        if (
            not isinstance(output_limit, int)
            or isinstance(output_limit, bool)
            or output_limit <= 0
            or output_limit > MAX_CHECK_OUTPUT_LIMIT_BYTES
        ):
            raise ValueError(
                "check output_limit_bytes must be between 1 and "
                f"{MAX_CHECK_OUTPUT_LIMIT_BYTES}"
            )
        return CheckSpec(
            argv=argv,
            cwd=canonical_cwd,
            timeout_seconds=timeout,
            env_allowlist=tuple(env_names),
            output_limit_bytes=output_limit,
        )


class IterationStore:
    """Reserve immutable iteration directories and persist lifecycle state."""

    def __init__(self, artifact_root: Path, work_item_id: str) -> None:
        if not isinstance(work_item_id, str) or not ID_PATTERN.fullmatch(work_item_id):
            raise ValueError("work_item_id has an unsafe artifact namespace")
        if artifact_root.is_symlink():
            raise ValueError(f"Artifact root must not be a symlink: {artifact_root}")
        self.artifact_root = artifact_root.resolve()
        self.work_item_id = work_item_id
        self.work_item_root = self.artifact_root / "work-items" / work_item_id
        self.iterations_root = self.work_item_root / "iterations"

    @contextmanager
    def _work_item_lock(self) -> Iterable[None]:
        _assert_safe_artifact_path(self.artifact_root, self.work_item_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        if self.work_item_root.is_symlink() or self.iterations_root.is_symlink():
            raise ValueError("single-change work-item scope must not contain symlinks")
        self.iterations_root.mkdir(parents=True, exist_ok=True)
        _assert_safe_artifact_path(self.artifact_root, self.iterations_root)
        lock_path = self.work_item_root / ".work-item-lock"
        if lock_path.is_symlink():
            raise ValueError(f"single-change lock path must not be a symlink: {lock_path}")
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        open_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, open_flags, 0o600)
        except FileExistsError as exc:
            raise RuntimeError(
                f"single-change work item is already locked: {self.work_item_id}"
            ) from exc
        try:
            owner = f"work_item={self.work_item_id}\npid={os.getpid()}\n"
            os.write(descriptor, owner.encode("utf-8"))
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            yield
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            lock_path.unlink(missing_ok=True)

    def reserve(self) -> IterationScope:
        with self._work_item_lock():
            for number in range(1, 10000):
                iteration_id = f"{number:04d}"
                scope_path = self.iterations_root / iteration_id
                if scope_path.is_symlink():
                    raise ValueError(f"iteration scope is a symlink: {scope_path}")
                try:
                    scope_path.mkdir(exist_ok=False)
                except FileExistsError:
                    continue
                _assert_safe_artifact_path(self.artifact_root, scope_path)
                lifecycle = {
                    "schema_version": SCHEMA_VERSION,
                    "work_item_id": self.work_item_id,
                    "iteration_id": iteration_id,
                    "state": "reserved",
                    "history": [{"state": "reserved", "at": _utc_now()}],
                }
                _exclusive_write_json(scope_path / "lifecycle.json", lifecycle)
                return IterationScope(iteration_id, scope_path, self.work_item_id)
        raise RuntimeError("single-change iteration number exhausted")

    def write_intent(self, scope: IterationScope, payload: Mapping[str, object]) -> None:
        _assert_safe_artifact_path(self.artifact_root, scope.path)
        _exclusive_write_json(scope.path / "intent.json", payload)

    def read_lifecycle(self, scope: IterationScope) -> dict[str, object]:
        _assert_safe_artifact_path(self.artifact_root, scope.path)
        raw = json.loads((scope.path / "lifecycle.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("lifecycle.json must contain an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported lifecycle schema version")
        if raw.get("work_item_id") != self.work_item_id:
            raise ValueError("lifecycle work item does not match its store")
        if raw.get("iteration_id") != scope.iteration_id or not ITERATION_PATTERN.fullmatch(
            scope.iteration_id
        ):
            raise ValueError("lifecycle iteration does not match its scope")
        return raw

    def transition(
        self,
        scope: IterationScope,
        state: str,
        *,
        terminal_status: str | None = None,
        reason_codes: Sequence[str] = (),
    ) -> dict[str, object]:
        _assert_safe_artifact_path(self.artifact_root, scope.path)
        lifecycle = self.read_lifecycle(scope)
        current = lifecycle.get("state")
        allowed: dict[str, set[str]] = {
            "reserved": {"in_progress", "terminal"},
            "in_progress": {"source_changed", "terminal"},
            "source_changed": {"checked", "terminal"},
            "checked": {"terminal"},
            "terminal": set(),
        }
        if current not in allowed or state not in allowed[current]:
            raise ValueError(f"invalid lifecycle transition: {current} -> {state}")
        if state == "terminal" and terminal_status not in {
            "succeeded",
            "completed_with_findings",
            "failed",
            "interrupted",
        }:
            raise ValueError("terminal lifecycle transition requires a status")
        history = lifecycle.get("history")
        if not isinstance(history, list):
            raise ValueError("lifecycle history must be a list")
        updated = dict(lifecycle)
        updated["state"] = state
        updated["history"] = [*history, {"state": state, "at": _utc_now()}]
        if state == "terminal":
            updated["status"] = terminal_status
            updated["reason_codes"] = list(reason_codes)
            updated["terminal_at"] = _utc_now()
        _atomic_write_json(scope.path / "lifecycle.json", updated)
        return updated


@dataclass
class GitSnapshot:
    boundary: str
    directory: Path
    repository: dict[str, object]
    paths: dict[str, dict[str, object]]
    status_paths: tuple[str, ...]

    def record(self, path: str) -> dict[str, object]:
        return self.paths.get(
            path,
            {
                "path": path,
                "present": False,
                "kind": "missing",
                "content_sha256": None,
                "size": 0,
                "mode": None,
                "image": None,
                "unsupported": None,
            },
        )

    def unsupported_for(self, path: str) -> str | None:
        value = self.record(path).get("unsupported")
        return str(value) if value else None


class GitStateCapture:
    """Capture NUL-safe Git status and content-aware worktree state."""

    def __init__(
        self,
        workdir: Path,
        *,
        excluded_paths: Iterable[str] = (),
        max_capture_bytes: int = MAX_SOURCE_CAPTURE_BYTES,
    ) -> None:
        self.workdir = workdir.resolve()
        normalized_exclusions: set[str] = set()
        for excluded in excluded_paths:
            normalized_exclusions.add(
                _safe_relative_path(str(excluded), "excluded path", allow_dot=True)
            )
        self.excluded_paths = tuple(sorted(normalized_exclusions))
        self.max_capture_bytes = max_capture_bytes

    def _git(self, arguments: Sequence[str], *, check: bool = True) -> bytes:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(self.workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"git {' '.join(arguments)} failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        return completed.stdout

    def capture(
        self,
        boundary_directory: Path,
        *,
        known_paths: Iterable[str] = (),
    ) -> GitSnapshot:
        if boundary_directory.is_symlink():
            raise ValueError(f"Git boundary directory must not be a symlink: {boundary_directory}")
        boundary_directory.mkdir(parents=True, exist_ok=True)
        if boundary_directory.is_symlink():
            raise ValueError(f"Git boundary directory must not be a symlink: {boundary_directory}")
        root_text = self._git(["rev-parse", "--show-toplevel"]).decode("utf-8", "replace").strip()
        repository_root = Path(root_text).resolve()
        if repository_root != self.workdir:
            raise RuntimeError(
                f"Git repository root does not match workdir: {repository_root} != {self.workdir}"
            )

        head_result = self._git(["rev-parse", "--verify", "HEAD"], check=False)
        head = head_result.decode("ascii", "replace").strip() or None
        git_dir = self._git(["rev-parse", "--git-dir"]).decode("utf-8", "replace").strip()
        git_dir_path = Path(git_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = self.workdir / git_dir_path
        index_result = self._git(["rev-parse", "--git-path", "index"], check=False)
        index_path: Path | None = None
        index_sha256: str | None = None
        if index_result:
            index_path = Path(index_result.decode("utf-8", "replace").strip())
            if not index_path.is_absolute():
                index_path = self.workdir / index_path
            if index_path.is_file():
                index_sha256 = _sha256_file(index_path)
        index_tree_result = self._git(["write-tree"], check=False)
        index_tree = index_tree_result.decode("ascii", "replace").strip() or None
        index_error = None
        if index_tree is None:
            index_error = "git write-tree failed; index may contain an unsupported state"

        status_bytes = self._git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        )
        _atomic_write_bytes(boundary_directory / "status.z", status_bytes)
        status_entries = self._parse_status(status_bytes)
        status_by_path: dict[str, dict[str, object]] = {}
        status_paths: set[str] = set()
        for entry in status_entries:
            path = str(entry["path"])
            if self._is_excluded(path):
                continue
            status_paths.add(path)
            status_by_path[path] = entry

        path_names = {
            path
            for path in known_paths
            if isinstance(path, str) and path and not self._is_excluded(path)
        }
        path_names.update(status_paths)
        paths: dict[str, dict[str, object]] = {}
        images_directory = boundary_directory / "images"
        if images_directory.is_symlink():
            raise ValueError(f"Git image directory must not be a symlink: {images_directory}")
        images_directory.mkdir(parents=True, exist_ok=True)
        if images_directory.is_symlink():
            raise ValueError(f"Git image directory must not be a symlink: {images_directory}")
        for relative_path in sorted(path_names):
            entry = status_by_path.get(relative_path, {})
            paths[relative_path] = self._capture_path(
                relative_path,
                images_directory,
                status=str(entry.get("xy", "")),
                status_unsupported=(
                    entry.get("unsupported")
                    if isinstance(entry.get("unsupported"), str)
                    and entry.get("unsupported")
                    else None
                ),
            )

        repository_payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "boundary": boundary_directory.name,
            "captured_at": _utc_now(),
            "worktree": str(self.workdir),
            "git_dir": str(git_dir_path),
            "head": head,
            "index_sha256": index_sha256,
            "index_tree": index_tree,
            "index_error": index_error,
            "status_ref": "status.z",
            "status_sha256": _sha256_bytes(status_bytes),
            "excluded_paths": list(self.excluded_paths),
            "capture_policy": {
                "max_capture_bytes": self.max_capture_bytes,
                "regular_utf8_text_only": True,
            },
        }
        _atomic_write_json(boundary_directory / "repository.json", repository_payload)
        _atomic_write_json(
            boundary_directory / "paths.json",
            {
                "schema_version": SCHEMA_VERSION,
                "paths": list(paths.values()),
                "status_paths": sorted(status_paths),
            },
        )
        return GitSnapshot(
            boundary=boundary_directory.name,
            directory=boundary_directory,
            repository=repository_payload,
            paths=paths,
            status_paths=tuple(sorted(status_paths)),
        )

    def _capture_path(
        self,
        relative_path: str,
        images_directory: Path,
        *,
        status: str,
        status_unsupported: str | None,
    ) -> dict[str, object]:
        base: dict[str, object] = {
            "path": relative_path,
            "status": status,
            "present": False,
            "kind": "missing",
            "mode": None,
            "size": 0,
            "content_sha256": None,
            "image": None,
            "unsupported": status_unsupported,
        }
        try:
            source_path = _safe_repo_path(
                self.workdir,
                relative_path,
                "Git status path",
                allow_final_symlink=True,
            )
        except ValueError as exc:
            base["unsupported"] = str(exc)
            return base

        if not os.path.lexists(source_path):
            return base
        if _has_symlink_component(self.workdir, source_path):
            base.update(
                {
                    "present": True,
                    "kind": "symlink",
                    "unsupported": status_unsupported or "symlink path",
                }
            )
            try:
                base["link_target"] = os.readlink(source_path)
            except OSError:
                pass
            return base

        stat_result = source_path.stat()
        base.update(
            {
                "present": True,
                "mode": stat_result.st_mode & 0o7777,
                "size": stat_result.st_size,
            }
        )
        if source_path.is_dir():
            base.update(
                {
                    "kind": "directory",
                    "unsupported": status_unsupported or "directory path",
                }
            )
            return base
        if not source_path.is_file():
            base.update(
                {
                    "kind": "special",
                    "unsupported": status_unsupported or "special file",
                }
            )
            return base

        base["kind"] = "regular"
        base["content_sha256"] = _sha256_file(source_path)
        if stat_result.st_size > self.max_capture_bytes:
            base["unsupported"] = status_unsupported or "oversize file"
            return base
        content = source_path.read_bytes()
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            base["unsupported"] = status_unsupported or "binary or non-UTF-8 file"
            return base
        if b"\x00" in content:
            base["unsupported"] = status_unsupported or "binary file"
            return base

        image_name = f"{_sha256_bytes(relative_path.encode('utf-8', 'surrogateescape'))}.bin"
        image_path = images_directory / image_name
        _atomic_write_bytes(image_path, content)
        base["image"] = f"images/{image_name}"
        return base

    @staticmethod
    def _parse_status(value: bytes) -> list[dict[str, object]]:
        tokens = value.split(b"\0")
        entries: list[dict[str, object]] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if not token:
                continue
            if len(token) < 4 or token[2:3] != b" ":
                entries.append(
                    {
                        "path": os.fsdecode(token),
                        "xy": "??",
                        "unsupported": "malformed git status entry",
                    }
                )
                continue
            xy = token[:2].decode("ascii", "replace")
            path = os.fsdecode(token[3:])
            unsupported = None
            if "R" in xy or "C" in xy:
                unsupported = "rename or copy status"
            entries.append({"path": path, "xy": xy, "unsupported": unsupported})
            if "R" in xy or "C" in xy:
                if index < len(tokens) and tokens[index]:
                    old_path = os.fsdecode(tokens[index])
                    index += 1
                    entries.append(
                        {
                            "path": old_path,
                            "xy": xy,
                            "unsupported": "rename or copy status",
                        }
                    )
        return entries

    def _is_excluded(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        return any(
            normalized == excluded or normalized.startswith(excluded.rstrip("/") + "/")
            for excluded in self.excluded_paths
        )


def _snapshot_identity(record: Mapping[str, object]) -> tuple[object, ...]:
    return (
        record.get("present"),
        record.get("kind"),
        record.get("mode"),
        record.get("size"),
        record.get("content_sha256"),
        record.get("link_target"),
    )


def changed_paths_between(
    before: GitSnapshot,
    after: GitSnapshot,
) -> list[str]:
    paths = set(before.paths) | set(after.paths)
    return sorted(
        path
        for path in paths
        if _snapshot_identity(before.record(path)) != _snapshot_identity(after.record(path))
    )


def _read_snapshot_image(snapshot: GitSnapshot, path: str) -> bytes | None:
    record = snapshot.record(path)
    image = record.get("image")
    if not isinstance(image, str):
        return None
    image_path = snapshot.directory / image
    if not image_path.is_file():
        return None
    return image_path.read_bytes()


def write_iteration_diff(
    before: GitSnapshot | None,
    after: GitSnapshot | None,
    path: Path,
) -> list[str]:
    if before is None or after is None:
        _atomic_write_bytes(path, b"# diff unavailable: a Git boundary could not be captured\n")
        return []
    changed = changed_paths_between(before, after)
    output: list[str] = []
    for relative_path in changed:
        before_record = before.record(relative_path)
        after_record = after.record(relative_path)
        before_image = _read_snapshot_image(before, relative_path)
        after_image = _read_snapshot_image(after, relative_path)
        before_present = bool(before_record.get("present"))
        after_present = bool(after_record.get("present"))
        if (
            before_record.get("unsupported")
            or after_record.get("unsupported")
            or (before_present and before_image is None)
            or (after_present and after_image is None)
        ):
            reason = (
                after_record.get("unsupported")
                or before_record.get("unsupported")
                or "image unavailable"
            )
            output.append(
                "# unsupported path: "
                f"{json.dumps(relative_path, ensure_ascii=True)} ({reason})\n"
            )
            continue
        before_text = (before_image if before_image is not None else b"").decode(
            "utf-8"
        ).splitlines(keepends=True)
        after_text = (after_image if after_image is not None else b"").decode(
            "utf-8"
        ).splitlines(keepends=True)
        output.extend(
            line if line.endswith("\n") else line + "\n"
            for line in difflib.unified_diff(
                before_text,
                after_text,
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
                lineterm="",
            )
        )
    _atomic_write_bytes(path, "".join(output).encode("utf-8"))
    return changed


class CheckRunner:
    """Execute argv checks with bounded output and timeout handling."""

    def __init__(self, workdir: Path, iteration_root: Path) -> None:
        self.workdir = workdir.resolve()
        self.iteration_root = iteration_root
        self.checks_dir = iteration_root / "checks"

    def run(self, checks: Sequence[CheckSpec]) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for index, check in enumerate(checks, start=1):
            results.append(self._run_one(index, check))
        return results

    def _run_one(self, index: int, check: CheckSpec) -> dict[str, object]:
        prefix = f"{index:04d}"
        stdout_ref = f"checks/{prefix}.stdout"
        stderr_ref = f"checks/{prefix}.stderr"
        stdout_path = self.iteration_root / stdout_ref
        stderr_path = self.iteration_root / stderr_ref
        if self.checks_dir.is_symlink():
            raise ValueError(f"Checks directory must not be a symlink: {self.checks_dir}")
        self.checks_dir.mkdir(parents=True, exist_ok=True)
        if self.checks_dir.is_symlink():
            raise ValueError(f"Checks directory must not be a symlink: {self.checks_dir}")
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        # PATH is required for ordinary argv commands such as ``python`` but
        # is safe to inherit as command lookup metadata.  All other variables
        # remain opt-in and are copied only from the declared allowlist.
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            **{
                name: os.environ[name]
                for name in check.env_allowlist
                if name in os.environ and name != "PATH"
            },
        }
        cwd_path: Path | None = None
        process_error: str | None = None
        returncode: int | None = None
        timed_out = False
        stdout_bytes = b""
        stderr_bytes = b""
        stdout_total = 0
        stderr_total = 0
        stdout_truncated = False
        stderr_truncated = False

        try:
            cwd_path = (
                self.workdir
                if check.cwd == "."
                else _safe_repo_path(self.workdir, check.cwd, "check cwd")
            )
            if not cwd_path.is_dir():
                raise ValueError("check cwd must be an existing directory")
            process = subprocess.Popen(
                list(check.argv),
                cwd=str(cwd_path),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
        except (OSError, ValueError) as exc:
            process_error = f"{type(exc).__name__}: {exc}"
        else:
            (
                stdout_bytes,
                stderr_bytes,
                stdout_total,
                stderr_total,
                stdout_truncated,
                stderr_truncated,
                timed_out,
            ) = self._communicate_bounded(process, check.timeout_seconds, check.output_limit_bytes)
            returncode = process.returncode

        _atomic_write_bytes(stdout_path, stdout_bytes)
        _atomic_write_bytes(stderr_path, stderr_bytes)
        ended_at = _utc_now()
        if process_error:
            status = "error"
        elif timed_out:
            status = "timeout"
        elif returncode == 0:
            status = "passed"
        else:
            status = "failed"
        result: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "check_id": prefix,
            "argv": list(check.argv),
            "cwd": check.cwd,
            "timeout_seconds": check.timeout_seconds,
            "env_allowlist": list(check.env_allowlist),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": round(time.monotonic() - started_monotonic, 6),
            "status": status,
            "exit_code": returncode,
            "signal": -returncode if returncode is not None and returncode < 0 else None,
            "timed_out": timed_out,
            "output_limit_bytes": check.output_limit_bytes,
            "stdout_ref": stdout_ref,
            "stderr_ref": stderr_ref,
            "stdout_bytes": stdout_total,
            "stderr_bytes": stderr_total,
            "stdout_stored_bytes": len(stdout_bytes),
            "stderr_stored_bytes": len(stderr_bytes),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "redacted": False,
            "process_error": process_error,
        }
        _atomic_write_json(self.checks_dir / f"{prefix}.json", result)
        return result

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        else:
            process.terminate()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()

    @classmethod
    def _communicate_bounded(
        cls,
        process: subprocess.Popen[bytes],
        timeout_seconds: int,
        output_limit: int,
    ) -> tuple[bytes, bytes, int, int, bool, bool, bool]:
        if process.stdout is None or process.stderr is None:
            return b"", b"", 0, 0, False, False, False
        selector = selectors.DefaultSelector()
        streams = {process.stdout: "stdout", process.stderr: "stderr"}
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ, streams[stream])
        stored = {"stdout": bytearray(), "stderr": bytearray()}
        totals = {"stdout": 0, "stderr": 0}
        truncated = {"stdout": False, "stderr": False}
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        drain_deadline: float | None = None
        try:
            while process.poll() is None or selector.get_map():
                now = time.monotonic()
                # A child process can outlive its parent while keeping the
                # parent's stdout/stderr pipes open.  The deadline therefore
                # applies to both process completion and pipe draining.
                if not timed_out and now >= deadline:
                    cls._terminate_process(process)
                    timed_out = True
                    drain_deadline = now + 1.0
                if timed_out and drain_deadline is not None and now >= drain_deadline:
                    for key in list(selector.get_map().values()):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    break
                if timed_out:
                    wait_time = max(0.0, min(0.05, (drain_deadline or now) - now))
                elif not selector.get_map():
                    # Once a check closes both output descriptors it may
                    # still be running.  Wait for process completion instead
                    # of treating closed pipes as completion of the check.
                    wait_time = max(0.0, min(0.05, deadline - now))
                    try:
                        process.wait(timeout=wait_time)
                    except subprocess.TimeoutExpired:
                        pass
                    continue
                else:
                    wait_time = max(0.0, min(0.05, deadline - now))
                events = selector.select(wait_time)
                if not events:
                    continue
                for key, _ in events:
                    stream = key.fileobj
                    label = key.data
                    try:
                        chunk = os.read(stream.fileno(), 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        try:
                            selector.unregister(stream)
                        except Exception:
                            pass
                        stream.close()
                        continue
                    totals[label] += len(chunk)
                    remaining = output_limit - len(stored[label])
                    if remaining > 0:
                        stored[label].extend(chunk[:remaining])
                    if len(chunk) > max(remaining, 0):
                        truncated[label] = True
        finally:
            selector.close()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            cls._terminate_process(process)
            process.wait(timeout=0.5)
        return (
            bytes(stored["stdout"]),
            bytes(stored["stderr"]),
            totals["stdout"],
            totals["stderr"],
            truncated["stdout"],
            truncated["stderr"],
            timed_out,
        )


class IterationClassifier:
    """Turn captured boundaries and execution results into a fail-closed outcome."""

    @staticmethod
    def classify(
        *,
        iteration_id: str,
        work_item_id: str,
        request: SingleChangeRequest,
        before: GitSnapshot | None,
        after_change: GitSnapshot | None,
        after_checks: GitSnapshot | None,
        check_results: Sequence[Mapping[str, object]],
        checks_attempted: bool,
        executor_error: str | None,
        capture_errors: Sequence[str],
        required_artifacts_complete: bool,
    ) -> dict[str, object]:
        final_snapshot = after_checks or after_change
        executor_changed: list[str] = []
        check_changed: list[str] = []
        changed: list[str] = []
        if before is not None and after_change is not None:
            executor_changed = changed_paths_between(before, after_change)
        if after_change is not None and after_checks is not None:
            check_changed = changed_paths_between(after_change, after_checks)
        if before is not None and final_snapshot is not None:
            changed = changed_paths_between(before, final_snapshot)
        all_changed = sorted(set(executor_changed) | set(check_changed))
        allowed = set(request.allowed_paths)
        unplanned = sorted(set(all_changed) - allowed)

        reason_codes: list[str] = []

        def add_reason(code: str) -> None:
            if code not in reason_codes:
                reason_codes.append(code)

        if executor_error:
            add_reason("executor_failed")
        if capture_errors:
            add_reason("capture_failed")
        if unplanned:
            add_reason("unplanned_path")

        unsupported_states: list[dict[str, str]] = []
        for path in sorted(set(all_changed) | allowed):
            for snapshot in (before, after_change, after_checks):
                if snapshot is None:
                    continue
                unsupported = snapshot.unsupported_for(path)
                if unsupported:
                    item = {"path": path, "reason": unsupported, "boundary": snapshot.boundary}
                    if item not in unsupported_states:
                        unsupported_states.append(item)
        for snapshot in (before, after_change, after_checks):
            if snapshot is None:
                continue
            index_error = snapshot.repository.get("index_error")
            if index_error:
                item = {
                    "path": "<index>",
                    "reason": str(index_error),
                    "boundary": snapshot.boundary,
                }
                if item not in unsupported_states:
                    unsupported_states.append(item)
        if unsupported_states:
            add_reason("unsupported_state")

        if before is not None:
            for snapshot in (after_change, after_checks):
                if snapshot is None:
                    continue
                if (
                    snapshot.repository.get("head") != before.repository.get("head")
                    or snapshot.repository.get("index_tree")
                    != before.repository.get("index_tree")
                    or snapshot.repository.get("worktree")
                    != before.repository.get("worktree")
                    or snapshot.repository.get("git_dir")
                    != before.repository.get("git_dir")
                ):
                    add_reason("git_identity_drift")
        for result in check_results:
            status = str(result.get("status", "error"))
            if status != "passed":
                add_reason("check_timeout" if status == "timeout" else "check_failed")
        if not required_artifacts_complete:
            add_reason("artifact_incomplete")

        status = "failed" if reason_codes else "succeeded"
        return {
            "schema_version": SCHEMA_VERSION,
            "iteration_id": iteration_id,
            "work_item_id": work_item_id,
            "target": request.active_targets[0].to_payload(),
            "change_intent": request.change_intent,
            "allowed_paths": list(request.allowed_paths),
            "status": status,
            "reason_codes": reason_codes,
            "changed_paths": changed,
            "executor_changed_paths": executor_changed,
            "check_changed_paths": check_changed,
            "unplanned_paths": unplanned,
            "unsupported_states": unsupported_states,
            "check_results": [dict(result) for result in check_results],
            "checks_attempted": checks_attempted,
            "checks_skipped": bool(request.checks) and not checks_attempted,
            "capture_errors": list(capture_errors),
            "executor_error": executor_error,
            "required_artifacts_complete": required_artifacts_complete,
            "artifact_digests": {},
            "plan_deviations": {
                "unplanned_paths": unplanned,
                "allowed_path_boundary": list(request.allowed_paths),
            },
            "potential_hitchhiking": (
                "Semantic relatedness within an allowed path cannot be fully "
                "determined by this iteration; review the recorded diff."
            ),
            "unhandled_points": [
                "This provenance does not guarantee complete rollback.",
                "This classifier does not prove that the change is one semantic change.",
            ],
        }


def _summary_json(value: object) -> str:
    """Render JSON values without allowing data to create Markdown code spans."""

    return json.dumps(value, ensure_ascii=True).replace("`", r"\`")


def render_summary(outcome: Mapping[str, object]) -> str:
    unplanned = outcome.get("unplanned_paths") or []
    unsupported = outcome.get("unsupported_states") or []
    checks = outcome.get("check_results") or []
    reason_codes = outcome.get("reason_codes") or []
    lines = [
        "# Single-change iteration summary",
        "",
        f"- Status: `{outcome.get('status')}`",
        f"- Work item: `{outcome.get('work_item_id')}`",
        f"- Iteration: `{outcome.get('iteration_id')}`",
        f"- Target: {_summary_json(outcome.get('target'))}",
        f"- Change intent: {_summary_json(outcome.get('change_intent'))}",
        f"- Allowed paths: {_summary_json(outcome.get('allowed_paths', []))}",
        f"- Reason codes: `{', '.join(str(item) for item in reason_codes) or 'none'}`",
        "",
        "## Changes",
        "",
        f"- Changed paths: {_summary_json(outcome.get('changed_paths', []))}",
        f"- Executor boundary changes: {_summary_json(outcome.get('executor_changed_paths', []))}",
        f"- Check boundary changes: {_summary_json(outcome.get('check_changed_paths', []))}",
        "",
        "## Plan deviations",
        "",
        f"- Unplanned paths: {_summary_json(unplanned)}",
        "- Potential hitchhiking changes: semantic relatedness inside an allowed path requires review of `diff.patch`.",
        "",
        "## Checks",
        "",
    ]
    if checks:
        for result in checks:
            lines.append(
                f"- `{result.get('check_id')}`: `{result.get('status')}`, "
                f"exit={result.get('exit_code')}, stdout=`{result.get('stdout_ref')}`, "
                f"stderr=`{result.get('stderr_ref')}`"
            )
    else:
        lines.append("- No checks were executed.")
    lines.extend(
        [
            "",
            "## Unsupported or unhandled points",
            "",
            f"- Unsupported states: {_summary_json(unsupported)}",
            "- This artifact records provenance; it does not guarantee complete rollback.",
            "- Meaningful one-change boundaries cannot be proven mechanically from paths alone.",
            "",
            f"- Required artifacts complete: `{outcome.get('required_artifacts_complete')}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _artifact_digests(scope_path: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(scope_path.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(scope_path).as_posix()
        if relative in {"lifecycle.json", "outcome.json", "summary.md"}:
            continue
        digests[relative] = _sha256_file(path)
    return digests


class SingleChangeOrchestrator:
    """Run one validated request through one executor invocation."""

    def __init__(
        self,
        *,
        workdir: Path,
        artifact_root: Path,
        executor: Callable[[SingleChangeRequest, IterationScope], object],
        excluded_paths: Iterable[str] = (),
    ) -> None:
        self.workdir = workdir.resolve()
        if artifact_root.is_symlink():
            raise ValueError(f"Artifact root must not be a symlink: {artifact_root}")
        self.artifact_root = artifact_root.resolve(strict=False)
        self.executor = executor
        exclusions = set(excluded_paths) | {".kelpie"}
        try:
            artifact_relative = self.artifact_root.relative_to(self.workdir).as_posix()
        except ValueError:
            artifact_relative = None
        if artifact_relative and artifact_relative != ".":
            exclusions.add(artifact_relative)
        self.excluded_paths = tuple(
            sorted(
                _safe_relative_path(str(path), "excluded path", allow_dot=True)
                for path in exclusions
            )
        )

    def run(self, request: SingleChangeRequest) -> IterationResult:
        validated = SingleChangeRequestValidator(self.workdir).validate(request)
        try:
            artifact_relative = self.artifact_root.relative_to(self.workdir).as_posix()
        except ValueError:
            artifact_relative = None
        if artifact_relative and artifact_relative != ".":
            if any(
                path == artifact_relative or path.startswith(artifact_relative + "/")
                for path in validated.allowed_paths
            ):
                raise ValueError("allowed path must not target the artifact root")
        excluded = set(self.excluded_paths)
        if any(
            any(
                path == excluded_path
                or path.startswith(excluded_path.rstrip("/") + "/")
                for excluded_path in excluded
            )
            for path in validated.allowed_paths
        ):
            raise ValueError("allowed path overlaps an excluded provenance path")
        store = IterationStore(self.artifact_root, validated.work_item_id)
        scope = store.reserve()
        intent_payload = {
            "schema_version": SCHEMA_VERSION,
            "iteration_id": scope.iteration_id,
            "work_item_id": validated.work_item_id,
            "target": validated.active_targets[0].to_payload(),
            "change_intent": validated.change_intent,
            "allowed_paths": list(validated.allowed_paths),
            "checks": [check.to_payload() for check in validated.checks],
            "created_at": _utc_now(),
        }
        store.write_intent(scope, intent_payload)

        capture = GitStateCapture(self.workdir, excluded_paths=self.excluded_paths)
        before: GitSnapshot | None = None
        after_change: GitSnapshot | None = None
        after_checks: GitSnapshot | None = None
        check_results: list[dict[str, object]] = []
        checks_attempted = False
        capture_errors: list[str] = []
        executor_error: str | None = None
        known_paths: set[str] = set(validated.allowed_paths)

        try:
            before = capture.capture(scope.path / "git-before", known_paths=known_paths)
            known_paths.update(before.paths)
            store.transition(scope, "in_progress")
            initial_unsupported = [
                {"path": path, "reason": before.unsupported_for(path), "boundary": "git-before"}
                for path in validated.allowed_paths
                if before.unsupported_for(path)
            ]
            if initial_unsupported:
                # Do not invoke the executor when an explicitly declared path
                # is already outside the MVP capture policy.
                after_change = capture.capture(
                    scope.path / "git-after-change",
                    known_paths=known_paths,
                )
                known_paths.update(after_change.paths)
                after_checks = capture.capture(
                    scope.path / "git-after-checks",
                    known_paths=known_paths,
                )
                store.transition(scope, "source_changed")
                store.transition(scope, "checked")
            else:
                try:
                    self.executor(validated, scope)
                except BaseException as exc:
                    executor_error = f"{type(exc).__name__}: {exc}"
                try:
                    after_change = capture.capture(
                        scope.path / "git-after-change",
                        known_paths=known_paths,
                    )
                    known_paths.update(after_change.paths)
                    store.transition(scope, "source_changed")
                except BaseException as exc:
                    capture_errors.append(f"after-change capture: {type(exc).__name__}: {exc}")
                if after_change is not None and executor_error is None:
                    checks_attempted = True
                    try:
                        check_results = CheckRunner(self.workdir, scope.path).run(validated.checks)
                    except BaseException as exc:
                        capture_errors.append(f"check execution: {type(exc).__name__}: {exc}")
                try:
                    after_checks = capture.capture(
                        scope.path / "git-after-checks",
                        known_paths=known_paths,
                    )
                    known_paths.update(after_checks.paths)
                    if store.read_lifecycle(scope).get("state") == "source_changed":
                        store.transition(scope, "checked")
                except BaseException as exc:
                    capture_errors.append(f"after-checks capture: {type(exc).__name__}: {exc}")
        except BaseException as exc:
            capture_errors.append(f"orchestration: {type(exc).__name__}: {exc}")
            # A source mutation may have happened before a capture failure.
            # Best-effort snapshots preserve whatever can still be observed.
            if before is not None and after_change is None:
                try:
                    after_change = capture.capture(
                        scope.path / "git-after-change",
                        known_paths=known_paths,
                    )
                    known_paths.update(after_change.paths)
                except BaseException as nested:
                    capture_errors.append(
                        f"recovery after-change capture: {type(nested).__name__}: {nested}"
                    )
            if after_change is not None and after_checks is None:
                try:
                    after_checks = capture.capture(
                        scope.path / "git-after-checks",
                        known_paths=known_paths,
                    )
                    known_paths.update(after_checks.paths)
                except BaseException as nested:
                    capture_errors.append(
                        f"recovery after-checks capture: {type(nested).__name__}: {nested}"
                    )

        # A failed check capture still gets a diff against the last known
        # source boundary.  Keep ``after_checks`` as None so state.json and
        # required-artifact checks accurately expose the missing boundary.
        final_snapshot = after_checks or after_change
        diff_path = scope.path / "diff.patch"
        changed_paths = write_iteration_diff(before, final_snapshot, diff_path)
        state_payload = {
            "schema_version": SCHEMA_VERSION,
            "iteration_id": scope.iteration_id,
            "before": "git-before" if before is not None else None,
            "after_change": "git-after-change" if after_change is not None else None,
            "after_checks": "git-after-checks" if after_checks is not None else None,
            "diff": "diff.patch",
            "changed_paths": changed_paths,
            "checks_attempted": checks_attempted,
            "excluded_paths": list(self.excluded_paths),
            "capture_errors": capture_errors,
        }
        _atomic_write_json(scope.path / "state.json", state_payload)
        required_artifacts_complete = self._required_artifacts_complete(
            scope.path,
            validated,
            before,
            after_change,
            after_checks,
            checks_attempted,
        )
        outcome = IterationClassifier.classify(
            iteration_id=scope.iteration_id,
            work_item_id=validated.work_item_id,
            request=validated,
            before=before,
            after_change=after_change,
            after_checks=after_checks,
            check_results=check_results,
            checks_attempted=checks_attempted,
            executor_error=executor_error,
            capture_errors=capture_errors,
            required_artifacts_complete=required_artifacts_complete,
        )
        outcome["artifact_digests"] = _artifact_digests(scope.path)
        if before is not None:
            initial_unsupported = [
                {
                    "path": path,
                    "reason": before.unsupported_for(path),
                    "boundary": "git-before",
                }
                for path in validated.allowed_paths
                if before.unsupported_for(path)
            ]
            if initial_unsupported:
                outcome["unsupported_states"] = [
                    *outcome.get("unsupported_states", []),
                    *[item for item in initial_unsupported if item not in outcome.get("unsupported_states", [])],
                ]
                if "unsupported_state" not in outcome["reason_codes"]:
                    outcome["reason_codes"].append("unsupported_state")
                outcome["status"] = "failed"
        _atomic_write_json(scope.path / "outcome.json", outcome)
        _atomic_write_bytes(scope.path / "summary.md", render_summary(outcome).encode("utf-8"))

        lifecycle = store.read_lifecycle(scope)
        if lifecycle.get("state") != "terminal":
            store.transition(
                scope,
                "terminal",
                terminal_status=str(outcome["status"]),
                reason_codes=[str(code) for code in outcome.get("reason_codes", [])],
            )
        return IterationResult(
            iteration_id=scope.iteration_id,
            iteration_dir=scope.path,
            status=str(outcome["status"]),
            outcome=outcome,
            error=executor_error or (capture_errors[0] if capture_errors else None),
        )

    @staticmethod
    def _required_artifacts_complete(
        scope_path: Path,
        request: SingleChangeRequest,
        before: GitSnapshot | None,
        after_change: GitSnapshot | None,
        after_checks: GitSnapshot | None,
        checks_attempted: bool,
    ) -> bool:
        required = [
            "intent.json",
            "lifecycle.json",
            "state.json",
            "diff.patch",
            "git-before/repository.json",
            "git-before/status.z",
            "git-before/paths.json",
            "git-after-change/repository.json",
            "git-after-change/status.z",
            "git-after-change/paths.json",
            "git-after-checks/repository.json",
            "git-after-checks/status.z",
            "git-after-checks/paths.json",
        ]
        if checks_attempted:
            for index in range(1, len(request.checks) + 1):
                required.extend(
                    [
                        f"checks/{index:04d}.json",
                        f"checks/{index:04d}.stdout",
                        f"checks/{index:04d}.stderr",
                    ]
                )
        if not all((scope_path / relative).is_file() for relative in required):
            return False
        for snapshot in (before, after_change, after_checks):
            if snapshot is None:
                continue
            for record in snapshot.paths.values():
                if (
                    record.get("present")
                    and record.get("kind") == "regular"
                    and not record.get("unsupported")
                ):
                    image = record.get("image")
                    if not isinstance(image, str) or not (snapshot.directory / image).is_file():
                        return False
        return True


def run_single_change(
    request: SingleChangeRequest,
    *,
    workdir: Path,
    artifact_root: Path,
    executor: Callable[[SingleChangeRequest, IterationScope], object],
    excluded_paths: Iterable[str] = (),
) -> IterationResult:
    """Convenience entry point for fixture and non-WorkflowRunner callers."""

    return SingleChangeOrchestrator(
        workdir=workdir,
        artifact_root=artifact_root,
        executor=executor,
        excluded_paths=excluded_paths,
    ).run(request)
