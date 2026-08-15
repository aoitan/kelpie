#!/usr/bin/env python3
"""One-shot Implement -> Verify -> Review evaluation loop.

The single-change runner owns mutation and targeted-check execution.  This
module deliberately consumes that runner's immutable artifacts instead of
running checks a second time.  Review output is treated as an untrusted wire
contract; the final verdict, finding identity, finding lifecycle, and summary
are owned by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

try:
    from scripts.single_change import (
        ActiveTarget,
        IterationResult,
        IterationScope,
        SingleChangeRequest,
        SingleChangeRequestValidator,
        run_single_change,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from single_change import (  # type: ignore
        ActiveTarget,
        IterationResult,
        IterationScope,
        SingleChangeRequest,
        SingleChangeRequestValidator,
        run_single_change,
    )


SCHEMA_VERSION = "1.0"
IDENTITY_VERSION = "1.0"
LOOP_ID_PATTERN = re.compile(r"^[0-9]{4}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FINDING_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

VERDICTS = frozenset(
    {
        "satisfied",
        "changes_requested",
        "execution_failed",
        "invalid_output",
        "plan_defect",
    }
)
EVIDENCE_KINDS = frozenset({"requirement", "plan", "diff", "source", "check"})
SEVERITIES = frozenset({"critical", "high", "medium", "low"})
CATEGORIES = frozenset({"implementation_defect", "verification_gap", "plan_defect"})
REVIEW_EXECUTION_STATES = frozenset({"succeeded", "failed", "timed_out"})
REVIEW_VALIDATION_STATES = frozenset(
    {
        "not_started_due_to_dependency",
        "execution_failed",
        "empty",
        "truncated",
        "parse_invalid",
        "schema_invalid",
        "evidence_invalid",
        "duplicate_or_collision",
        "valid",
    }
)

MAX_REVIEW_TIMEOUT_SECONDS = 900
DEFAULT_REVIEW_TIMEOUT_SECONDS = 300
MAX_REVIEW_OUTPUT_BYTES = 262_144
DEFAULT_REVIEW_OUTPUT_BYTES = MAX_REVIEW_OUTPUT_BYTES
MAX_FINDINGS = 100
MAX_EVIDENCE_PER_FINDING = 20
MAX_MESSAGE_LENGTH = 8_192
MAX_FINDING_KEY_LENGTH = 128
MAX_JSON_DEPTH = 32
MAX_LOCATOR_LENGTH = 4_096


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    return sha256_bytes(_canonical_bytes(value))


def _pretty_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


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
    if path.is_symlink():
        raise ValueError(f"artifact destination must not be a symlink: {path}")
    temporary_path: Path | None = None
    try:
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
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _exclusive_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"artifact destination must not be a symlink: {path}")
    descriptor: int | None = None
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
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


def _safe_relative_path(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty relative path")
    if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
        raise ValueError(f"invalid {field_name}: {value}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"invalid {field_name}: {value}")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"invalid {field_name}: {value}")
    return "/".join(parts)


def _assert_safe_path(root: Path, path: Path) -> None:
    if root.is_symlink():
        raise ValueError(f"artifact root must not be a symlink: {root}")
    root_canonical = root.resolve(strict=False)
    try:
        path.resolve(strict=False).relative_to(root_canonical)
    except ValueError as exc:
        raise ValueError(f"artifact path escapes root: {path}") from exc
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"artifact path is not below root: {path}") from exc
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"artifact path contains a symlink: {path}")


def _read_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required JSON artifact is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must contain an object: {path}")
    return value


def _max_depth(value: object, depth: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        return depth
    if isinstance(value, Mapping):
        return max([depth, *(_max_depth(item, depth + 1) for item in value.values())])
    if isinstance(value, list):
        return max([depth, *(_max_depth(item, depth + 1) for item in value)])
    return depth


def _clean_summary_text(value: object) -> str:
    text = str(value)
    return "".join(character for character in text if ord(character) >= 0x20 or character in "\n\t")


def _json_summary_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True).replace("`", r"\`")


@dataclass(frozen=True)
class EvidenceRef:
    """A digest-bound, typed artifact reference accepted by Review."""

    kind: str
    artifact_ref: str
    locator: str = "artifact"
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in EVIDENCE_KINDS:
            raise ValueError(f"unsupported evidence kind: {self.kind}")
        _safe_relative_path(self.artifact_ref, "evidence artifact_ref")
        if not isinstance(self.locator, str) or not self.locator.strip():
            raise ValueError("evidence locator must be a non-empty string")
        if len(self.locator) > MAX_LOCATOR_LENGTH:
            raise ValueError("evidence locator is too long")
        if any(ord(character) < 0x20 and character not in "\t\n" for character in self.locator):
            raise ValueError("evidence locator contains control characters")
        if self.sha256 is not None and (
            not isinstance(self.sha256, str) or not SHA256_PATTERN.fullmatch(self.sha256)
        ):
            raise ValueError("evidence sha256 must be 64 lowercase hexadecimal characters")

    @classmethod
    def from_value(cls, value: object, *, default_kind: str = "requirement") -> "EvidenceRef":
        if isinstance(value, EvidenceRef):
            return value
        if isinstance(value, str):
            return cls(default_kind, value)
        if not isinstance(value, Mapping):
            raise ValueError("evidence reference must be an object or path string")
        allowed = {"kind", "artifact_ref", "locator", "sha256"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown evidence fields: {sorted(unknown)}")
        return cls(
            kind=value.get("kind", default_kind),
            artifact_ref=value.get("artifact_ref", ""),
            locator=value.get("locator", "artifact"),
            sha256=value.get("sha256"),
        )

    def to_payload(self) -> dict[str, str]:
        payload = {
            "kind": self.kind,
            "artifact_ref": self.artifact_ref,
            "locator": self.locator,
        }
        if self.sha256 is not None:
            payload["sha256"] = self.sha256
        return payload


@dataclass(frozen=True)
class ReviewProcessResult:
    """Result returned by an injected reviewer adapter."""

    raw_output: bytes | str | Mapping[str, object] | Sequence[object] | None = None
    state: str = "succeeded"
    exit_code: int | None = 0
    timed_out: bool = False
    process_error: str | None = None
    capture_failed: bool = False
    stderr: bytes | str | None = None

    @classmethod
    def success(
        cls,
        raw_output: bytes | str | Mapping[str, object] | Sequence[object] | None,
    ) -> "ReviewProcessResult":
        return cls(raw_output=raw_output)

    @classmethod
    def failure(
        cls,
        reason: str,
        *,
        timed_out: bool = False,
        exit_code: int | None = None,
        raw_output: bytes | str | None = None,
    ) -> "ReviewProcessResult":
        return cls(
            raw_output=raw_output,
            state="timed_out" if timed_out else "failed",
            exit_code=exit_code,
            timed_out=timed_out,
            process_error=reason,
        )


class Reviewer(Protocol):
    def review(self, request: "ReviewInvocation") -> ReviewProcessResult | bytes | str | Mapping[str, object]:
        ...


@dataclass(frozen=True)
class ReviewInvocation:
    loop_id: str
    work_item_id: str
    target: ActiveTarget
    target_sha256: str
    manifest_sha256: str
    input_path: Path
    evidence: tuple[EvidenceRef, ...]
    input_manifest: Mapping[str, object]

    @property
    def input(self) -> Mapping[str, object]:
        return self.input_manifest

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "loop_id": self.loop_id,
            "work_item_id": self.work_item_id,
            "target": self.target.to_payload(),
            "target_sha256": self.target_sha256,
            "manifest_sha256": self.manifest_sha256,
            "input_path": str(self.input_path),
            "evidence": [item.to_payload() for item in self.evidence],
        }


@dataclass(frozen=True)
class EvaluationLoopRequest:
    work_item_id: str
    single_change: SingleChangeRequest
    requirement_refs: tuple[EvidenceRef, ...] | Sequence[EvidenceRef | Mapping[str, object] | str]
    reviewer: Reviewer | Callable[[ReviewInvocation], object] | None
    review_timeout_seconds: int = DEFAULT_REVIEW_TIMEOUT_SECONDS
    review_output_limit_bytes: int = DEFAULT_REVIEW_OUTPUT_BYTES

    def __post_init__(self) -> None:
        refs = tuple(
            EvidenceRef.from_value(item)
            for item in self.requirement_refs
        )
        object.__setattr__(self, "requirement_refs", refs)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "EvaluationLoopRequest":
        if not isinstance(raw, Mapping):
            raise ValueError("evaluation loop request must be an object")
        single_change = raw.get("single_change")
        if isinstance(single_change, SingleChangeRequest):
            parsed = single_change
        elif isinstance(single_change, Mapping):
            parsed = SingleChangeRequest.from_mapping(single_change)
        else:
            raise ValueError("single_change must be a SingleChangeRequest or object")
        raw_refs = raw.get("requirement_refs", raw.get("requirements", ()))
        if not isinstance(raw_refs, (tuple, list)):
            raise ValueError("requirement_refs must be a sequence")
        return cls(
            work_item_id=raw.get("work_item_id", parsed.work_item_id),
            single_change=parsed,
            requirement_refs=tuple(EvidenceRef.from_value(item) for item in raw_refs),
            reviewer=raw.get("reviewer"),
            review_timeout_seconds=raw.get("review_timeout_seconds", DEFAULT_REVIEW_TIMEOUT_SECONDS),
            review_output_limit_bytes=raw.get("review_output_limit_bytes", DEFAULT_REVIEW_OUTPUT_BYTES),
        )


@dataclass(frozen=True)
class EvaluationLoopResult:
    loop_id: str
    loop_dir: Path
    verdict: str
    result: dict[str, object]
    error: str | None = None

    @property
    def status(self) -> str:
        return self.verdict


class EvaluationLoopRequestValidator:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir.resolve()

    def validate(self, request: EvaluationLoopRequest) -> EvaluationLoopRequest:
        if not isinstance(request, EvaluationLoopRequest):
            raise TypeError("request must be an EvaluationLoopRequest")
        if not isinstance(request.work_item_id, str) or not ID_PATTERN.fullmatch(request.work_item_id):
            raise ValueError("work_item_id has an unsafe namespace")
        single_change = SingleChangeRequestValidator(self.workdir).validate(request.single_change)
        if single_change.work_item_id != request.work_item_id:
            raise ValueError("work_item_id does not match single_change")
        if not single_change.checks:
            raise ValueError("evaluation loop requires at least one targeted check")
        if request.reviewer is None:
            raise ValueError("evaluation loop requires an injected reviewer")
        if not isinstance(request.requirement_refs, (tuple, list)) or not request.requirement_refs:
            raise ValueError("evaluation loop requires at least one requirement reference")
        if not any(item.kind == "requirement" for item in request.requirement_refs):
            raise ValueError("evaluation loop requires at least one requirement evidence reference")
        timeout = request.review_timeout_seconds
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_REVIEW_TIMEOUT_SECONDS:
            raise ValueError(
                f"review_timeout_seconds must be between 1 and {MAX_REVIEW_TIMEOUT_SECONDS}"
            )
        limit = request.review_output_limit_bytes
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_REVIEW_OUTPUT_BYTES:
            raise ValueError(
                f"review_output_limit_bytes must be between 1 and {MAX_REVIEW_OUTPUT_BYTES}"
            )
        return EvaluationLoopRequest(
            work_item_id=request.work_item_id,
            single_change=single_change,
            requirement_refs=tuple(request.requirement_refs),
            reviewer=request.reviewer,
            review_timeout_seconds=timeout,
            review_output_limit_bytes=limit,
        )


class EvaluationLoopStore:
    """Reserve and persist immutable one-shot evaluation loop scopes."""

    def __init__(self, artifact_root: Path, work_item_id: str) -> None:
        if not isinstance(work_item_id, str) or not ID_PATTERN.fullmatch(work_item_id):
            raise ValueError("work_item_id has an unsafe artifact namespace")
        if artifact_root.is_symlink():
            raise ValueError(f"artifact root must not be a symlink: {artifact_root}")
        self.artifact_root = artifact_root.resolve(strict=False)
        self.work_item_id = work_item_id
        self.work_item_root = self.artifact_root / "work-items" / work_item_id
        self.loops_root = self.work_item_root / "evaluation-loops"

    @contextmanager
    def _lock(self) -> Iterable[None]:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        if self.work_item_root.is_symlink() or self.loops_root.is_symlink():
            raise ValueError("evaluation loop namespace must not contain symlinks")
        self.loops_root.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(self.artifact_root, self.loops_root)
        lock_path = self.work_item_root / ".evaluation-loop-lock"
        if lock_path.is_symlink():
            raise ValueError("evaluation loop lock must not be a symlink")
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, flags, 0o600)
        except FileExistsError as exc:
            raise RuntimeError(f"evaluation loop work item is already locked: {self.work_item_id}") from exc
        try:
            os.write(descriptor, f"work_item={self.work_item_id}\npid={os.getpid()}\n".encode())
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            lock_path.unlink(missing_ok=True)

    def _scope_path(self, loop_id: str) -> Path:
        if not LOOP_ID_PATTERN.fullmatch(loop_id):
            raise ValueError("invalid evaluation loop id")
        path = self.loops_root / loop_id
        _assert_safe_path(self.artifact_root, path)
        return path

    def reserve(self) -> tuple[str, Path]:
        with self._lock():
            for number in range(1, 10000):
                loop_id = f"{number:04d}"
                path = self.loops_root / loop_id
                if path.is_symlink():
                    raise ValueError(f"evaluation loop scope is a symlink: {path}")
                try:
                    path.mkdir(exist_ok=False)
                except FileExistsError:
                    continue
                _assert_safe_path(self.artifact_root, path)
                lifecycle = {
                    "schema_version": SCHEMA_VERSION,
                    "work_item_id": self.work_item_id,
                    "loop_id": loop_id,
                    "state": "reserved",
                    "history": [{"state": "reserved", "at": _utc_now()}],
                }
                self.write_json(path, "lifecycle.json", lifecycle, exclusive=True)
                return loop_id, path
        raise RuntimeError("evaluation loop number exhausted")

    def _path(self, scope: Path, relative_path: str) -> Path:
        relative = _safe_relative_path(relative_path, "artifact relative path")
        path = scope.joinpath(*relative.split("/"))
        _assert_safe_path(self.artifact_root, path)
        return path

    def write_json(
        self,
        scope: Path,
        relative_path: str,
        payload: Mapping[str, object],
        *,
        exclusive: bool = False,
    ) -> Path:
        path = self._path(scope, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(self.artifact_root, path.parent)
        data = _pretty_bytes(payload)
        if exclusive:
            _exclusive_write_bytes(path, data)
        else:
            _atomic_write_bytes(path, data)
        return path

    def write_bytes(
        self,
        scope: Path,
        relative_path: str,
        data: bytes,
        *,
        exclusive: bool = False,
    ) -> Path:
        path = self._path(scope, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(self.artifact_root, path.parent)
        if exclusive:
            _exclusive_write_bytes(path, data)
        else:
            _atomic_write_bytes(path, data)
        return path

    def read_json(self, scope: Path, relative_path: str) -> dict[str, object]:
        return _read_json(self._path(scope, relative_path))

    def read_lifecycle(self, scope: Path) -> dict[str, object]:
        payload = self.read_json(scope, "lifecycle.json")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported evaluation lifecycle schema version")
        if payload.get("work_item_id") != self.work_item_id:
            raise ValueError("lifecycle work item does not match store")
        loop_id = scope.name
        if payload.get("loop_id") != loop_id or not LOOP_ID_PATTERN.fullmatch(loop_id):
            raise ValueError("lifecycle loop id does not match scope")
        return payload

    def transition(
        self,
        scope: Path,
        state: str,
        *,
        reason_code: str | None = None,
    ) -> dict[str, object]:
        lifecycle = self.read_lifecycle(scope)
        current = lifecycle.get("state")
        allowed = {
            "reserved": {"manifest_recorded", "terminal_incomplete"},
            "manifest_recorded": {"implement_recorded", "terminal_incomplete"},
            "implement_recorded": {"verify_recorded", "terminal_incomplete"},
            "verify_recorded": {"review_recorded", "review_skipped", "terminal_incomplete"},
            "review_recorded": {"finalized", "terminal_incomplete"},
            "review_skipped": {"finalized", "terminal_incomplete"},
            "finalized": set(),
            "terminal_incomplete": set(),
        }
        if current not in allowed or state not in allowed[current]:
            raise ValueError(f"invalid evaluation lifecycle transition: {current} -> {state}")
        history = lifecycle.get("history")
        if not isinstance(history, list):
            raise ValueError("evaluation lifecycle history must be a list")
        updated = dict(lifecycle)
        updated["state"] = state
        updated["history"] = [*history, {"state": state, "at": _utc_now()}]
        if reason_code is not None:
            updated["reason_code"] = reason_code
        self.write_json(scope, "lifecycle.json", updated)
        return updated

    def artifact_ref(self, path: Path) -> str:
        _assert_safe_path(self.artifact_root, path)
        return path.relative_to(self.artifact_root).as_posix()


def _target_payload(target: ActiveTarget) -> dict[str, str]:
    return target.to_payload()


def _target_sha256(target: ActiveTarget) -> str:
    return canonical_sha256(_target_payload(target))


def _git_identity(workdir: Path) -> dict[str, object]:
    identity: dict[str, object] = {"root": str(workdir.resolve()), "head": None}
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workdir,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=workdir,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return identity
    if root.returncode == 0 and root.stdout.strip():
        identity["root"] = str(Path(root.stdout.strip()).resolve())
    if head.returncode == 0 and head.stdout.strip():
        identity["head"] = head.stdout.strip()
    return identity


def _resolve_ref_path(
    ref: EvidenceRef,
    *,
    workdir: Path,
    artifact_root: Path,
) -> Path:
    generated_ref = ref.artifact_ref.startswith("work-items/")
    candidates = (
        [artifact_root / ref.artifact_ref, workdir / ref.artifact_ref]
        if generated_ref
        else [workdir / ref.artifact_ref, artifact_root / ref.artifact_ref]
    )
    for candidate in candidates:
        if candidate.is_symlink():
            raise ValueError(f"evidence artifact must not be a symlink: {ref.artifact_ref}")
        if candidate.is_file():
            current = candidate.parent
            while current != current.parent:
                if current.is_symlink():
                    raise ValueError(
                        f"evidence artifact contains a symlink component: {ref.artifact_ref}"
                    )
                if current == workdir or current == artifact_root:
                    break
                current = current.parent
            try:
                candidate.resolve().relative_to(workdir.resolve())
            except ValueError:
                try:
                    candidate.resolve().relative_to(artifact_root.resolve())
                except ValueError as exc:
                    raise ValueError(f"evidence artifact escapes allowed roots: {ref.artifact_ref}") from exc
            return candidate
    raise ValueError(f"evidence artifact does not exist: {ref.artifact_ref}")


def _resolve_external_refs(
    refs: Sequence[EvidenceRef],
    *,
    workdir: Path,
    artifact_root: Path,
) -> tuple[EvidenceRef, ...]:
    resolved: list[EvidenceRef] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in refs:
        path = _resolve_ref_path(ref, workdir=workdir, artifact_root=artifact_root)
        digest = sha256_file(path)
        if ref.sha256 is not None and ref.sha256 != digest:
            raise ValueError(f"evidence digest mismatch: {ref.artifact_ref}")
        if path.name == "intent.json":
            raise ValueError("implementer intent/completion artifacts cannot be review evidence")
        normalized = EvidenceRef(ref.kind, ref.artifact_ref, ref.locator, digest)
        identity = (normalized.kind, normalized.artifact_ref, normalized.locator)
        if identity in seen:
            raise ValueError(f"duplicate requirement evidence reference: {ref.artifact_ref}")
        seen.add(identity)
        resolved.append(normalized)
    return tuple(resolved)


def _file_ref(
    *,
    kind: str,
    path: Path,
    locator: str,
    store: EvaluationLoopStore,
) -> EvidenceRef:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"generated evidence is not a regular file: {path}")
    return EvidenceRef(kind, store.artifact_ref(path), locator, sha256_file(path))


def _stable_artifact_ref(value: str) -> str:
    """Remove only run-number components from generated artifact paths.

    The persisted evidence keeps its exact path, while finding identity uses a
    logical path so rerunning the same target/source does not change an ID
    merely because the immutable iteration number increased.
    """

    match = re.match(
        r"^(work-items/[^/]+/(?:iterations|evaluation-loops))/[0-9]{4}/(.*)$",
        value,
    )
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return value


def _common_binding(
    *,
    loop_id: str,
    work_item_id: str,
    target: ActiveTarget,
    manifest_sha256: str,
    repository: Mapping[str, object],
    iteration_id: str | None,
    source_sha256: str | None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "loop_id": loop_id,
        "work_item_id": work_item_id,
        "target": target.to_payload(),
        "target_sha256": _target_sha256(target),
        "manifest_sha256": manifest_sha256,
        "repository": dict(repository),
        "implementation_iteration_id": iteration_id,
        "evaluated_source_sha256": source_sha256,
    }


def _source_binding(iteration_dir: Path, outcome: Mapping[str, object]) -> str | None:
    parts: dict[str, object] = {}
    diff_path = iteration_dir / "diff.patch"
    if diff_path.is_file() and not diff_path.is_symlink():
        parts["diff_sha256"] = sha256_file(diff_path)
    paths_path = iteration_dir / "git-after-checks" / "paths.json"
    if paths_path.is_file() and not paths_path.is_symlink():
        parts["after_checks_paths_sha256"] = sha256_file(paths_path)
    artifact_digests = outcome.get("artifact_digests")
    if isinstance(artifact_digests, Mapping):
        parts["outcome_artifact_digests"] = dict(artifact_digests)
    return canonical_sha256(parts) if parts else None


def _worktree_source_binding(workdir: Path, paths: Sequence[str]) -> str:
    records: list[dict[str, object]] = []
    for relative in sorted(paths):
        path = workdir.joinpath(*relative.split("/"))
        if path.is_symlink():
            records.append({"path": relative, "kind": "symlink"})
        elif path.is_file():
            records.append({"path": relative, "kind": "file", "sha256": sha256_file(path)})
        else:
            records.append({"path": relative, "kind": "missing"})
    return canonical_sha256(records)


def _iteration_result_value(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_bytes(value: object) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False).encode("utf-8")
    raise ValueError("reviewer output must be bytes, text, or JSON data")


def _reviewer_call(reviewer: object, invocation: ReviewInvocation) -> object:
    method = getattr(reviewer, "review", None)
    if callable(method):
        return method(invocation)
    if callable(reviewer):
        return reviewer(invocation)  # type: ignore[misc]
    raise TypeError("reviewer must implement review() or be callable")


def _normalize_process_result(value: object) -> ReviewProcessResult:
    if isinstance(value, ReviewProcessResult):
        return value
    if isinstance(value, Mapping) and (
        "raw_output" in value
        or "state" in value
        or "exit_code" in value
        or "timed_out" in value
    ):
        return ReviewProcessResult(
            raw_output=value.get("raw_output"),
            state=value.get("state", "succeeded"),
            exit_code=value.get("exit_code", 0),
            timed_out=value.get("timed_out", False),
            process_error=value.get("process_error"),
            capture_failed=value.get("capture_failed", False),
            stderr=value.get("stderr"),
        )
    return ReviewProcessResult.success(value)  # type: ignore[arg-type]


def _wire_validation_error(message: str) -> dict[str, object]:
    return {"state": "schema_invalid", "errors": [message]}


class ReviewResultValidator:
    """Validate untrusted reviewer JSON and add system-owned finding fields."""

    def __init__(
        self,
        *,
        target_sha256: str,
        manifest_sha256: str,
        allowlist: Sequence[EvidenceRef],
        workdir: Path | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self.target_sha256 = target_sha256
        self.manifest_sha256 = manifest_sha256
        self.allowlist = tuple(allowlist)
        self.workdir = workdir.resolve() if workdir is not None else None
        self.artifact_root = artifact_root.resolve(strict=False) if artifact_root is not None else None

    def validate(self, raw: bytes) -> tuple[str, dict[str, object], tuple[dict[str, object], ...]]:
        if not raw.strip():
            return "empty", {"state": "empty", "errors": ["review output is empty"]}, ()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return "parse_invalid", {"state": "parse_invalid", "errors": [str(exc)]}, ()
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            return "parse_invalid", {"state": "parse_invalid", "errors": [str(exc)]}, ()
        if _max_depth(value) > MAX_JSON_DEPTH:
            return "schema_invalid", _wire_validation_error("review JSON nesting exceeds limit"), ()
        state, errors, findings = self._validate_value(value)
        validation = {"state": state, "errors": errors, "finding_count": len(findings)}
        if state == "valid":
            validation["target_sha256"] = self.target_sha256
            validation["manifest_sha256"] = self.manifest_sha256
        return state, validation, findings

    def _validate_value(
        self,
        value: object,
    ) -> tuple[str, list[str], tuple[dict[str, object], ...]]:
        if not isinstance(value, Mapping):
            return "schema_invalid", ["review result must be an object"], ()
        if set(value) != {"schema_version", "findings"}:
            return "schema_invalid", ["review result must contain only schema_version and findings"], ()
        if value.get("schema_version") != SCHEMA_VERSION:
            return "schema_invalid", ["unsupported review schema_version"], ()
        raw_findings = value.get("findings")
        if not isinstance(raw_findings, list):
            return "schema_invalid", ["findings must be an array"], ()
        if len(raw_findings) > MAX_FINDINGS:
            return "schema_invalid", [f"findings exceeds {MAX_FINDINGS} entries"], ()

        normalized: list[dict[str, object]] = []
        errors: list[str] = []
        identities: dict[str, bytes] = {}
        for index, raw_finding in enumerate(raw_findings):
            try:
                item = self._normalize_finding(raw_finding, index)
                identity = _canonical_bytes(
                    {
                        "identity_version": IDENTITY_VERSION,
                        "target_sha256": self.target_sha256,
                        "category": item["category"],
                        "finding_key": item["finding_key"],
                        "evidence": [
                            {
                                "kind": ref["kind"],
                                "artifact_ref": _stable_artifact_ref(ref["artifact_ref"]),
                                "locator": ref["locator"],
                                "sha256": ref["sha256"],
                            }
                            for ref in item["evidence"]  # type: ignore[index]
                        ],
                    }
                )
                finding_id = "F-" + hashlib.sha256(identity).hexdigest()[:32]
                previous = identities.get(finding_id)
                if previous is not None:
                    if previous != identity:
                        raise ValueError("finding ID collision")
                    raise ValueError("duplicate finding identity")
                identities[finding_id] = identity
                normalized.append(
                    {
                        "id": finding_id,
                        "identity_version": IDENTITY_VERSION,
                        "finding_key": item["finding_key"],
                        "severity": item["severity"],
                        "category": item["category"],
                        "status": "open",
                        "message": item["message"],
                        "evidence": item["evidence"],
                    }
                )
            except ValueError as exc:
                errors.append(f"finding {index}: {exc}")
        if errors:
            if any("duplicate finding identity" in error or "collision" in error for error in errors):
                return "duplicate_or_collision", errors, ()
            return "evidence_invalid" if any("evidence" in error for error in errors) else "schema_invalid", errors, ()
        return "valid", [], tuple(normalized)

    def _normalize_finding(self, raw: object, index: int) -> dict[str, object]:
        if not isinstance(raw, Mapping):
            raise ValueError("finding must be an object")
        allowed = {"finding_key", "severity", "category", "message", "evidence"}
        unknown = set(raw) - allowed
        if unknown:
            if "id" in unknown or "status" in unknown or "verdict" in unknown:
                raise ValueError("reviewer may not provide id, status, or verdict")
            raise ValueError(f"unknown finding fields: {sorted(unknown)}")
        key = raw.get("finding_key")
        if not isinstance(key, str) or len(key) > MAX_FINDING_KEY_LENGTH or not FINDING_KEY_PATTERN.fullmatch(key):
            raise ValueError("finding_key must be a lowercase stable slug")
        severity = raw.get("severity")
        if not isinstance(severity, str) or severity not in SEVERITIES:
            raise ValueError("finding severity is invalid")
        category = raw.get("category")
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ValueError("finding category is invalid")
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_LENGTH:
            raise ValueError("finding message must be a non-empty bounded string")
        raw_evidence = raw.get("evidence")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise ValueError("finding evidence must be a non-empty array")
        if len(raw_evidence) > MAX_EVIDENCE_PER_FINDING:
            raise ValueError(f"finding evidence exceeds {MAX_EVIDENCE_PER_FINDING} entries")

        refs: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for evidence_index, raw_ref in enumerate(raw_evidence):
            if not isinstance(raw_ref, Mapping):
                raise ValueError(f"evidence {evidence_index} must be an object")
            if set(raw_ref) != {"kind", "artifact_ref", "locator", "sha256"}:
                raise ValueError(f"evidence {evidence_index} must be a typed digest-bound reference")
            try:
                ref = EvidenceRef.from_value(raw_ref)
            except ValueError as exc:
                raise ValueError(f"evidence {evidence_index}: {exc}") from exc
            if ref.sha256 is None:
                raise ValueError(f"evidence {evidence_index} must include sha256")
            matching_allowlist = next((
                item
                for item in self.allowlist
                if item.kind == ref.kind
                and item.artifact_ref == ref.artifact_ref
                and item.locator == ref.locator
                and item.sha256 == ref.sha256
            ), None)
            if matching_allowlist is None:
                raise ValueError(f"evidence {evidence_index} is not in the review allowlist")
            if self.workdir is not None and self.artifact_root is not None:
                try:
                    evidence_path = _resolve_ref_path(
                        ref,
                        workdir=self.workdir,
                        artifact_root=self.artifact_root,
                    )
                    if ref.sha256 != sha256_file(evidence_path):
                        raise ValueError(f"evidence {evidence_index} digest no longer matches artifact")
                except ValueError as exc:
                    raise ValueError(f"evidence {evidence_index}: {exc}") from exc
            identity = (ref.kind, ref.artifact_ref, ref.locator, ref.sha256)
            if identity in seen:
                raise ValueError("evidence contains a duplicate reference")
            seen.add(identity)
            refs.append(ref.to_payload())
        refs.sort(key=lambda item: (item["kind"], item["artifact_ref"], item["locator"], item["sha256"]))
        kinds = {item["kind"] for item in refs}
        if category == "implementation_defect" and not (
            kinds & {"requirement", "plan"} and kinds & {"diff", "source", "check"}
        ):
            raise ValueError("implementation_defect requires requirement/plan and diff/source/check evidence")
        if category == "verification_gap" and not (
            kinds & {"requirement", "plan"} and "check" in kinds
        ):
            raise ValueError("verification_gap requires requirement/plan and check evidence")
        if category == "plan_defect" and not ({"requirement", "plan"} <= kinds):
            raise ValueError("plan_defect requires both requirement and plan evidence")
        return {
            "finding_key": key,
            "severity": severity,
            "category": category,
            "message": message,
            "evidence": refs,
        }


def _derive_verdict(
    *,
    implementation: Mapping[str, object],
    verify: Mapping[str, object],
    review_execution: Mapping[str, object],
    review_validation: Mapping[str, object],
    findings: Sequence[Mapping[str, object]],
) -> tuple[str, str, list[dict[str, object]]]:
    if implementation.get("state") != "succeeded":
        return "execution_failed", str(implementation.get("reason_code") or "implement_execution_failed"), [dict(implementation)]
    if verify.get("state") != "succeeded":
        return "execution_failed", str(verify.get("reason_code") or "targeted_check_failed"), [dict(verify)]
    if review_execution.get("state") == "timed_out":
        return "execution_failed", "review_timed_out", [dict(review_execution)]
    if review_execution.get("state") != "succeeded":
        return "execution_failed", str(review_execution.get("reason_code") or "review_execution_failed"), [dict(review_execution)]
    validation_state = review_validation.get("state")
    if validation_state != "valid":
        if validation_state == "not_started_due_to_dependency":
            return "execution_failed", "review_not_started_due_to_dependency", [dict(review_validation)]
        return "invalid_output", str(review_validation.get("reason_code") or f"review_{validation_state}"), [dict(review_validation)]
    open_findings = [item for item in findings if item.get("status") == "open"]
    if any(item.get("category") == "plan_defect" for item in open_findings):
        return "plan_defect", "open_plan_defect", [dict(item) for item in open_findings]
    if open_findings:
        return "changes_requested", "open_review_findings", [dict(item) for item in open_findings]
    return "satisfied", "validated_no_findings", [dict(review_validation)]


def derive_verdict(
    *,
    implementation: Mapping[str, object],
    verify: Mapping[str, object],
    review_execution: Mapping[str, object],
    review_validation: Mapping[str, object],
    findings: Sequence[Mapping[str, object]] = (),
) -> tuple[str, str]:
    """Pure decision-table entry point used by contract tests and consumers."""

    verdict, reason, _ = _derive_verdict(
        implementation=implementation,
        verify=verify,
        review_execution=review_execution,
        review_validation=review_validation,
        findings=findings,
    )
    return verdict, reason


def render_summary(result: Mapping[str, object], *, result_sha256: str) -> str:
    target = result.get("target")
    findings = result.get("findings")
    finding_ids = [item.get("id") for item in findings if isinstance(item, Mapping)] if isinstance(findings, list) else []
    lines = [
        "# Evaluation loop summary",
        "",
        f"- Loop: `{_clean_summary_text(result.get('loop_id'))}`",
        f"- Work item: `{_clean_summary_text(result.get('work_item_id'))}`",
        f"- Target: `{_json_summary_value(target)}`",
        f"- Verdict: `{_clean_summary_text(result.get('verdict'))}`",
        f"- Decision reason: `{_clean_summary_text(result.get('decision_reason'))}`",
        f"- Result SHA-256: `{result_sha256}`",
        f"- Finding IDs: `{_json_summary_value(finding_ids)}`",
        "",
        "The verdict is derived from the recorded Implement, Verify, and Review channels.",
        "`satisfied` means that the required bound evidence produced a valid review with no open findings; it is not a proof of complete correctness.",
        "",
        "## Findings",
        "",
    ]
    if isinstance(findings, list) and findings:
        for finding in findings:
            if isinstance(finding, Mapping):
                lines.append(
                    f"- `{_clean_summary_text(finding.get('id'))}` "
                    f"`{_clean_summary_text(finding.get('severity'))}` "
                    f"`{_clean_summary_text(finding.get('category'))}` "
                    f"{_clean_summary_text(finding.get('message'))}"
                )
    else:
        lines.append("- No open findings.")
    return "\n".join(lines) + "\n"


class VerdictFinalizer:
    """Build a machine result and human summary from validated observations."""

    @staticmethod
    def build_result(
        *,
        loop_id: str,
        work_item_id: str,
        target: ActiveTarget,
        manifest_sha256: str,
        observations: Mapping[str, Mapping[str, object]],
        findings: Sequence[Mapping[str, object]],
        artifact_digests: Mapping[str, str],
    ) -> dict[str, object]:
        implementation = observations["implement"]
        verify = observations["verify"]
        review_execution = observations["review_execution"]
        review_validation = observations["review_validation"]
        verdict, reason, decision_records = _derive_verdict(
            implementation=implementation,
            verify=verify,
            review_execution=review_execution,
            review_validation=review_validation,
            findings=findings,
        )
        decision_evidence: list[dict[str, str]] = []
        validation_ref = review_validation.get("record_ref")
        validation_digest = review_validation.get("record_sha256")
        if isinstance(validation_ref, str) and isinstance(validation_digest, str):
            decision_evidence.append(
                {"artifact_ref": validation_ref, "sha256": validation_digest}
            )
        for record in decision_records:
            ref = record.get("record_ref")
            digest = record.get("record_sha256")
            if isinstance(ref, str) and isinstance(digest, str):
                decision_evidence.append({"artifact_ref": ref, "sha256": digest})
            elif isinstance(record.get("id"), str):
                decision_evidence.append({"finding_id": str(record["id"])})
        unique_decision_evidence: list[dict[str, str]] = []
        seen_decision_evidence: set[tuple[tuple[str, str], ...]] = set()
        for evidence in decision_evidence:
            key = tuple(sorted(evidence.items()))
            if key not in seen_decision_evidence:
                seen_decision_evidence.add(key)
                unique_decision_evidence.append(evidence)
        return {
            "schema_version": SCHEMA_VERSION,
            "loop_id": loop_id,
            "work_item_id": work_item_id,
            "target": target.to_payload(),
            "target_sha256": _target_sha256(target),
            "manifest_sha256": manifest_sha256,
            "verdict": verdict,
            "decision_reason": reason,
            "observations": {
                key: dict(value)
                for key, value in observations.items()
            },
            "findings": [dict(item) for item in findings],
            "decision_evidence": unique_decision_evidence,
            "artifact_digests": dict(artifact_digests),
        }


@dataclass
class _StageState:
    state: str
    reason_code: str | None = None
    record_ref: str | None = None
    record_sha256: str | None = None


class EvaluationLoopOrchestrator:
    """Compose the fixed stages exactly once for one active target."""

    def __init__(
        self,
        *,
        workdir: Path,
        artifact_root: Path,
        executor: Callable[[SingleChangeRequest, IterationScope], object] | None = None,
        implementer: Callable[[SingleChangeRequest, IterationScope], object] | None = None,
    ) -> None:
        self.workdir = workdir.resolve()
        self.artifact_root = artifact_root
        if executor is not None and implementer is not None:
            raise ValueError("provide executor or implementer, not both")
        self.executor = executor or implementer or (lambda _request, _scope: None)

    def run(self, request: EvaluationLoopRequest) -> EvaluationLoopResult:
        validated = EvaluationLoopRequestValidator(self.workdir).validate(request)
        resolved_requirements = _resolve_external_refs(
            validated.requirement_refs,
            workdir=self.workdir,
            artifact_root=self.artifact_root.resolve(strict=False),
        )
        store = EvaluationLoopStore(self.artifact_root, validated.work_item_id)
        loop_id, scope = store.reserve()
        target = validated.single_change.active_targets[0]
        repository = _git_identity(self.workdir)
        manifest_base: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "loop_id": loop_id,
            "work_item_id": validated.work_item_id,
            "target": target.to_payload(),
            "target_sha256": _target_sha256(target),
            "requirements": [item.to_payload() for item in resolved_requirements],
            "single_change": {
                "change_intent": validated.single_change.change_intent,
                "allowed_paths": list(validated.single_change.allowed_paths),
                "checks": [self._check_payload(check) for check in validated.single_change.checks],
            },
            "expected_checks": [self._check_payload(check) for check in validated.single_change.checks],
            "repository": repository,
            "limits": {
                "review_timeout_seconds": validated.review_timeout_seconds,
                "review_output_limit_bytes": validated.review_output_limit_bytes,
                "max_findings": MAX_FINDINGS,
                "max_evidence_per_finding": MAX_EVIDENCE_PER_FINDING,
            },
            "created_at": _utc_now(),
        }
        manifest_sha256 = canonical_sha256(manifest_base)
        manifest = dict(manifest_base)
        manifest["manifest_sha256"] = manifest_sha256
        store.write_json(scope, "manifest.json", manifest, exclusive=True)
        store.transition(scope, "manifest_recorded")

        implementation = self._run_implementation(
            store=store,
            scope=scope,
            request=validated,
            target=target,
            manifest_sha256=manifest_sha256,
            repository=repository,
        )
        implementation_path = store.write_json(scope, "implementation.json", implementation, exclusive=True)
        implementation["record_ref"] = store.artifact_ref(implementation_path)
        implementation["record_sha256"] = sha256_file(implementation_path)
        store.transition(scope, "implement_recorded")

        verify = self._build_verify_record(
            store=store,
            scope=scope,
            request=validated,
            target=target,
            manifest_sha256=manifest_sha256,
            repository=repository,
            implementation=implementation,
        )
        verify_path = store.write_json(scope, "verify/execution.json", verify, exclusive=True)
        verify["record_ref"] = store.artifact_ref(verify_path)
        verify["record_sha256"] = sha256_file(verify_path)
        store.transition(scope, "verify_recorded")

        if implementation.get("state") != "succeeded" or verify.get("state") != "succeeded":
            review_execution = self._review_skip_record(
                store=store,
                scope=scope,
                common=self._common(
                    loop_id,
                    validated,
                    target,
                    manifest_sha256,
                    repository,
                    implementation,
                ),
                reason="not_started_due_to_dependency",
            )
            review_validation = {
                **self._common(
                    loop_id,
                    validated,
                    target,
                    manifest_sha256,
                    repository,
                    implementation,
                ),
                "stage": "review_validation",
                "state": "not_started_due_to_dependency",
                "reason_code": "verify_dependency_failed",
                "record_ref": store.artifact_ref(scope / "review" / "validation.json"),
            }
            review_validation_path = store.write_json(
                scope,
                "review/validation.json",
                review_validation,
                exclusive=True,
            )
            review_validation["record_sha256"] = sha256_file(review_validation_path)
            findings: tuple[dict[str, object], ...] = ()
            store.transition(scope, "review_skipped", reason_code="verify_dependency_failed")
        else:
            review_execution, review_validation, findings = self._run_review(
                store=store,
                scope=scope,
                request=validated,
                requirements=resolved_requirements,
                target=target,
                manifest_sha256=manifest_sha256,
                repository=repository,
                implementation=implementation,
                verify=verify,
            )
            store.transition(scope, "review_recorded")

        observations = {
            "implement": implementation,
            "verify": verify,
            "review_execution": review_execution,
            "review_validation": review_validation,
            "review": {
                "state": (
                    review_validation.get("state")
                    if review_execution.get("state") == "succeeded"
                    else review_execution.get("state")
                ),
                "execution_state": review_execution.get("state"),
                "validation_state": review_validation.get("state"),
                "record_ref": review_validation.get("record_ref"),
                "record_sha256": review_validation.get("record_sha256"),
            },
        }
        artifact_digests = self._artifact_digests(scope)
        result = VerdictFinalizer.build_result(
            loop_id=loop_id,
            work_item_id=validated.work_item_id,
            target=target,
            manifest_sha256=manifest_sha256,
            observations=observations,
            findings=findings,
            artifact_digests=artifact_digests,
        )
        result_path = store.write_json(scope, "result.json", result, exclusive=True)
        result_sha256 = sha256_file(result_path)
        summary = render_summary(result, result_sha256=result_sha256)
        summary_path = store.write_bytes(scope, "summary.md", summary.encode("utf-8"), exclusive=True)
        marker = {
            "schema_version": SCHEMA_VERSION,
            "loop_id": loop_id,
            "result_sha256": result_sha256,
            "summary_sha256": sha256_file(summary_path),
            "finalized_at": _utc_now(),
        }
        store.transition(scope, "finalized")
        store.write_json(scope, "finalized", marker, exclusive=True)
        return EvaluationLoopResult(loop_id, scope, str(result["verdict"]), result)

    @staticmethod
    def _check_payload(check: object) -> dict[str, object]:
        payload = check.to_payload()
        return dict(payload)

    @staticmethod
    def _common(
        loop_id: str,
        request: EvaluationLoopRequest,
        target: ActiveTarget,
        manifest_sha256: str,
        repository: Mapping[str, object],
        implementation: Mapping[str, object],
    ) -> dict[str, object]:
        return _common_binding(
            loop_id=loop_id,
            work_item_id=request.work_item_id,
            target=target,
            manifest_sha256=manifest_sha256,
            repository=repository,
            iteration_id=implementation.get("iteration_id") if isinstance(implementation.get("iteration_id"), str) else None,
            source_sha256=implementation.get("evaluated_source_sha256") if isinstance(implementation.get("evaluated_source_sha256"), str) else None,
        )

    def _run_implementation(
        self,
        *,
        store: EvaluationLoopStore,
        scope: Path,
        request: EvaluationLoopRequest,
        target: ActiveTarget,
        manifest_sha256: str,
        repository: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            iteration_result = run_single_change(
                request.single_change,
                workdir=self.workdir,
                artifact_root=self.artifact_root,
                executor=self.executor,
                excluded_paths={".kelpie"},
            )
        except BaseException as exc:
            return {
                **_common_binding(
                    loop_id=scope.name,
                    work_item_id=request.work_item_id,
                    target=target,
                    manifest_sha256=manifest_sha256,
                    repository=repository,
                    iteration_id=None,
                    source_sha256=None,
                ),
                "stage": "implement",
                "state": "failed",
                "reason_code": "implement_execution_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "record_ref": "implementation.json",
            }
        iteration_id = _iteration_result_value(iteration_result, "iteration_id")
        iteration_dir_value = _iteration_result_value(iteration_result, "iteration_dir")
        outcome = _iteration_result_value(iteration_result, "outcome", {})
        status = _iteration_result_value(iteration_result, "status", "failed")
        if not isinstance(iteration_id, str) or not isinstance(iteration_dir_value, Path):
            return {
                **_common_binding(
                    loop_id=scope.name,
                    work_item_id=request.work_item_id,
                    target=target,
                    manifest_sha256=manifest_sha256,
                    repository=repository,
                    iteration_id=None,
                    source_sha256=None,
                ),
                "stage": "implement",
                "state": "failed",
                "reason_code": "implementation_artifact_invalid",
                "record_ref": "implementation.json",
            }
        if not isinstance(outcome, Mapping):
            outcome = {}
        iteration_dir = iteration_dir_value.resolve(strict=False)
        try:
            expected_iteration_dir = (
                store.artifact_root
                / "work-items"
                / request.work_item_id
                / "iterations"
                / iteration_id
            ).resolve(strict=False)
            _assert_safe_path(store.artifact_root, iteration_dir)
            if iteration_dir != expected_iteration_dir:
                raise ValueError("implementation iteration artifact is outside its bound namespace")
        except (OSError, ValueError) as exc:
            return {
                **_common_binding(
                    loop_id=scope.name,
                    work_item_id=request.work_item_id,
                    target=target,
                    manifest_sha256=manifest_sha256,
                    repository=repository,
                    iteration_id=None,
                    source_sha256=None,
                ),
                "stage": "implement",
                "state": "failed",
                "reason_code": "implementation_artifact_invalid",
                "error": str(exc),
                "record_ref": "implementation.json",
            }
        source_sha256 = _source_binding(iteration_dir, outcome)
        worktree_source_sha256 = _worktree_source_binding(
            self.workdir,
            request.single_change.allowed_paths,
        )
        outcome_reason_codes = (
            [str(item) for item in outcome.get("reason_codes", [])]
            if isinstance(outcome.get("reason_codes", []), list)
            else []
        )
        # ``run_single_change`` aggregates implementation/capture failures and
        # targeted-check failures into one terminal status.  The evaluation
        # contract must keep those channels separate: a completed mutation and
        # capture remains a successful Implement stage when only the checks
        # failed, and Verify owns the resulting execution failure.
        implementation_reason_codes = [
            code
            for code in outcome_reason_codes
            if code not in {"check_failed", "check_timeout"}
        ]
        state = "succeeded" if not implementation_reason_codes else "failed"
        record: dict[str, object] = {
            **_common_binding(
                loop_id=scope.name,
                work_item_id=request.work_item_id,
                target=target,
                manifest_sha256=manifest_sha256,
                repository=repository,
                iteration_id=iteration_id,
                source_sha256=source_sha256,
            ),
            "stage": "implement",
            "state": state,
            "reason_code": None if state == "succeeded" else "implement_execution_failed",
            "iteration_id": iteration_id,
            "iteration_artifact_ref": str(iteration_dir),
            "outcome_ref": str(iteration_dir / "outcome.json"),
            "diff_ref": str(iteration_dir / "diff.patch"),
            "evaluated_worktree_source_sha256": worktree_source_sha256,
            "record_ref": "implementation.json",
            "outcome_status": status,
            "outcome_reason_codes": outcome_reason_codes,
            "implementation_reason_codes": implementation_reason_codes,
            "artifact_digests": dict(outcome.get("artifact_digests", {})) if isinstance(outcome.get("artifact_digests", {}), Mapping) else {},
        }
        return record

    def _build_verify_record(
        self,
        *,
        store: EvaluationLoopStore,
        scope: Path,
        request: EvaluationLoopRequest,
        target: ActiveTarget,
        manifest_sha256: str,
        repository: Mapping[str, object],
        implementation: Mapping[str, object],
    ) -> dict[str, object]:
        common = self._common(scope.name, request, target, manifest_sha256, repository, implementation)
        iteration_dir_text = implementation.get("iteration_artifact_ref")
        if not isinstance(iteration_dir_text, str):
            return {
                **common,
                "stage": "verify",
                "state": "failed",
                "reason_code": "invalid_verify_record",
                "record_ref": "verify/execution.json",
                "records": [],
            }
        iteration_dir = Path(iteration_dir_text)
        outcome_path = iteration_dir / "outcome.json"
        try:
            outcome = _read_json(outcome_path)
        except ValueError as exc:
            return {
                **common,
                "stage": "verify",
                "state": "failed",
                "reason_code": "invalid_verify_record",
                "error": str(exc),
                "record_ref": "verify/execution.json",
                "records": [],
            }
        if (
            outcome.get("iteration_id") != implementation.get("iteration_id")
            or outcome.get("work_item_id") != request.work_item_id
            or outcome.get("target") != target.to_payload()
        ):
            return {
                **common,
                "stage": "verify",
                "state": "failed",
                "reason_code": "verify_binding_mismatch",
                "record_ref": "verify/execution.json",
                "records": [],
            }
        raw_results = outcome.get("check_results")
        checks_attempted = outcome.get("checks_attempted")
        if checks_attempted is not True or not isinstance(raw_results, list) or len(raw_results) != len(request.single_change.checks):
            return {
                **common,
                "stage": "verify",
                "state": "failed",
                "reason_code": "invalid_verify_record",
                "record_ref": "verify/execution.json",
                "records": [],
            }
        records: list[dict[str, object]] = []
        reason_code: str | None = None
        invalid = False
        for index, (expected, raw) in enumerate(zip(request.single_change.checks, raw_results), start=1):
            if not isinstance(raw, Mapping):
                invalid = True
                continue
            expected_payload = self._check_payload(expected)
            actual_payload = {
                key: raw.get(key)
                for key in ("argv", "cwd", "timeout_seconds", "env_allowlist", "output_limit_bytes")
            }
            if actual_payload != expected_payload:
                invalid = True
            status = raw.get("status")
            exit_code = raw.get("exit_code")
            timed_out = raw.get("timed_out")
            process_error = raw.get("process_error")
            if status == "passed":
                if exit_code != 0 or timed_out is not False or process_error not in (None, ""):
                    invalid = True
            elif status == "failed":
                if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code == 0 or timed_out is not False:
                    invalid = True
                reason_code = reason_code or "targeted_check_failed"
            elif status == "timeout":
                if timed_out is not True:
                    invalid = True
                reason_code = reason_code or "verify_timed_out"
            elif status == "error":
                if not isinstance(process_error, str) or not process_error or exit_code is not None:
                    invalid = True
                reason_code = reason_code or "targeted_check_execution_failed"
            else:
                invalid = True
            record_name = raw.get("check_id")
            if record_name != f"{index:04d}":
                invalid = True
            record_path = iteration_dir / "checks" / f"{index:04d}.json"
            if not record_path.is_file() or record_path.is_symlink():
                invalid = True
            else:
                records.append(
                    {
                        "check_id": record_name,
                        "artifact_ref": store.artifact_ref(record_path),
                        "sha256": sha256_file(record_path),
                        "status": status,
                        "exit_code": exit_code,
                    }
                )
        if invalid:
            reason_code = "invalid_verify_record"
        elif reason_code is None:
            reason_code = None
        state = "succeeded" if not invalid and reason_code is None else "failed"
        return {
            **common,
            "stage": "verify",
            "state": state,
            "reason_code": reason_code,
            "record_ref": "verify/execution.json",
            "records": records,
            "check_results": [dict(item) for item in raw_results if isinstance(item, Mapping)],
            "checks_attempted": True,
        }

    def _review_skip_record(
        self,
        *,
        store: EvaluationLoopStore,
        scope: Path,
        common: Mapping[str, object],
        reason: str,
    ) -> dict[str, object]:
        record = {
            **dict(common),
            "stage": "review_execution",
            "state": "not_started_due_to_dependency",
            "reason_code": reason,
            "record_ref": store.artifact_ref(scope / "review" / "execution.json"),
        }
        execution_path = store.write_json(scope, "review/execution.json", record, exclusive=True)
        record["record_sha256"] = sha256_file(execution_path)
        return record

    def _build_allowlist(
        self,
        *,
        store: EvaluationLoopStore,
        iteration_dir: Path,
        requirements: Sequence[EvidenceRef],
        verify: Mapping[str, object],
    ) -> tuple[EvidenceRef, ...]:
        refs: list[EvidenceRef] = list(requirements)
        diff_path = iteration_dir / "diff.patch"
        if diff_path.is_file():
            refs.append(_file_ref(kind="diff", path=diff_path, locator="diff", store=store))
        for item in verify.get("records", []):
            if not isinstance(item, Mapping):
                continue
            artifact_ref = item.get("artifact_ref")
            if not isinstance(artifact_ref, str):
                continue
            path = store.artifact_root / artifact_ref
            if path.is_file():
                refs.append(_file_ref(kind="check", path=path, locator=str(item.get("check_id", "check")), store=store))
        paths_path = iteration_dir / "git-after-checks" / "paths.json"
        if paths_path.is_file():
            try:
                paths_payload = _read_json(paths_path)
            except ValueError:
                paths_payload = {}
            for item in paths_payload.get("paths", []):
                if not isinstance(item, Mapping) or item.get("kind") != "regular":
                    continue
                image = item.get("image")
                relative_path = item.get("path")
                if not isinstance(image, str) or not isinstance(relative_path, str):
                    continue
                image_path = iteration_dir / "git-after-checks" / image
                if image_path.is_file() and not image_path.is_symlink():
                    refs.append(_file_ref(kind="source", path=image_path, locator=relative_path, store=store))
        unique: dict[tuple[str, str, str, str | None], EvidenceRef] = {}
        for ref in refs:
            unique[(ref.kind, ref.artifact_ref, ref.locator, ref.sha256)] = ref
        return tuple(unique.values())

    def _review_binding_error(
        self,
        *,
        store: EvaluationLoopStore,
        scope: Path,
        input_path: Path,
        input_sha256: str,
        manifest_sha256: str,
        allowlist: Sequence[EvidenceRef],
    ) -> str | None:
        try:
            manifest = _read_json(scope / "manifest.json")
            claimed = manifest.get("manifest_sha256")
            manifest_without_digest = dict(manifest)
            manifest_without_digest.pop("manifest_sha256", None)
            if claimed != manifest_sha256 or canonical_sha256(manifest_without_digest) != manifest_sha256:
                return "review manifest binding changed"
            if sha256_file(input_path) != input_sha256:
                return "review input binding changed"
            for ref in allowlist:
                if ref.sha256 is None:
                    return "review allowlist contains an unbound evidence reference"
                path = _resolve_ref_path(
                    ref,
                    workdir=self.workdir,
                    artifact_root=store.artifact_root,
                )
                if sha256_file(path) != ref.sha256:
                    return f"review evidence binding changed: {ref.artifact_ref}"
        except (OSError, ValueError) as exc:
            return f"review binding validation failed: {exc}"
        return None

    def _run_review(
        self,
        *,
        store: EvaluationLoopStore,
        scope: Path,
        request: EvaluationLoopRequest,
        requirements: Sequence[EvidenceRef],
        target: ActiveTarget,
        manifest_sha256: str,
        repository: Mapping[str, object],
        implementation: Mapping[str, object],
        verify: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object], tuple[dict[str, object], ...]]:
        iteration_dir = Path(str(implementation["iteration_artifact_ref"]))
        allowlist = self._build_allowlist(
            store=store,
            iteration_dir=iteration_dir,
            requirements=requirements,
            verify=verify,
        )
        input_payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "loop_id": scope.name,
            "work_item_id": request.work_item_id,
            "target": target.to_payload(),
            "target_sha256": _target_sha256(target),
            "manifest_sha256": manifest_sha256,
            "evidence": [item.to_payload() for item in allowlist],
            "limits": {
                "timeout_seconds": request.review_timeout_seconds,
                "output_limit_bytes": request.review_output_limit_bytes,
            },
        }
        input_path = store.write_json(scope, "review/input.json", input_payload, exclusive=True)
        input_sha256 = sha256_file(input_path)
        invocation = ReviewInvocation(
            loop_id=scope.name,
            work_item_id=request.work_item_id,
            target=target,
            target_sha256=_target_sha256(target),
            manifest_sha256=manifest_sha256,
            input_path=input_path,
            evidence=allowlist,
            input_manifest=input_payload,
        )
        started_at = _utc_now()
        try:
            process = _normalize_process_result(_reviewer_call(request.reviewer, invocation))
        except BaseException as exc:
            process = ReviewProcessResult.failure(f"{type(exc).__name__}: {exc}")
        binding_error = self._review_binding_error(
            store=store,
            scope=scope,
            input_path=input_path,
            input_sha256=input_sha256,
            manifest_sha256=manifest_sha256,
            allowlist=allowlist,
        )
        if binding_error is not None:
            process = ReviewProcessResult.failure(
                binding_error,
                raw_output=process.raw_output if isinstance(process.raw_output, (bytes, str)) else None,
            )
        if (
            implementation.get("evaluated_worktree_source_sha256")
            != _worktree_source_binding(self.workdir, request.single_change.allowed_paths)
        ):
            process = ReviewProcessResult.failure(
                "review source binding changed",
                raw_output=process.raw_output if isinstance(process.raw_output, (bytes, str)) else None,
            )
        conversion_error: str | None = None
        try:
            raw_bytes = _as_bytes(process.raw_output)
        except ValueError as exc:
            # The reviewer process itself succeeded, but its returned value
            # is outside the wire contract.  Keep this in invalid-output,
            # rather than misclassifying it as an execution failure.
            raw_bytes = b""
            conversion_error = str(exc)
        output_limit = request.review_output_limit_bytes
        full_digest = sha256_bytes(raw_bytes)
        stored_bytes = raw_bytes[:output_limit]
        truncated = len(raw_bytes) > output_limit or process.capture_failed or process.state == "truncated"
        state = process.state if isinstance(process.state, str) else "failed"
        if state not in REVIEW_EXECUTION_STATES:
            state = "failed"
        if process.timed_out:
            state = "timed_out"
        elif process.process_error or process.capture_failed or (process.exit_code not in (None, 0)):
            state = "failed"
        ended_at = _utc_now()
        raw_path = store.write_bytes(scope, "review/raw-output.bin", stored_bytes, exclusive=True)
        raw_meta = {
            **self._common(scope.name, request, target, manifest_sha256, repository, implementation),
            "schema_version": SCHEMA_VERSION,
            "raw_output_ref": store.artifact_ref(raw_path),
            "full_sha256": full_digest,
            "stored_sha256": sha256_bytes(stored_bytes),
            "byte_count": len(raw_bytes),
            "stored_byte_count": len(stored_bytes),
            "truncated": truncated,
            "encoding": "utf-8",
        }
        raw_meta_path = store.write_json(scope, "review/raw-output.json", raw_meta, exclusive=True)
        execution = {
            **self._common(scope.name, request, target, manifest_sha256, repository, implementation),
            "stage": "review_execution",
            "state": "timed_out" if state == "timed_out" else state,
            "reason_code": (
                "review_timed_out" if state == "timed_out" else
                ("review_capture_failed" if process.capture_failed else
                 ("review_execution_failed" if state != "succeeded" else None))
            ),
            "started_at": started_at,
            "finished_at": ended_at,
            "exit_code": process.exit_code,
            "timed_out": process.timed_out,
            "process_error": process.process_error,
            "raw_output_ref": store.artifact_ref(raw_path),
            "raw_metadata_ref": store.artifact_ref(raw_meta_path),
            "record_ref": store.artifact_ref(scope / "review" / "execution.json"),
            "truncated": truncated,
        }
        execution_path = store.write_json(scope, "review/execution.json", execution, exclusive=True)
        execution["record_sha256"] = sha256_file(execution_path)
        # The record is immutable.  Its digest is intentionally the digest of
        # the persisted record before the self-referential convenience field.
        validation_state: str
        if state != "succeeded":
            validation_state = "execution_failed"
            validation_payload = {
                **self._common(scope.name, request, target, manifest_sha256, repository, implementation),
                "stage": "review_validation",
                "state": validation_state,
                "reason_code": execution["reason_code"],
                "errors": [process.process_error or "review process did not succeed"],
                "raw_output_ref": store.artifact_ref(raw_path),
                "record_ref": store.artifact_ref(scope / "review" / "validation.json"),
            }
            validation_path = store.write_json(
                scope,
                "review/validation.json",
                validation_payload,
                exclusive=True,
            )
            validation_payload["record_sha256"] = sha256_file(validation_path)
            return execution, validation_payload, ()
        if truncated:
            validation_state = "truncated"
            validation_payload = {
                **self._common(scope.name, request, target, manifest_sha256, repository, implementation),
                "stage": "review_validation",
                "state": validation_state,
                "reason_code": "review_output_truncated",
                "errors": ["review output exceeded the configured byte limit"],
                "raw_output_ref": store.artifact_ref(raw_path),
                "record_ref": store.artifact_ref(scope / "review" / "validation.json"),
            }
            validation_path = store.write_json(
                scope,
                "review/validation.json",
                validation_payload,
                exclusive=True,
            )
            validation_payload["record_sha256"] = sha256_file(validation_path)
            return execution, validation_payload, ()
        if conversion_error is not None:
            validation_payload = {
                **self._common(scope.name, request, target, manifest_sha256, repository, implementation),
                "stage": "review_validation",
                "state": "schema_invalid",
                "reason_code": "review_schema_invalid",
                "errors": [conversion_error],
                "finding_count": 0,
                "raw_output_ref": store.artifact_ref(raw_path),
                "record_ref": store.artifact_ref(scope / "review" / "validation.json"),
            }
            validation_path = store.write_json(
                scope,
                "review/validation.json",
                validation_payload,
                exclusive=True,
            )
            validation_payload["record_sha256"] = sha256_file(validation_path)
            return execution, validation_payload, ()
        validator = ReviewResultValidator(
            target_sha256=_target_sha256(target),
            manifest_sha256=manifest_sha256,
            allowlist=allowlist,
            workdir=self.workdir,
            artifact_root=store.artifact_root,
        )
        validation_state, validation_info, findings = validator.validate(stored_bytes)
        validation_payload = {
            **self._common(scope.name, request, target, manifest_sha256, repository, implementation),
            "stage": "review_validation",
            "state": validation_state,
            "reason_code": None if validation_state == "valid" else f"review_{validation_state}",
            "errors": validation_info.get("errors", []),
            "finding_count": len(findings),
            "raw_output_ref": store.artifact_ref(raw_path),
            "record_ref": store.artifact_ref(scope / "review" / "validation.json"),
        }
        if validation_state == "valid":
            validated_payload = {
                **self._common(scope.name, request, target, manifest_sha256, repository, implementation),
                "schema_version": SCHEMA_VERSION,
                "findings": [dict(item) for item in findings],
                "raw_output_sha256": full_digest,
            }
            validated_path = store.write_json(scope, "review/validated.json", validated_payload, exclusive=True)
            validation_payload["validated_ref"] = store.artifact_ref(validated_path)
        validation_path = store.write_json(
            scope,
            "review/validation.json",
            validation_payload,
            exclusive=True,
        )
        validation_payload["record_sha256"] = sha256_file(validation_path)
        return execution, validation_payload, findings

    @staticmethod
    def _artifact_digests(scope: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in sorted(scope.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(scope).as_posix()
            if relative in {"lifecycle.json", "result.json", "summary.md", "finalized"}:
                continue
            result[relative] = sha256_file(path)
        return result


def run_evaluation_loop(
    request: EvaluationLoopRequest | Mapping[str, object],
    *,
    workdir: Path,
    artifact_root: Path,
    executor: Callable[[SingleChangeRequest, IterationScope], object] | None = None,
    implementer: Callable[[SingleChangeRequest, IterationScope], object] | None = None,
    reviewer: Reviewer | Callable[[ReviewInvocation], object] | None = None,
) -> EvaluationLoopResult:
    """Run one fixed evaluation loop without retry or automatic fixes."""

    if isinstance(request, Mapping):
        request = EvaluationLoopRequest.from_mapping(request)
    if not isinstance(request, EvaluationLoopRequest):
        raise TypeError("request must be an EvaluationLoopRequest or mapping")
    if reviewer is not None:
        request = EvaluationLoopRequest(
            work_item_id=request.work_item_id,
            single_change=request.single_change,
            requirement_refs=request.requirement_refs,
            reviewer=reviewer,
            review_timeout_seconds=request.review_timeout_seconds,
            review_output_limit_bytes=request.review_output_limit_bytes,
        )
    return EvaluationLoopOrchestrator(
        workdir=workdir,
        artifact_root=artifact_root,
        executor=executor,
        implementer=implementer,
    ).run(request)


__all__ = [
    "CATEGORIES",
    "DEFAULT_REVIEW_OUTPUT_BYTES",
    "DEFAULT_REVIEW_TIMEOUT_SECONDS",
    "EVIDENCE_KINDS",
    "EvaluationLoopOrchestrator",
    "EvaluationLoopRequest",
    "EvaluationLoopRequestValidator",
    "EvaluationLoopResult",
    "EvaluationLoopStore",
    "EvidenceRef",
    "IDENTITY_VERSION",
    "ReviewInvocation",
    "ReviewProcessResult",
    "ReviewResultValidator",
    "Reviewer",
    "SCHEMA_VERSION",
    "VerdictFinalizer",
    "derive_verdict",
    "render_summary",
    "run_evaluation_loop",
]
