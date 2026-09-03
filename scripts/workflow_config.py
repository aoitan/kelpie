"""Strict JSON v1 workflow configuration loading.

This module owns the untrusted configuration boundary for the configured
workflow work.  It deliberately stops at immutable, version-specific DTOs:
capability authorization, artifact graph construction, and execution belong
to later pipeline stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field, fields, is_dataclass
import hashlib
import inspect
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import tempfile
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping, Protocol, Sequence
import unicodedata


WORKFLOW_SCHEMA_VERSION = "1.0"
SUPPORTED_WORKFLOW_SCHEMA_VERSIONS = frozenset({WORKFLOW_SCHEMA_VERSION})

# These are parser hard limits.  Config-declared limits are represented in
# ``WorkflowLimits`` but cannot raise these loader limits.
MAX_WORKFLOW_CONFIG_BYTES = 1024 * 1024
MAX_WORKFLOW_JSON_DEPTH = 32
MAX_WORKFLOW_STRING_BYTES = 64 * 1024
MAX_CAPABILITY_RESOURCE_BYTES = 1024 * 1024
CAPABILITY_REGISTRY_VERSION = "1.0"
DEFAULT_CAPABILITY_PROFILE = "repository_issue"
CAPABILITY_KINDS = frozenset(
    {"runner", "lifecycle", "loop_controller", "virtual_input", "loop_source"}
)
CAPABILITY_ID_PATTERN = re.compile(r"^[A-Za-z0-9$][A-Za-z0-9$_.:-]{0,127}$")

# Short aliases make the boundaries convenient for callers without creating a
# second set of independent limits.
MAX_CONFIG_BYTES = MAX_WORKFLOW_CONFIG_BYTES
MAX_JSON_DEPTH = MAX_WORKFLOW_JSON_DEPTH

# These are structural/runtime hard caps for the bounded loop preflight.  A
# workflow may request a lower value through ``WorkflowLimits`` but cannot
# raise one of these process-level ceilings.  The values are deliberately
# finite even when a caller supplies an untrusted or non-terminating source.
MAX_WORKFLOW_NODES = 256
MAX_WORKFLOW_LOOPS = 32
MAX_WORKFLOW_BODY_STEPS = 64
MAX_WORKFLOW_LOOP_ITEMS = 1000
MAX_WORKFLOW_ITEM_BYTES = 256 * 1024
MAX_WORKFLOW_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_WORKFLOW_TOTAL_STEPS = 10_000
MAX_WORKFLOW_PROMPT_INPUT_BYTES = 8 * 1024 * 1024

# Artifact manifests are a runtime boundary, not workflow configuration.  The
# filename is intentionally hidden and therefore cannot be selected by the
# portable config path grammar (which requires an alphanumeric first segment).
ARTIFACT_MANIFEST_SCHEMA_VERSION = "1.0"
DEFAULT_ARTIFACT_MANIFEST_PATH = ".artifact-manifest.json"
ARTIFACT_SCOPE_LOCK_FILENAME = ".artifact-scope.lock"
MAX_ARTIFACT_MANIFEST_BYTES = 8 * 1024 * 1024

SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SAFE_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_MISSING = object()


class _JSONObject(dict[str, object]):
    """JSON object retaining duplicate-key information for source paths."""

    __slots__ = ("duplicate_keys",)

    def __init__(self, pairs: Iterable[tuple[str, object]]) -> None:
        super().__init__()
        duplicates: list[str] = []
        for key, value in pairs:
            if key in self and key not in duplicates:
                duplicates.append(key)
            self[key] = value
        self.duplicate_keys = tuple(duplicates)


@dataclass(frozen=True, slots=True)
class WorkflowConfigDiagnostic:
    """One stable, machine-readable configuration diagnostic."""

    code: str
    path: str
    message: str
    line: int | None = None
    column: int | None = None

    @property
    def json_pointer(self) -> str:
        return self.path

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        return result


class WorkflowConfigError(ValueError):
    """Raised when a workflow config cannot be safely loaded or parsed."""

    def __init__(
        self,
        diagnostics: Sequence[WorkflowConfigDiagnostic],
        *,
        source_path: Path | None = None,
    ) -> None:
        if not diagnostics:
            raise ValueError("WorkflowConfigError requires at least one diagnostic")
        self.diagnostics = tuple(diagnostics)
        # These aliases keep the error convenient for both callers that want
        # the complete report and callers that only need the first code.
        self.errors = self.diagnostics
        self.issues = self.diagnostics
        self.code = self.diagnostics[0].code
        self.error_code = self.code
        self.source_path = source_path
        prefix = f"{source_path}: " if source_path is not None else ""
        rendered = "\n".join(_format_diagnostic(item) for item in self.diagnostics)
        super().__init__(prefix + rendered)


# More specific names are useful to integrations while keeping one error
# contract for load, syntax, and DTO validation failures.
WorkflowConfigLoadError = WorkflowConfigError
WorkflowConfigValidationError = WorkflowConfigError
WorkflowConfigParseError = WorkflowConfigError


@dataclass(frozen=True, slots=True)
class InputBinding:
    name: str
    source: str

    @property
    def from_(self) -> str:
        """Return the JSON ``from`` value without using a reserved keyword."""

        return self.source

    @property
    def from_value(self) -> str:
        return self.source


@dataclass(frozen=True, slots=True)
class OutputDeclaration:
    id: str
    kind: str
    path: str


@dataclass(frozen=True, slots=True)
class CollectionExport:
    id: str
    source: str
    cardinality: str

    @property
    def from_(self) -> str:
        return self.source


@dataclass(frozen=True, slots=True)
class LoopSource:
    source: str
    provider: str

    @property
    def from_(self) -> str:
        return self.source


@dataclass(frozen=True, slots=True)
class WorkflowLimits:
    """Optional requested limits represented by the v1 config.

    The parser validates these as positive integers.  Enforcement against
    system hard caps is intentionally owned by the later preflight stage.
    """

    max_config_bytes: int | None = None
    max_json_depth: int | None = None
    max_nodes: int | None = None
    max_loops: int | None = None
    max_body_steps: int | None = None
    max_loop_items: int | None = None
    max_item_bytes: int | None = None
    max_snapshot_bytes: int | None = None
    max_total_steps: int | None = None
    max_prompt_input_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class WorkflowHardLimits:
    """Process-level ceilings used by bounded workflow preflight.

    The limits are not configurable by workflow JSON.  Values from
    :class:`WorkflowLimits` are intersected with this object before any loop
    source is read, so configuration can only make a run smaller.
    """

    max_config_bytes: int = MAX_WORKFLOW_CONFIG_BYTES
    max_json_depth: int = MAX_WORKFLOW_JSON_DEPTH
    max_nodes: int = MAX_WORKFLOW_NODES
    max_loops: int = MAX_WORKFLOW_LOOPS
    max_body_steps: int = MAX_WORKFLOW_BODY_STEPS
    max_loop_items: int = MAX_WORKFLOW_LOOP_ITEMS
    max_item_bytes: int = MAX_WORKFLOW_ITEM_BYTES
    max_snapshot_bytes: int = MAX_WORKFLOW_SNAPSHOT_BYTES
    max_total_steps: int = MAX_WORKFLOW_TOTAL_STEPS
    max_prompt_input_bytes: int = MAX_WORKFLOW_PROMPT_INPUT_BYTES

    def __post_init__(self) -> None:
        for field_name in (
            "max_config_bytes",
            "max_json_depth",
            "max_nodes",
            "max_loops",
            "max_body_steps",
            "max_loop_items",
            "max_item_bytes",
            "max_snapshot_bytes",
            "max_total_steps",
            "max_prompt_input_bytes",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")

    def as_dict(self) -> dict[str, int]:
        return {
            field_name: getattr(self, field_name)
            for field_name in (
                "max_config_bytes",
                "max_json_depth",
                "max_nodes",
                "max_loops",
                "max_body_steps",
                "max_loop_items",
                "max_item_bytes",
                "max_snapshot_bytes",
                "max_total_steps",
                "max_prompt_input_bytes",
            )
        }

    def effective(self, requested: WorkflowLimits) -> "WorkflowEffectiveLimits":
        """Return the lower of the workflow request and each hard ceiling."""

        if not isinstance(requested, WorkflowLimits):
            raise TypeError("requested limits must be a WorkflowLimits")
        values = {
            field_name: min(
                getattr(self, field_name),
                requested_value,
            )
            if (requested_value := getattr(requested, field_name)) is not None
            else getattr(self, field_name)
            for field_name in self.as_dict()
        }
        return WorkflowEffectiveLimits(**values)


@dataclass(frozen=True, slots=True)
class WorkflowEffectiveLimits:
    """The limits actually enforced by one preflight."""

    max_config_bytes: int
    max_json_depth: int
    max_nodes: int
    max_loops: int
    max_body_steps: int
    max_loop_items: int
    max_item_bytes: int
    max_snapshot_bytes: int
    max_total_steps: int
    max_prompt_input_bytes: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_config_bytes",
            "max_json_depth",
            "max_nodes",
            "max_loops",
            "max_body_steps",
            "max_loop_items",
            "max_item_bytes",
            "max_snapshot_bytes",
            "max_total_steps",
            "max_prompt_input_bytes",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")

    def as_dict(self) -> dict[str, int]:
        return {
            field_name: getattr(self, field_name)
            for field_name in (
                "max_config_bytes",
                "max_json_depth",
                "max_nodes",
                "max_loops",
                "max_body_steps",
                "max_loop_items",
                "max_item_bytes",
                "max_snapshot_bytes",
                "max_total_steps",
                "max_prompt_input_bytes",
            )
        }


# These aliases keep the structural-bound vocabulary usable by callers that
# refer to the design's generic ``ResourceLimits`` name.
SystemResourceLimits = WorkflowHardLimits
ResourceLimits = WorkflowHardLimits
DEFAULT_WORKFLOW_HARD_LIMITS = WorkflowHardLimits()


@dataclass(frozen=True, slots=True)
class StepConfig:
    type: str
    id: str
    lifecycle: str
    runner: str
    prompt: str
    skill: str
    inputs: tuple[InputBinding, ...]
    outputs: tuple[OutputDeclaration, ...]
    depends_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LoopConfig:
    type: str
    id: str
    source: LoopSource
    max_items: int
    controller: str
    body: tuple[StepConfig, ...]
    exports: tuple[CollectionExport, ...]


WorkflowNode = StepConfig | LoopConfig


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    schema_version: str
    id: str
    profile: str
    limits: WorkflowLimits
    nodes: tuple[WorkflowNode, ...]

    @property
    def workflow_id(self) -> str:
        return self.id

    @property
    def pipeline(self) -> tuple[WorkflowNode, ...]:
        """Compatibility spelling for callers describing nodes as a pipeline."""

        return self.nodes

    @classmethod
    def from_dict(cls, payload: object) -> "WorkflowConfig":
        return parse_workflow_config(payload)

    @classmethod
    def from_json(cls, path: Path | str) -> "WorkflowConfig":
        return load_workflow_config(path)

    def normalize(self, *, source_path: Path | str | None = None) -> "WorkflowPlan":
        """Build the immutable structural IR for this parsed configuration."""

        return normalize_workflow_config(self, source_path=source_path)

    normalise = normalize

    def preflight_loop_sources(
        self,
        providers: object = None,
        *,
        provider_registry: object = None,
        registry: "CapabilityRegistry | CapabilityRegistrySnapshot | None" = None,
        hard_limits: "WorkflowHardLimits | None" = None,
    ) -> "WorkflowBoundsResult":
        """Normalize the config and validate its bounded loop sources."""

        return preflight_workflow_bounds(
            self,
            providers,
            provider_registry=provider_registry,
            registry=registry,
            hard_limits=hard_limits,
        )

    def preflight_artifact_namespaces(
        self,
        source_snapshots: "WorkflowBoundsResult | Mapping[str, LoopSourceSnapshot] | None" = None,
        *,
        artifact_root: Path | str | None = None,
        item_namespace: Literal["default", "work-items"] = "default",
    ) -> "ArtifactNamespacePlan":
        """Compute physical output namespaces without creating artifacts."""

        return build_artifact_namespace_plan(
            self,
            source_snapshots,
            artifact_root=artifact_root,
            item_namespace=item_namespace,
        )

    build_artifact_namespaces = preflight_artifact_namespaces
    preflight_artifacts = preflight_artifact_namespaces


# DTO aliases make the version-specific boundary explicit to callers that use
# DTO/IR terminology.  They are aliases, not mutable wrapper classes.
WorkflowConfigDTO = WorkflowConfig
WorkflowLimitsDTO = WorkflowLimits
InputBindingDTO = InputBinding
OutputDeclarationDTO = OutputDeclaration
CollectionExportDTO = CollectionExport
LoopSourceDTO = LoopSource
StepDTO = StepConfig
LoopDTO = LoopConfig


_ROOT_FIELDS = {"schema_version", "id", "profile", "limits", "nodes"}
_LIMIT_FIELDS = {
    "max_config_bytes",
    "max_json_depth",
    "max_nodes",
    "max_loops",
    "max_body_steps",
    "max_loop_items",
    "max_item_bytes",
    "max_snapshot_bytes",
    "max_total_steps",
    "max_prompt_input_bytes",
}
_STEP_FIELDS = {
    "type",
    "id",
    "lifecycle",
    "runner",
    "prompt",
    "skill",
    "inputs",
    "outputs",
    "depends_on",
}
_LOOP_FIELDS = {
    "type",
    "id",
    "source",
    "max_items",
    "controller",
    "body",
    "exports",
}
_SOURCE_FIELDS = {"from", "provider"}
_INPUT_FIELDS = {"name", "from"}
_OUTPUT_FIELDS = {"id", "kind", "path"}
_EXPORT_FIELDS = {"id", "from", "cardinality"}
_NODE_FIELDS = _STEP_FIELDS | _LOOP_FIELDS

# A closed schema already rejects these as unknown fields.  Keeping the lists
# makes the error message explicit and prevents future additions from
# accidentally turning policy or executable configuration into v1 syntax.
_POLICY_FIELDS = {
    "budget",
    "condition",
    "convergence",
    "failure",
    "human_gate",
    "max_retries",
    "optional",
    "policy",
    "policies",
    "retry",
    "route",
    "skip",
    "target_selection",
    "verdict",
    "when",
}
_EXECUTABLE_FIELDS = {
    "action",
    "actions",
    "callable",
    "command",
    "commands",
    "exec",
    "function",
    "hook",
    "hooks",
    "module",
    "post_action",
    "post_actions",
    "python",
    "shell",
}


class _IssueCollector:
    def __init__(self) -> None:
        self.items: list[WorkflowConfigDiagnostic] = []

    def add(
        self,
        code: str,
        path: str,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.items.append(
            WorkflowConfigDiagnostic(
                code=code,
                path=path,
                message=message,
                line=line,
                column=column,
            )
        )

    def raise_if_any(self, *, source_path: Path | None = None) -> None:
        if self.items:
            raise WorkflowConfigError(self.items, source_path=source_path)


def _format_diagnostic(diagnostic: WorkflowConfigDiagnostic) -> str:
    location = diagnostic.path or "$"
    if diagnostic.line is not None and diagnostic.column is not None:
        location = f"{location} (line {diagnostic.line}, column {diagnostic.column})"
    return f"{diagnostic.code} at {location}: {diagnostic.message}"


def _pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _join_pointer(path: str, part: str | int) -> str:
    return f"{path}/{_pointer_part(str(part))}" if path else f"/{_pointer_part(str(part))}"


def _object_pairs(pairs: list[tuple[str, object]]) -> _JSONObject:
    return _JSONObject(pairs)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value}")


def _scan_json_depth(raw: bytes) -> int:
    """Return maximum container nesting without interpreting string contents."""

    depth = 0
    maximum = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { or [
            depth += 1
            maximum = max(maximum, depth)
        elif byte in (0x7D, 0x5D) and depth:
            depth -= 1
    return maximum


def _read_bounded_bytes(path: Path, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise WorkflowConfigError(
            (
                WorkflowConfigDiagnostic(
                    code="config_read_error",
                    path="",
                    message="could not read workflow config",
                ),
            ),
            source_path=path,
        ) from exc
    if len(raw) > max_bytes:
        raise WorkflowConfigError(
            (
                WorkflowConfigDiagnostic(
                    code="config_too_large",
                    path="",
                    message=f"workflow config exceeds {max_bytes} bytes",
                ),
            ),
            source_path=path,
        )
    return raw


def _decode_json(raw: bytes, *, max_depth: int, source_path: Path | None) -> object:
    if len(raw) > MAX_WORKFLOW_CONFIG_BYTES:
        raise WorkflowConfigError(
            (
                WorkflowConfigDiagnostic(
                    code="config_too_large",
                    path="",
                    message=f"workflow config exceeds {MAX_WORKFLOW_CONFIG_BYTES} bytes",
                ),
            ),
            source_path=source_path,
        )
    if _scan_json_depth(raw) > max_depth:
        raise WorkflowConfigError(
            (
                WorkflowConfigDiagnostic(
                    code="json_depth_exceeded",
                    path="",
                    message=f"JSON nesting exceeds {max_depth} levels",
                ),
            ),
            source_path=source_path,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowConfigError(
            (
                WorkflowConfigDiagnostic(
                    code="invalid_utf8",
                    path="",
                    message="workflow config must be valid UTF-8",
                ),
            ),
            source_path=source_path,
        ) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise WorkflowConfigError(
            (
                WorkflowConfigDiagnostic(
                    code="invalid_json",
                    path="",
                    message=exc.msg,
                    line=exc.lineno,
                    column=exc.colno,
                ),
            ),
            source_path=source_path,
        ) from exc
    except RecursionError as exc:
        raise WorkflowConfigError(
            (
                WorkflowConfigDiagnostic(
                    code="json_depth_exceeded",
                    path="",
                    message=f"JSON nesting exceeds {max_depth} levels",
                ),
            ),
            source_path=source_path,
        ) from exc
    except ValueError as exc:
        raise WorkflowConfigError(
            (
                WorkflowConfigDiagnostic(
                    code="invalid_json",
                    path="",
                    message="invalid JSON value",
                ),
            ),
            source_path=source_path,
        ) from exc


def _expect_object(value: object, path: str, issues: _IssueCollector) -> dict[str, object] | None:
    if value is _MISSING:
        return None
    if not isinstance(value, dict):
        issues.add("wrong_type", path, "expected an object")
        return None
    return value


def _check_object_keys(
    value: Mapping[str, object],
    *,
    allowed: set[str],
    required: set[str],
    path: str,
    issues: _IssueCollector,
) -> None:
    for duplicate in getattr(value, "duplicate_keys", ()):
        issues.add(
            "duplicate_key",
            _join_pointer(path, duplicate),
            f"duplicate object key {duplicate!r}",
        )
    for key in value:
        if key not in allowed:
            if key in _POLICY_FIELDS:
                explanation = "policy fields are not supported by workflow schema v1"
            elif key in _EXECUTABLE_FIELDS:
                explanation = "executable fields are not supported by workflow schema v1"
            else:
                explanation = "field is not part of the closed workflow schema"
            issues.add(
                "unknown_field",
                _join_pointer(path, key),
                f"{key!r}: {explanation}",
            )
    for key in sorted(required - set(value)):
        issues.add("missing_field", _join_pointer(path, key), "required field is missing")


def _read_string(
    value: object,
    path: str,
    issues: _IssueCollector,
    *,
    field_name: str,
    identifier: bool = False,
    relative_path: bool = False,
) -> str | None:
    if value is _MISSING:
        return None
    if not isinstance(value, str):
        issues.add("wrong_type", path, f"{field_name} must be a string")
        return None
    if not value:
        issues.add("empty_string", path, f"{field_name} must not be empty")
        return None
    if len(value.encode("utf-8")) > MAX_WORKFLOW_STRING_BYTES:
        issues.add(
            "string_too_large",
            path,
            f"{field_name} exceeds {MAX_WORKFLOW_STRING_BYTES} UTF-8 bytes",
        )
        return None
    if identifier and SAFE_IDENTIFIER_PATTERN.fullmatch(value) is None:
        issues.add(
            "invalid_identifier",
            path,
            f"{field_name} must match {SAFE_IDENTIFIER_PATTERN.pattern}",
        )
        return None
    if relative_path and not _is_safe_relative_path(value):
        issues.add(
            "unsafe_path",
            path,
            f"{field_name} must be a safe repository-relative path",
        )
        return None
    return value


def _is_safe_relative_path(value: str) -> bool:
    if not value or "\\" in value or PurePosixPath(value).is_absolute():
        return False
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return False
    segments = value.split("/")
    return all(SAFE_PATH_SEGMENT_PATTERN.fullmatch(segment) for segment in segments)


def _read_positive_int(
    value: object,
    path: str,
    issues: _IssueCollector,
    *,
    field_name: str,
) -> int | None:
    if value is _MISSING:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        issues.add("wrong_type", path, f"{field_name} must be an integer")
        return None
    if value <= 0:
        issues.add("invalid_value", path, f"{field_name} must be greater than zero")
        return None
    return value


def _read_enum(
    value: object,
    path: str,
    issues: _IssueCollector,
    *,
    field_name: str,
    choices: set[str],
) -> str | None:
    if value is _MISSING:
        return None
    if not isinstance(value, str):
        issues.add("wrong_type", path, f"{field_name} must be a string")
        return None
    if value not in choices:
        choices_text = ", ".join(sorted(choices))
        issues.add(
            "unsupported_value",
            path,
            f"{field_name} must be one of: {choices_text}",
        )
        return None
    return value


def _read_list(value: object, path: str, issues: _IssueCollector, *, field_name: str) -> list[object]:
    if value is _MISSING:
        return []
    if not isinstance(value, list):
        issues.add("wrong_type", path, f"{field_name} must be an array")
        return []
    return value


def _parse_limits(value: object, path: str, issues: _IssueCollector) -> WorkflowLimits:
    raw = _expect_object(value, path, issues)
    if raw is None:
        return WorkflowLimits()
    _check_object_keys(raw, allowed=_LIMIT_FIELDS, required=set(), path=path, issues=issues)
    values: dict[str, int | None] = {}
    for field_name in sorted(_LIMIT_FIELDS):
        if field_name not in raw:
            values[field_name] = None
            continue
        values[field_name] = _read_positive_int(
            raw[field_name],
            _join_pointer(path, field_name),
            issues,
            field_name=field_name,
        )
    return WorkflowLimits(**values)


def _parse_input(value: object, path: str, issues: _IssueCollector) -> InputBinding | None:
    raw = _expect_object(value, path, issues)
    if raw is None:
        return None
    _check_object_keys(raw, allowed=_INPUT_FIELDS, required=_INPUT_FIELDS, path=path, issues=issues)
    name = _read_string(
        raw.get("name", _MISSING),
        _join_pointer(path, "name"),
        issues,
        field_name="input name",
        identifier=True,
    )
    source = _read_string(
        raw.get("from", _MISSING),
        _join_pointer(path, "from"),
        issues,
        field_name="input source",
    )
    if name is None or source is None:
        return None
    return InputBinding(name=name, source=source)


def _parse_inputs(value: object, path: str, issues: _IssueCollector) -> tuple[InputBinding, ...]:
    raw_items = _read_list(value, path, issues, field_name="inputs")
    parsed: list[InputBinding] = []
    for index, item in enumerate(raw_items):
        binding = _parse_input(item, _join_pointer(path, index), issues)
        if binding is not None:
            parsed.append(binding)
    return tuple(parsed)


def _parse_output(value: object, path: str, issues: _IssueCollector) -> OutputDeclaration | None:
    raw = _expect_object(value, path, issues)
    if raw is None:
        return None
    _check_object_keys(raw, allowed=_OUTPUT_FIELDS, required=_OUTPUT_FIELDS, path=path, issues=issues)
    output_id = _read_string(
        raw.get("id", _MISSING),
        _join_pointer(path, "id"),
        issues,
        field_name="output id",
        identifier=True,
    )
    kind = _read_enum(
        raw.get("kind", _MISSING),
        _join_pointer(path, "kind"),
        issues,
        field_name="output kind",
        choices={"file", "directory"},
    )
    output_path = _read_string(
        raw.get("path", _MISSING),
        _join_pointer(path, "path"),
        issues,
        field_name="output path",
        relative_path=True,
    )
    if output_id is None or kind is None or output_path is None:
        return None
    return OutputDeclaration(id=output_id, kind=kind, path=output_path)


def _parse_outputs(value: object, path: str, issues: _IssueCollector) -> tuple[OutputDeclaration, ...]:
    raw_items = _read_list(value, path, issues, field_name="outputs")
    parsed: list[OutputDeclaration] = []
    for index, item in enumerate(raw_items):
        output = _parse_output(item, _join_pointer(path, index), issues)
        if output is not None:
            parsed.append(output)
    return tuple(parsed)


def _parse_dependencies(value: object, path: str, issues: _IssueCollector) -> tuple[str, ...]:
    raw_items = _read_list(value, path, issues, field_name="depends_on")
    parsed: list[str] = []
    for index, item in enumerate(raw_items):
        dependency = _read_string(
            item,
            _join_pointer(path, index),
            issues,
            field_name="dependency id",
            identifier=True,
        )
        if dependency is not None:
            parsed.append(dependency)
    return tuple(parsed)


def _parse_step(value: object, path: str, issues: _IssueCollector) -> StepConfig | None:
    raw = _expect_object(value, path, issues)
    if raw is None:
        return None
    _check_object_keys(raw, allowed=_STEP_FIELDS, required=_STEP_FIELDS, path=path, issues=issues)
    node_type = _read_enum(
        raw.get("type", _MISSING),
        _join_pointer(path, "type"),
        issues,
        field_name="node type",
        choices={"step"},
    )
    node_id = _read_string(
        raw.get("id", _MISSING),
        _join_pointer(path, "id"),
        issues,
        field_name="step id",
        identifier=True,
    )
    lifecycle = _read_string(
        raw.get("lifecycle", _MISSING),
        _join_pointer(path, "lifecycle"),
        issues,
        field_name="lifecycle capability",
    )
    runner = _read_string(
        raw.get("runner", _MISSING),
        _join_pointer(path, "runner"),
        issues,
        field_name="runner capability",
    )
    prompt = _read_string(
        raw.get("prompt", _MISSING),
        _join_pointer(path, "prompt"),
        issues,
        field_name="prompt path",
        relative_path=True,
    )
    skill = _read_string(
        raw.get("skill", _MISSING),
        _join_pointer(path, "skill"),
        issues,
        field_name="skill path",
        relative_path=True,
    )
    inputs = _parse_inputs(raw.get("inputs", _MISSING), _join_pointer(path, "inputs"), issues)
    outputs = _parse_outputs(raw.get("outputs", _MISSING), _join_pointer(path, "outputs"), issues)
    dependencies = _parse_dependencies(
        raw.get("depends_on", _MISSING),
        _join_pointer(path, "depends_on"),
        issues,
    )
    if any(value is None for value in (node_type, node_id, lifecycle, runner, prompt, skill)):
        return None
    return StepConfig(
        type=node_type,
        id=node_id,
        lifecycle=lifecycle,
        runner=runner,
        prompt=prompt,
        skill=skill,
        inputs=inputs,
        outputs=outputs,
        depends_on=dependencies,
    )


def _parse_source(value: object, path: str, issues: _IssueCollector) -> LoopSource | None:
    raw = _expect_object(value, path, issues)
    if raw is None:
        return None
    _check_object_keys(raw, allowed=_SOURCE_FIELDS, required=_SOURCE_FIELDS, path=path, issues=issues)
    source = _read_string(
        raw.get("from", _MISSING),
        _join_pointer(path, "from"),
        issues,
        field_name="loop source",
    )
    provider = _read_string(
        raw.get("provider", _MISSING),
        _join_pointer(path, "provider"),
        issues,
        field_name="loop source provider",
    )
    if source is None or provider is None:
        return None
    return LoopSource(source=source, provider=provider)


def _parse_export(value: object, path: str, issues: _IssueCollector) -> CollectionExport | None:
    raw = _expect_object(value, path, issues)
    if raw is None:
        return None
    _check_object_keys(raw, allowed=_EXPORT_FIELDS, required=_EXPORT_FIELDS, path=path, issues=issues)
    export_id = _read_string(
        raw.get("id", _MISSING),
        _join_pointer(path, "id"),
        issues,
        field_name="export id",
        identifier=True,
    )
    source = _read_string(
        raw.get("from", _MISSING),
        _join_pointer(path, "from"),
        issues,
        field_name="export source",
    )
    cardinality = _read_enum(
        raw.get("cardinality", _MISSING),
        _join_pointer(path, "cardinality"),
        issues,
        field_name="export cardinality",
        choices={"collection"},
    )
    if export_id is None or source is None or cardinality is None:
        return None
    return CollectionExport(id=export_id, source=source, cardinality=cardinality)


def _parse_loop(value: object, path: str, issues: _IssueCollector) -> LoopConfig | None:
    raw = _expect_object(value, path, issues)
    if raw is None:
        return None
    _check_object_keys(raw, allowed=_LOOP_FIELDS, required=_LOOP_FIELDS, path=path, issues=issues)
    node_type = _read_enum(
        raw.get("type", _MISSING),
        _join_pointer(path, "type"),
        issues,
        field_name="node type",
        choices={"loop"},
    )
    node_id = _read_string(
        raw.get("id", _MISSING),
        _join_pointer(path, "id"),
        issues,
        field_name="loop id",
        identifier=True,
    )
    source = _parse_source(raw.get("source", _MISSING), _join_pointer(path, "source"), issues)
    max_items = _read_positive_int(
        raw.get("max_items", _MISSING),
        _join_pointer(path, "max_items"),
        issues,
        field_name="max_items",
    )
    controller = _read_string(
        raw.get("controller", _MISSING),
        _join_pointer(path, "controller"),
        issues,
        field_name="loop controller capability",
    )

    body_path = _join_pointer(path, "body")
    raw_body = _read_list(raw.get("body", _MISSING), body_path, issues, field_name="loop body")
    if not raw_body:
        issues.add("empty_loop_body", body_path, "loop body must contain at least one step")
    body: list[StepConfig] = []
    for index, item in enumerate(raw_body):
        item_path = _join_pointer(body_path, index)
        if isinstance(item, dict) and item.get("type") == "loop":
            issues.add(
                "nested_loop",
                _join_pointer(item_path, "type"),
                "nested loops are not supported by workflow schema v1",
            )
            continue
        parsed = _parse_step(item, item_path, issues)
        if parsed is not None:
            body.append(parsed)

    export_path = _join_pointer(path, "exports")
    raw_exports = _read_list(
        raw.get("exports", _MISSING),
        export_path,
        issues,
        field_name="loop exports",
    )
    exports: list[CollectionExport] = []
    for index, item in enumerate(raw_exports):
        parsed = _parse_export(item, _join_pointer(export_path, index), issues)
        if parsed is not None:
            exports.append(parsed)

    if any(value is None for value in (node_type, node_id, source, max_items, controller)):
        return None
    return LoopConfig(
        type=node_type,
        id=node_id,
        source=source,
        max_items=max_items,
        controller=controller,
        body=tuple(body),
        exports=tuple(exports),
    )


def _parse_node(value: object, path: str, issues: _IssueCollector, *, allow_loop: bool) -> WorkflowNode | None:
    raw = _expect_object(value, path, issues)
    if raw is None:
        return None
    node_type = raw.get("type", _MISSING)
    if node_type == "step":
        return _parse_step(raw, path, issues)
    if node_type == "loop":
        if not allow_loop:
            issues.add(
                "nested_loop",
                _join_pointer(path, "type"),
                "nested loops are not supported by workflow schema v1",
            )
            return None
        return _parse_loop(raw, path, issues)
    if node_type is _MISSING:
        issues.add("missing_field", _join_pointer(path, "type"), "required field is missing")
    elif not isinstance(node_type, str):
        issues.add("wrong_type", _join_pointer(path, "type"), "node type must be a string")
    else:
        issues.add("unsupported_node_type", _join_pointer(path, "type"), f"unsupported node type {node_type!r}")
    _check_object_keys(
        raw,
        allowed=_NODE_FIELDS,
        required=set(),
        path=path,
        issues=issues,
    )
    return None


def parse_workflow_config(
    payload: object,
    *,
    source_path: Path | str | None = None,
) -> WorkflowConfig:
    """Parse a decoded JSON value into an immutable workflow v1 DTO.

    This function does not perform file I/O.  Use ``load_workflow_config`` or
    ``WorkflowConfigLoader.load`` when the byte and UTF-8 boundary is needed.
    """

    issues = _IssueCollector()
    source_path_value = Path(source_path) if source_path is not None else None
    raw = _expect_object(payload, "", issues)
    if raw is None:
        issues.raise_if_any(source_path=source_path_value)
        raise AssertionError("unreachable")

    _check_object_keys(raw, allowed=_ROOT_FIELDS, required=_ROOT_FIELDS, path="", issues=issues)
    schema_version = _read_string(
        raw.get("schema_version", _MISSING),
        "/schema_version",
        issues,
        field_name="schema_version",
    )
    if schema_version is not None and schema_version not in SUPPORTED_WORKFLOW_SCHEMA_VERSIONS:
        issues.add(
            "unknown_schema_version",
            "/schema_version",
            f"unsupported workflow schema version {schema_version!r}",
        )
    workflow_id = _read_string(raw.get("id", _MISSING), "/id", issues, field_name="workflow id", identifier=True)
    profile = _read_string(raw.get("profile", _MISSING), "/profile", issues, field_name="workflow profile")
    limits = _parse_limits(raw.get("limits", _MISSING), "/limits", issues)

    nodes_path = "/nodes"
    raw_nodes = _read_list(raw.get("nodes", _MISSING), nodes_path, issues, field_name="nodes")
    if not raw_nodes:
        issues.add("empty_nodes", nodes_path, "workflow must contain at least one node")
    nodes: list[WorkflowNode] = []
    for index, item in enumerate(raw_nodes):
        parsed = _parse_node(item, _join_pointer(nodes_path, index), issues, allow_loop=True)
        if parsed is not None:
            nodes.append(parsed)

    issues.raise_if_any(source_path=source_path_value)
    assert schema_version is not None
    assert workflow_id is not None
    assert profile is not None
    return WorkflowConfig(
        schema_version=schema_version,
        id=workflow_id,
        profile=profile,
        limits=limits,
        nodes=tuple(nodes),
    )


class WorkflowConfigLoader:
    """Load bounded JSON bytes and parse the supported workflow version."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_WORKFLOW_CONFIG_BYTES,
        max_depth: int = MAX_WORKFLOW_JSON_DEPTH,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth <= 0:
            raise ValueError("max_depth must be a positive integer")
        if max_bytes > MAX_WORKFLOW_CONFIG_BYTES:
            raise ValueError("max_bytes cannot exceed the workflow loader hard limit")
        if max_depth > MAX_WORKFLOW_JSON_DEPTH:
            raise ValueError("max_depth cannot exceed the workflow loader hard limit")
        self.max_bytes = max_bytes
        self.max_depth = max_depth

    def load(self, path: Path | str) -> WorkflowConfig:
        source_path = Path(path)
        raw = _read_bounded_bytes(source_path, self.max_bytes)
        return self.loads(raw, source_path=source_path)

    def loads(self, source: bytes | bytearray | str, *, source_path: Path | str | None = None) -> WorkflowConfig:
        if isinstance(source, str):
            try:
                raw = source.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise WorkflowConfigError(
                    (
                        WorkflowConfigDiagnostic(
                            code="invalid_utf8",
                            path="",
                            message="workflow config text must be valid UTF-8",
                        ),
                    ),
                    source_path=Path(source_path) if source_path is not None else None,
                ) from exc
        elif isinstance(source, (bytes, bytearray)):
            raw = bytes(source)
        else:
            raise WorkflowConfigError(
                (
                    WorkflowConfigDiagnostic(
                        code="wrong_source_type",
                        path="",
                        message="workflow config source must be UTF-8 text or bytes",
                    ),
                ),
                source_path=Path(source_path) if source_path is not None else None,
            )
        source_path_value = Path(source_path) if source_path is not None else None
        if len(raw) > self.max_bytes:
            raise WorkflowConfigError(
                (
                    WorkflowConfigDiagnostic(
                        code="config_too_large",
                        path="",
                        message=f"workflow config exceeds {self.max_bytes} bytes",
                    ),
                ),
                source_path=source_path_value,
            )
        payload = _decode_json(raw, max_depth=self.max_depth, source_path=source_path_value)
        return parse_workflow_config(payload, source_path=source_path_value)

    def parse(self, payload: object) -> WorkflowConfig:
        return parse_workflow_config(payload)


def load_workflow_config(
    path: Path | str,
    *,
    max_bytes: int = MAX_WORKFLOW_CONFIG_BYTES,
    max_depth: int = MAX_WORKFLOW_JSON_DEPTH,
) -> WorkflowConfig:
    return WorkflowConfigLoader(max_bytes=max_bytes, max_depth=max_depth).load(path)


def load_workflow_config_text(
    source: bytes | bytearray | str,
    *,
    max_bytes: int = MAX_WORKFLOW_CONFIG_BYTES,
    max_depth: int = MAX_WORKFLOW_JSON_DEPTH,
    source_path: Path | str | None = None,
) -> WorkflowConfig:
    return WorkflowConfigLoader(max_bytes=max_bytes, max_depth=max_depth).loads(
        source,
        source_path=source_path,
    )


@dataclass(frozen=True, slots=True)
class CapabilityResourceLimits:
    """Resource limits advertised by a registered capability.

    These values are registry metadata, not workflow-controlled authority.
    A workflow may request a lower limit, but it cannot use its config to
    increase any value held here.
    """

    max_prompt_bytes: int | None = MAX_CAPABILITY_RESOURCE_BYTES
    max_skill_bytes: int | None = MAX_CAPABILITY_RESOURCE_BYTES
    max_input_bytes: int | None = MAX_CAPABILITY_RESOURCE_BYTES
    max_output_bytes: int | None = MAX_CAPABILITY_RESOURCE_BYTES

    def __post_init__(self) -> None:
        for field_name in (
            "max_prompt_bytes",
            "max_skill_bytes",
            "max_input_bytes",
            "max_output_bytes",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{field_name} must be a positive integer or None")

    @classmethod
    def from_mapping(cls, value: object) -> "CapabilityResourceLimits":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("resource_limits must be a mapping")
        allowed = {
            "max_prompt_bytes",
            "max_skill_bytes",
            "max_input_bytes",
            "max_output_bytes",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(
                "resource_limits has unsupported keys: " + ", ".join(sorted(unknown))
            )
        return cls(**{key: value[key] for key in allowed if key in value})

    def as_dict(self) -> dict[str, int | None]:
        return {
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_prompt_bytes": self.max_prompt_bytes,
            "max_skill_bytes": self.max_skill_bytes,
        }


def _normalise_capability_values(value: object, *, field_name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        values = (value,)
    else:
        try:
            values = tuple(value)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError(f"{field_name} must be a string or iterable of strings") from exc
    result: set[str] = set()
    for item in values:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field_name} must contain non-empty strings")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in item):
            raise ValueError(f"{field_name} contains a control character")
        result.add(item)
    return frozenset(result)


def _normalise_capability_id(value: object, *, field_name: str = "capability id") -> str:
    if not isinstance(value, str) or not value or CAPABILITY_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must match {CAPABILITY_ID_PATTERN.pattern}"
        )
    return value


def _normalise_resource_rules(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = (value,)
    else:
        try:
            values = tuple(value)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError(f"{field_name} must be a path or iterable of paths") from exc

    result: list[str] = []
    for raw_rule in values:
        if not isinstance(raw_rule, str) or not raw_rule:
            raise ValueError(f"{field_name} must contain non-empty paths")
        if raw_rule == "*":
            result.append(raw_rule)
            continue
        rule = raw_rule[:-1] if raw_rule.endswith("/") else raw_rule
        if not _is_safe_relative_path(rule):
            raise ValueError(f"{field_name} contains an unsafe repository-relative path")
        result.append(raw_rule)
    return tuple(dict.fromkeys(result))


def _freeze_metadata_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze_metadata_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_metadata_value(item) for item in value)
    return value


def _thaw_metadata_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_metadata_value(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return [_thaw_metadata_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, (tuple, list)):
        return [_thaw_metadata_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """Trusted, non-executable metadata for one named capability.

    In particular, this object intentionally has no command, callable, or
    action field.  Runner command resolution remains owned by
    ``run_issue_workflow.RunnerResolver`` and is connected through an adapter.
    """

    id: str
    kind: str
    allowed_profiles: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))
    allowed_prompt_paths: tuple[str, ...] = ()
    allowed_skill_paths: tuple[str, ...] = ()
    allowed_runners: frozenset[str] = frozenset()
    allowed_lifecycles: frozenset[str] = frozenset()
    allowed_controllers: frozenset[str] = frozenset()
    allowed_virtual_inputs: frozenset[str] = frozenset()
    external_send: bool = False
    resource_limits: CapabilityResourceLimits = field(default_factory=CapabilityResourceLimits)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _normalise_capability_id(self.id))
        if not isinstance(self.kind, str) or self.kind not in CAPABILITY_KINDS:
            raise ValueError(f"unsupported capability kind: {self.kind!r}")
        object.__setattr__(
            self,
            "allowed_profiles",
            _normalise_capability_values(self.allowed_profiles, field_name="allowed_profiles"),
        )
        object.__setattr__(
            self,
            "allowed_prompt_paths",
            _normalise_resource_rules(
                self.allowed_prompt_paths,
                field_name="allowed_prompt_paths",
            ),
        )
        object.__setattr__(
            self,
            "allowed_skill_paths",
            _normalise_resource_rules(
                self.allowed_skill_paths,
                field_name="allowed_skill_paths",
            ),
        )
        for field_name in (
            "allowed_runners",
            "allowed_lifecycles",
            "allowed_controllers",
            "allowed_virtual_inputs",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalise_capability_values(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        if not isinstance(self.external_send, bool):
            raise ValueError("external_send must be a boolean")
        object.__setattr__(
            self,
            "resource_limits",
            CapabilityResourceLimits.from_mapping(self.resource_limits),
        )
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        metadata: dict[str, object] = {}
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not key:
                raise ValueError("metadata keys must be non-empty strings")
            metadata[key] = _freeze_metadata_value(value)
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    @property
    def capability_id(self) -> str:
        return self.id

    @property
    def prompt_allowlist(self) -> tuple[str, ...]:
        return self.allowed_prompt_paths

    @property
    def skill_allowlist(self) -> tuple[str, ...]:
        return self.allowed_skill_paths

    @property
    def external_send_allowed(self) -> bool:
        return self.external_send

    def permits_profile(self, profile: str) -> bool:
        return "*" in self.allowed_profiles or profile in self.allowed_profiles

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed_controllers": sorted(self.allowed_controllers),
            "allowed_lifecycles": sorted(self.allowed_lifecycles),
            "allowed_profiles": sorted(self.allowed_profiles),
            "allowed_prompt_paths": list(self.allowed_prompt_paths),
            "allowed_runners": sorted(self.allowed_runners),
            "allowed_skill_paths": list(self.allowed_skill_paths),
            "allowed_virtual_inputs": sorted(self.allowed_virtual_inputs),
            "external_send": self.external_send,
            "id": self.id,
            "kind": self.kind,
            "metadata": _thaw_metadata_value(self.metadata),
            "resource_limits": self.resource_limits.as_dict(),
        }

    @classmethod
    def from_mapping(
        cls,
        capability_id: str,
        kind: str,
        value: object,
    ) -> "CapabilitySpec":
        if isinstance(value, cls):
            if value.id != capability_id or value.kind != kind:
                raise ValueError(
                    f"capability {capability_id!r} does not match its registry key"
                )
            return value
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ValueError(f"capability {capability_id!r} must be a mapping")

        allowed = {
            "allowed_controllers",
            "allowed_lifecycles",
            "allowed_profiles",
            "allowed_prompt_paths",
            "allowed_runners",
            "allowed_skill_paths",
            "allowed_virtual_inputs",
            "external_send",
            "id",
            "kind",
            "metadata",
            "profile_allowlist",
            "profiles",
            "prompt_allowlist",
            "prompt_paths",
            "resource_limits",
            "resources",
            "skill_allowlist",
            "skill_paths",
        }
        unknown = set(value) - allowed
        if unknown:
            executable = unknown & _EXECUTABLE_FIELDS
            if executable:
                raise ValueError(
                    "capability definitions cannot contain executable fields: "
                    + ", ".join(sorted(executable))
                )
            raise ValueError(
                f"capability {capability_id!r} has unsupported keys: "
                + ", ".join(sorted(unknown))
            )
        if "id" in value and value["id"] != capability_id:
            raise ValueError(f"capability id {value['id']!r} does not match its registry key")

        def first(*keys: str, default: object = None) -> object:
            for key in keys:
                if key in value:
                    return value[key]
            return default

        return cls(
            id=capability_id,
            kind=value.get("kind", kind),
            allowed_profiles=first(
                "allowed_profiles",
                "profile_allowlist",
                "profiles",
                default=("*",),
            ),
            allowed_prompt_paths=first(
                "allowed_prompt_paths",
                "prompt_allowlist",
                "prompt_paths",
                default=(),
            ),
            allowed_skill_paths=first(
                "allowed_skill_paths",
                "skill_allowlist",
                "skill_paths",
                default=(),
            ),
            allowed_runners=first("allowed_runners", default=()),
            allowed_lifecycles=first("allowed_lifecycles", default=()),
            allowed_controllers=first("allowed_controllers", default=()),
            allowed_virtual_inputs=first("allowed_virtual_inputs", default=()),
            external_send=first("external_send", default=False),  # type: ignore[arg-type]
            resource_limits=first("resource_limits", "resources", default=None),  # type: ignore[arg-type]
            metadata=first("metadata", default={}),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ResourceMetadata:
    """Digest and type information captured for an authorized resource."""

    path: str
    roles: frozenset[str]
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.roles:
            raise ValueError("resource metadata requires at least one role")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("resource size must be a non-negative integer")
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("resource sha256 must be lowercase hexadecimal")
        object.__setattr__(self, "roles", frozenset(self.roles))


@dataclass(frozen=True, slots=True)
class CapabilityUse:
    kind: str
    capability_id: str
    config_path: str


def _normalise_capability_entries(
    value: object,
    *,
    kind: str,
) -> dict[str, CapabilitySpec]:
    if value is None:
        return {}
    entries: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        # Also accept one inline capability object for callers constructing a
        # registry from a decoded document rather than a keyed mapping.
        if "id" in value and ("kind" in value or "allowed_profiles" in value):
            entries.append((str(value["id"]), value))
        else:
            entries.extend((str(key), raw) for key, raw in value.items())
    else:
        try:
            iterable = tuple(value)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError(f"{kind} capabilities must be a mapping or iterable") from exc
        for raw in iterable:
            if isinstance(raw, CapabilitySpec):
                entries.append((raw.id, raw))
            elif isinstance(raw, Mapping) and "id" in raw:
                entries.append((str(raw["id"]), raw))
            else:
                raise ValueError(f"{kind} capability entries must include an id")

    result: dict[str, CapabilitySpec] = {}
    for capability_id, raw in entries:
        if capability_id in result:
            raise ValueError(f"duplicate {kind} capability: {capability_id}")
        spec = CapabilitySpec.from_mapping(capability_id, kind, raw)
        if spec.id in result:
            raise ValueError(f"duplicate {kind} capability: {spec.id}")
        result[spec.id] = spec
    return result


class CapabilityRegistry:
    """Trusted registry definition that produces immutable profile snapshots."""

    def __init__(
        self,
        *,
        runners: object = None,
        lifecycles: object = None,
        controllers: object = None,
        virtual_inputs: object = None,
        loop_sources: object = None,
        source_providers: object = None,
        version: str = CAPABILITY_REGISTRY_VERSION,
        registry_version: str | None = None,
        runner_capabilities: object = None,
        lifecycle_capabilities: object = None,
        controller_capabilities: object = None,
        virtual_input_capabilities: object = None,
    ) -> None:
        if registry_version is not None:
            if version != CAPABILITY_REGISTRY_VERSION and version != registry_version:
                raise ValueError("version and registry_version disagree")
            version = registry_version
        if not isinstance(version, str) or not version:
            raise ValueError("capability registry version must be a non-empty string")

        def choose(primary: object, alias: object, field_name: str) -> object:
            if primary is not None and alias is not None:
                raise ValueError(f"provide only one of {field_name} and its capability alias")
            return primary if primary is not None else alias

        maps = {
            "runners": _normalise_capability_entries(
                choose(runners, runner_capabilities, "runners"), kind="runner"
            ),
            "lifecycles": _normalise_capability_entries(
                choose(lifecycles, lifecycle_capabilities, "lifecycles"), kind="lifecycle"
            ),
            "controllers": _normalise_capability_entries(
                choose(controllers, controller_capabilities, "controllers"), kind="loop_controller"
            ),
            "virtual_inputs": _normalise_capability_entries(
                choose(virtual_inputs, virtual_input_capabilities, "virtual_inputs"),
                kind="virtual_input",
            ),
            "loop_sources": _normalise_capability_entries(
                choose(loop_sources, source_providers, "loop_sources"), kind="loop_source"
            ),
        }
        self.version = version
        for name, entries in maps.items():
            setattr(self, f"_{name}", MappingProxyType(entries))
        self._digest = _capability_registry_digest(version, maps)

    @classmethod
    def from_mapping(cls, value: object) -> "CapabilityRegistry":
        if not isinstance(value, Mapping):
            raise ValueError("capability registry must be a mapping")
        allowed = {
            "controllers",
            "lifecycle_capabilities",
            "lifecycles",
            "loop_sources",
            "registry_version",
            "runners",
            "runner_capabilities",
            "source_providers",
            "version",
            "virtual_input_capabilities",
            "virtual_inputs",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("capability registry has unsupported keys: " + ", ".join(sorted(unknown)))
        return cls(
            runners=value.get("runners", value.get("runner_capabilities")),
            lifecycles=value.get("lifecycles", value.get("lifecycle_capabilities")),
            controllers=value.get("controllers"),
            virtual_inputs=value.get("virtual_inputs", value.get("virtual_input_capabilities")),
            loop_sources=value.get("loop_sources", value.get("source_providers")),
            version=value.get("version", CAPABILITY_REGISTRY_VERSION),
            registry_version=value.get("registry_version"),
        )

    @classmethod
    def default(cls) -> "CapabilityRegistry":
        """Return the built-in non-executable registry for the repository profile."""

        runner_names = ("codex", "opencode", "copilot", "gemini", "hybrid_cli")
        runners = {
            name: CapabilitySpec(
                id=name,
                kind="runner",
                allowed_profiles=(DEFAULT_CAPABILITY_PROFILE,),
                allowed_prompt_paths=("prompts/",),
                allowed_skill_paths=("skills/",),
                resource_limits=CapabilityResourceLimits(
                    max_prompt_bytes=MAX_CAPABILITY_RESOURCE_BYTES,
                    max_skill_bytes=MAX_CAPABILITY_RESOURCE_BYTES,
                    max_input_bytes=MAX_CAPABILITY_RESOURCE_BYTES,
                    max_output_bytes=MAX_CAPABILITY_RESOURCE_BYTES,
                ),
            )
            for name in runner_names
        }
        phase_names = (
            "plan",
            "prototype_planning",
            "prototyping",
            "red_team_review",
            "solution_design",
            "work_breakdown",
            "plan_comprehension_check",
            "implementation",
            "review_fix_loop",
            "pull_request",
            "implementation_coder",
            "implementation_reviewer",
            "implementation_fix",
        )
        lifecycles = {
            f"kelpie.phase.{name}.v1": CapabilitySpec(
                id=f"kelpie.phase.{name}.v1",
                kind="lifecycle",
                allowed_profiles=(DEFAULT_CAPABILITY_PROFILE,),
                allowed_runners=runner_names,
                allowed_prompt_paths=("prompts/",),
                allowed_skill_paths=("skills/",),
            )
            for name in phase_names
        }
        return cls(
            runners=runners,
            lifecycles=lifecycles,
            controllers={
                "fixed_sequence.v1": CapabilitySpec(
                    id="fixed_sequence.v1",
                    kind="loop_controller",
                    allowed_profiles=(DEFAULT_CAPABILITY_PROFILE,),
                ),
                "implementation_review_v1": CapabilitySpec(
                    id="implementation_review_v1",
                    kind="loop_controller",
                    allowed_profiles=(DEFAULT_CAPABILITY_PROFILE,),
                ),
            },
            virtual_inputs={
                token: CapabilitySpec(
                    id=token,
                    kind="virtual_input",
                    allowed_profiles=(DEFAULT_CAPABILITY_PROFILE,),
                    allowed_runners=runner_names,
                    resource_limits=CapabilityResourceLimits(
                        max_input_bytes=MAX_CAPABILITY_RESOURCE_BYTES
                    ),
                )
                for token in (
                    "$issue",
                    "$repo_instructions",
                    "$loop_item",
                    "$review_findings",
                )
            },
            loop_sources={
                "kelpie.work_items.v1": CapabilitySpec(
                    id="kelpie.work_items.v1",
                    kind="loop_source",
                    allowed_profiles=(DEFAULT_CAPABILITY_PROFILE,),
                )
            },
        )

    @property
    def runners(self) -> Mapping[str, CapabilitySpec]:
        return self._runners

    @property
    def lifecycles(self) -> Mapping[str, CapabilitySpec]:
        return self._lifecycles

    @property
    def controllers(self) -> Mapping[str, CapabilitySpec]:
        return self._controllers

    @property
    def virtual_inputs(self) -> Mapping[str, CapabilitySpec]:
        return self._virtual_inputs

    @property
    def loop_sources(self) -> Mapping[str, CapabilitySpec]:
        return self._loop_sources

    @property
    def source_providers(self) -> Mapping[str, CapabilitySpec]:
        return self.loop_sources

    @property
    def registry_digest(self) -> str:
        return self._digest

    def with_runner_ids(self, runner_ids: Iterable[str]) -> "CapabilityRegistry":
        runners = dict(self.runners)
        for runner_id in runner_ids:
            if runner_id not in runners:
                runners[runner_id] = CapabilitySpec(
                    id=runner_id,
                    kind="runner",
                    allowed_profiles=(DEFAULT_CAPABILITY_PROFILE,),
                    allowed_prompt_paths=("prompts/",),
                    allowed_skill_paths=("skills/",),
                )
        return CapabilityRegistry(
            runners=runners,
            lifecycles=self.lifecycles,
            controllers=self.controllers,
            virtual_inputs=self.virtual_inputs,
            loop_sources=self.loop_sources,
            version=self.version,
        )

    def snapshot(self, profile: str) -> "CapabilityRegistrySnapshot":
        if not isinstance(profile, str) or not profile:
            raise ValueError("workflow profile must be a non-empty string")
        return CapabilityRegistrySnapshot(
            registry_version=self.version,
            profile=profile,
            runners=self.runners,
            lifecycles=self.lifecycles,
            controllers=self.controllers,
            virtual_inputs=self.virtual_inputs,
            loop_sources=self.loop_sources,
            registry_digest=self.registry_digest,
        )

    snapshot_for_profile = snapshot

    def authorize_step(
        self,
        step: StepConfig,
        *,
        profile: str,
        repo_root: Path | str,
        config_path: str = "/step",
    ) -> "CapabilityAuthorizationResult":
        return self.snapshot(profile).authorize_step(
            step,
            repo_root=repo_root,
            config_path=config_path,
        )

    def authorize_workflow(
        self,
        config: WorkflowConfig,
        *,
        repo_root: Path | str,
    ) -> "CapabilityAuthorizationResult":
        return validate_workflow_capabilities(config, self, repo_root=repo_root)

    validate_workflow = authorize_workflow


def _capability_registry_digest(version: str, maps: Mapping[str, Mapping[str, CapabilitySpec]]) -> str:
    payload = {
        "registry_version": version,
        "capabilities": {
            kind: [maps[kind][key].as_dict() for key in sorted(maps[kind])]
            for kind in sorted(maps)
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=repr)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityRegistrySnapshot:
    """Read-only registry view bound to one workflow profile."""

    registry_version: str
    profile: str
    runners: Mapping[str, CapabilitySpec]
    lifecycles: Mapping[str, CapabilitySpec]
    controllers: Mapping[str, CapabilitySpec]
    virtual_inputs: Mapping[str, CapabilitySpec]
    loop_sources: Mapping[str, CapabilitySpec]
    registry_digest: str
    resource_metadata: Mapping[str, ResourceMetadata] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "runners",
            "lifecycles",
            "controllers",
            "virtual_inputs",
            "loop_sources",
        ):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, MappingProxyType(dict(value)))
        object.__setattr__(self, "resource_metadata", MappingProxyType(dict(self.resource_metadata)))

    @property
    def version(self) -> str:
        return self.registry_version

    @property
    def source_providers(self) -> Mapping[str, CapabilitySpec]:
        return self.loop_sources

    @property
    def resource_digests(self) -> Mapping[str, str]:
        return MappingProxyType(
            {path: metadata.sha256 for path, metadata in self.resource_metadata.items()}
        )

    def capabilities_for_kind(self, kind: str) -> Mapping[str, CapabilitySpec]:
        maps: dict[str, Mapping[str, CapabilitySpec]] = {
            "runner": self.runners,
            "lifecycle": self.lifecycles,
            "loop_controller": self.controllers,
            "virtual_input": self.virtual_inputs,
            "loop_source": self.loop_sources,
        }
        try:
            return maps[kind]
        except KeyError as exc:
            raise ValueError(f"unsupported capability kind: {kind!r}") from exc

    def lookup(self, kind: str, capability_id: str) -> CapabilitySpec | None:
        return self.capabilities_for_kind(kind).get(capability_id)

    get = lookup

    def with_resource_metadata(
        self,
        resource_metadata: Mapping[str, ResourceMetadata],
    ) -> "CapabilityRegistrySnapshot":
        return CapabilityRegistrySnapshot(
            registry_version=self.registry_version,
            profile=self.profile,
            runners=self.runners,
            lifecycles=self.lifecycles,
            controllers=self.controllers,
            virtual_inputs=self.virtual_inputs,
            loop_sources=self.loop_sources,
            registry_digest=self.registry_digest,
            resource_metadata=resource_metadata,
        )

    def authorize_workflow(
        self,
        config: WorkflowConfig,
        *,
        repo_root: Path | str,
    ) -> "CapabilityAuthorizationResult":
        return validate_workflow_capabilities(config, self, repo_root=repo_root)

    validate_workflow = authorize_workflow
    validate = authorize_workflow

    def authorize_step(
        self,
        step: StepConfig,
        *,
        repo_root: Path | str,
        config_path: str = "/step",
    ) -> "CapabilityAuthorizationResult":
        if not isinstance(step, StepConfig):
            raise TypeError("step must be a StepConfig")
        issues = _IssueCollector()
        records: dict[str, ResourceMetadata] = {}
        uses: list[CapabilityUse] = []
        _validate_step_capabilities(
            step,
            config_path=config_path,
            snapshot=self,
            repo_root=Path(repo_root),
            records=records,
            issues=issues,
            uses=uses,
        )
        issues.raise_if_any()
        return CapabilityAuthorizationResult(
            snapshot=self.with_resource_metadata(records),
            uses=tuple(uses),
        )


@dataclass(frozen=True, slots=True)
class CapabilityAuthorizationResult:
    snapshot: CapabilityRegistrySnapshot
    uses: tuple[CapabilityUse, ...]

    @property
    def resource_metadata(self) -> Mapping[str, ResourceMetadata]:
        return self.snapshot.resource_metadata

    @property
    def resource_digests(self) -> Mapping[str, str]:
        return self.snapshot.resource_digests

    @property
    def external_send_capabilities(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    use.capability_id
                    for use in self.uses
                    if (
                        (spec := self.snapshot.lookup(use.kind, use.capability_id)) is not None
                        and spec.external_send
                    )
                }
            )
        )


CapabilityValidationResult = CapabilityAuthorizationResult
AuthorizationResult = CapabilityAuthorizationResult
RegistrySnapshot = CapabilityRegistrySnapshot
Capability = CapabilitySpec
RunnerCapability = CapabilitySpec
LifecycleCapability = CapabilitySpec
LoopControllerCapability = CapabilitySpec
VirtualInputCapability = CapabilitySpec
LoopSourceCapability = CapabilitySpec
CapabilityAuthorizationError = WorkflowConfigError
CapabilityValidationError = WorkflowConfigError


def _capability_relation_allows(allowed: frozenset[str], target: str) -> bool:
    return not allowed or "*" in allowed or target in allowed


def _resource_rule_matches(path: str, rule: str) -> bool:
    if rule == "*":
        return True
    if rule.endswith("/"):
        return path.startswith(rule)
    return path == rule


def _resource_allowed(path: str, specs: Iterable[CapabilitySpec], attribute: str) -> bool:
    for spec in specs:
        rules = getattr(spec, attribute)
        if rules and not any(_resource_rule_matches(path, rule) for rule in rules):
            return False
    return True


def _path_has_symlink(root: Path, path: Path) -> bool:
    if root.is_symlink():
        return True
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            return True
    return False


def _resource_limits_for(
    specs: Iterable[CapabilitySpec],
    field_name: str,
) -> int:
    values = [
        value
        for spec in specs
        if (value := getattr(spec.resource_limits, field_name)) is not None
    ]
    return min([MAX_CAPABILITY_RESOURCE_BYTES, *values])


def _record_resource(
    records: dict[str, ResourceMetadata],
    *,
    path: str,
    role: str,
    raw: bytes,
) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    previous = records.get(path)
    roles = (previous.roles if previous is not None else frozenset()) | {role}
    records[path] = ResourceMetadata(
        path=path,
        roles=roles,
        size_bytes=len(raw),
        sha256=digest,
    )


def _validate_authorized_resource(
    relative_path: str,
    *,
    role: str,
    config_path: str,
    specs: tuple[CapabilitySpec, ...],
    repo_root: Path,
    records: dict[str, ResourceMetadata],
    issues: _IssueCollector,
) -> None:
    root = Path(repo_root)
    if not _is_safe_relative_path(relative_path) or PurePosixPath(relative_path).suffix.lower() != ".md":
        issues.add(
            "unsafe_path",
            config_path,
            "resource must be a safe repository-relative Markdown path",
        )
        return
    allowlist_attribute = "allowed_prompt_paths" if role == "prompt" else "allowed_skill_paths"
    if not _resource_allowed(relative_path, specs, allowlist_attribute):
        issues.add(
            "unauthorized_capability",
            config_path,
            f"{role} resource is not permitted by the selected capability",
        )
        return

    try:
        canonical_root = repo_root.resolve(strict=True)
    except (OSError, RuntimeError):
        issues.add(
            "resource_root_unavailable",
            config_path,
            "repository root could not be resolved",
        )
        return
    if not canonical_root.is_dir() or root.is_symlink():
        issues.add(
            "unsafe_path",
            config_path,
            "repository root must be a real directory without symlink components",
        )
        return

    # Keep the caller's root spelling for component checks.  On macOS the
    # system temp path commonly contains /var -> /private/var; checking every
    # ancestor after resolving would reject an otherwise safe temporary repo.
    # Containment is still checked against the canonical root below, while
    # _path_has_symlink rejects the root itself and any resource descendant.
    candidate = root.joinpath(*relative_path.split("/"))
    try:
        canonical_candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        issues.add("unsafe_path", config_path, "resource path could not be resolved safely")
        return
    try:
        canonical_candidate.relative_to(canonical_root)
    except ValueError:
        issues.add("unsafe_path", config_path, "resource path escapes repository root")
        return
    if _path_has_symlink(root, candidate):
        issues.add("unsafe_path", config_path, "resource path contains a symlink component")
        return
    if not candidate.exists():
        issues.add("resource_not_found", config_path, "authorized resource does not exist")
        return

    max_bytes = _resource_limits_for(specs, "max_prompt_bytes" if role == "prompt" else "max_skill_bytes")
    try:
        before = os.lstat(candidate)
        if not stat.S_ISREG(before.st_mode):
            issues.add("invalid_resource", config_path, "authorized resource must be a regular file")
            return
        with candidate.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                issues.add("invalid_resource", config_path, "authorized resource must be a regular file")
                return
            raw = stream.read(max_bytes + 1)
        after = os.lstat(candidate)
    except OSError:
        issues.add("resource_read_error", config_path, "authorized resource could not be read")
        return
    if len(raw) > max_bytes:
        issues.add(
            "resource_limit_exceeded",
            config_path,
            f"{role} resource exceeds the registered byte limit",
        )
        return
    if (
        before.st_dev != opened.st_dev
        or before.st_ino != opened.st_ino
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or _path_has_symlink(root, candidate)
    ):
        issues.add("unsafe_path", config_path, "resource changed or became symlinked while reading")
        return
    _record_resource(records, path=relative_path, role=role, raw=raw)


def _authorize_capability(
    snapshot: CapabilityRegistrySnapshot,
    *,
    kind: str,
    capability_id: str,
    config_path: str,
    issues: _IssueCollector,
    uses: list[CapabilityUse],
) -> CapabilitySpec | None:
    spec = snapshot.lookup(kind, capability_id)
    if spec is None:
        issues.add(
            "unknown_capability",
            config_path,
            f"unknown {kind} capability {capability_id!r}",
        )
        return None
    if not spec.permits_profile(snapshot.profile):
        issues.add(
            "unauthorized_capability",
            config_path,
            f"{kind} capability is not authorized for profile {snapshot.profile!r}",
        )
        return None
    uses.append(CapabilityUse(kind=kind, capability_id=capability_id, config_path=config_path))
    return spec


def _validate_capability_pair(
    source: CapabilitySpec | None,
    target: CapabilitySpec | None,
    *,
    source_path: str,
    target_path: str,
    allowed_field: str,
    issues: _IssueCollector,
) -> None:
    if source is None or target is None:
        return
    allowed = getattr(source, allowed_field)
    if not _capability_relation_allows(allowed, target.id):
        issues.add(
            "unauthorized_capability",
            source_path,
            f"capability {source.id!r} does not authorize {target.id!r}",
        )


def _validate_step_capabilities(
    step: StepConfig,
    *,
    config_path: str,
    snapshot: CapabilityRegistrySnapshot,
    repo_root: Path,
    records: dict[str, ResourceMetadata],
    issues: _IssueCollector,
    uses: list[CapabilityUse],
) -> tuple[CapabilitySpec | None, CapabilitySpec | None]:
    runner = _authorize_capability(
        snapshot,
        kind="runner",
        capability_id=step.runner,
        config_path=f"{config_path}/runner",
        issues=issues,
        uses=uses,
    )
    lifecycle = _authorize_capability(
        snapshot,
        kind="lifecycle",
        capability_id=step.lifecycle,
        config_path=f"{config_path}/lifecycle",
        issues=issues,
        uses=uses,
    )
    _validate_capability_pair(
        runner,
        lifecycle,
        source_path=f"{config_path}/runner",
        target_path=f"{config_path}/lifecycle",
        allowed_field="allowed_lifecycles",
        issues=issues,
    )
    _validate_capability_pair(
        lifecycle,
        runner,
        source_path=f"{config_path}/lifecycle",
        target_path=f"{config_path}/runner",
        allowed_field="allowed_runners",
        issues=issues,
    )

    resource_specs = tuple(spec for spec in (runner, lifecycle) if spec is not None)
    _validate_authorized_resource(
        step.prompt,
        role="prompt",
        config_path=f"{config_path}/prompt",
        specs=resource_specs,
        repo_root=repo_root,
        records=records,
        issues=issues,
    )
    _validate_authorized_resource(
        step.skill,
        role="skill",
        config_path=f"{config_path}/skill",
        specs=resource_specs,
        repo_root=repo_root,
        records=records,
        issues=issues,
    )

    for index, binding in enumerate(step.inputs):
        if not binding.source.startswith("$"):
            continue
        virtual = _authorize_capability(
            snapshot,
            kind="virtual_input",
            capability_id=binding.source,
            config_path=f"{config_path}/inputs/{index}/from",
            issues=issues,
            uses=uses,
        )
        if virtual is not None and runner is not None and not _capability_relation_allows(
            virtual.allowed_runners,
            runner.id,
        ):
            issues.add(
                "unauthorized_capability",
                f"{config_path}/inputs/{index}/from",
                f"virtual input is not authorized for runner {runner.id!r}",
            )
    return runner, lifecycle


def validate_workflow_capabilities(
    config: WorkflowConfig,
    registry: CapabilityRegistry | CapabilityRegistrySnapshot | None = None,
    *,
    repo_root: Path | str,
) -> CapabilityAuthorizationResult:
    """Authorize all named capabilities and resource references in a config.

    This is a side-effect-free preflight operation: it reads prompt/skill
    bytes to calculate digests, but it does not create artifacts, locks, or
    invoke any runner.
    """

    if not isinstance(config, WorkflowConfig):
        raise TypeError("config must be a WorkflowConfig")
    if registry is None:
        registry = CapabilityRegistry.default()
    snapshot = registry.snapshot(config.profile) if isinstance(registry, CapabilityRegistry) else registry
    if snapshot.profile != config.profile:
        raise ValueError(
            "capability registry snapshot profile does not match workflow profile"
        )
    root = Path(repo_root)
    issues = _IssueCollector()
    records: dict[str, ResourceMetadata] = {}
    uses: list[CapabilityUse] = []

    for node_index, node in enumerate(config.nodes):
        node_path = f"/nodes/{node_index}"
        if isinstance(node, StepConfig):
            _validate_step_capabilities(
                node,
                config_path=node_path,
                snapshot=snapshot,
                repo_root=root,
                records=records,
                issues=issues,
                uses=uses,
            )
            continue

        controller = _authorize_capability(
            snapshot,
            kind="loop_controller",
            capability_id=node.controller,
            config_path=f"{node_path}/controller",
            issues=issues,
            uses=uses,
        )
        _authorize_capability(
            snapshot,
            kind="loop_source",
            capability_id=node.source.provider,
            config_path=f"{node_path}/source/provider",
            issues=issues,
            uses=uses,
        )
        body_specs: list[tuple[CapabilitySpec | None, CapabilitySpec | None]] = []
        for body_index, step in enumerate(node.body):
            body_specs.append(
                _validate_step_capabilities(
                    step,
                    config_path=f"{node_path}/body/{body_index}",
                    snapshot=snapshot,
                    repo_root=root,
                    records=records,
                    issues=issues,
                    uses=uses,
                )
            )
        if controller is not None:
            for body_index, (runner, lifecycle) in enumerate(body_specs):
                if lifecycle is not None and not _capability_relation_allows(
                    controller.allowed_lifecycles,
                    lifecycle.id,
                ):
                    issues.add(
                        "unauthorized_capability",
                        f"{node_path}/body/{body_index}/lifecycle",
                        f"loop controller {controller.id!r} does not authorize this lifecycle",
                    )
                if runner is not None and not _capability_relation_allows(
                    controller.allowed_runners,
                    runner.id,
                ):
                    issues.add(
                        "unauthorized_capability",
                        f"{node_path}/body/{body_index}/runner",
                        f"loop controller {controller.id!r} does not authorize this runner",
                    )

    issues.raise_if_any()
    authorized_snapshot = snapshot.with_resource_metadata(records)
    return CapabilityAuthorizationResult(
        snapshot=authorized_snapshot,
        uses=tuple(uses),
    )


authorize_workflow_capabilities = validate_workflow_capabilities
validate_capabilities = validate_workflow_capabilities


ARTIFACT_SCOPES = frozenset({"workflow", "loop_item"})
ARTIFACT_CARDINALITIES = frozenset({"scalar", "collection"})


@dataclass(frozen=True, slots=True)
class ArtifactKey:
    """Typed identity for one logical workflow artifact.

    ``producer_node_id`` is always a canonical node id (for example
    ``nodes/build`` or ``nodes/implementation/body/coder``).  The physical
    path is deliberately not part of this identity; later artifact handling
    stages map this logical key to a checked namespace.
    """

    producer_node_id: str
    output_id: str
    scope: Literal["workflow", "loop_item"]
    cardinality: Literal["scalar", "collection"]

    def __post_init__(self) -> None:
        if not isinstance(self.producer_node_id, str) or not self.producer_node_id:
            raise ValueError("artifact producer node id must be a non-empty string")
        if (
            not isinstance(self.output_id, str)
            or SAFE_IDENTIFIER_PATTERN.fullmatch(self.output_id) is None
        ):
            raise ValueError("artifact output id is not a safe identifier")
        if self.scope not in ARTIFACT_SCOPES:
            raise ValueError(f"unsupported artifact scope: {self.scope!r}")
        if self.cardinality not in ARTIFACT_CARDINALITIES:
            raise ValueError(f"unsupported artifact cardinality: {self.cardinality!r}")

    @property
    def producer(self) -> str:
        return self.producer_node_id

    @property
    def id(self) -> str:
        return self.output_id

    @property
    def reference(self) -> str:
        prefix = "item-artifact" if self.scope == "loop_item" else "artifact"
        return f"{prefix}:{self.producer_node_id}.{self.output_id}"

    @property
    def canonical_ref(self) -> str:
        return self.reference


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """A validated input/source reference and its typed artifact key."""

    source: str
    key: ArtifactKey
    expected_cardinality: str | None = None

    @property
    def artifact(self) -> ArtifactKey:
        return self.key

    @property
    def producer_node_id(self) -> str:
        return self.key.producer_node_id

    @property
    def output_id(self) -> str:
        return self.key.output_id

    @property
    def scope(self) -> str:
        return self.key.scope

    @property
    def cardinality(self) -> str:
        return self.key.cardinality

    @property
    def canonical_source(self) -> str:
        return self.key.canonical_ref

    @property
    def reference(self) -> str:
        return self.canonical_source


@dataclass(frozen=True, slots=True)
class InputBindingPlan:
    """Normalized input binding with either a virtual or artifact source."""

    name: str
    source: str
    artifact: ArtifactReference | None = None
    virtual_input: str | None = None

    @property
    def from_(self) -> str:
        return self.source

    @property
    def from_value(self) -> str:
        return self.source

    @property
    def artifact_key(self) -> ArtifactKey | None:
        return self.artifact.key if self.artifact is not None else None

    @property
    def reference(self) -> ArtifactReference | None:
        return self.artifact

    @property
    def source_kind(self) -> Literal["virtual", "artifact"]:
        return "virtual" if self.virtual_input is not None else "artifact"

    @property
    def cardinality(self) -> str | None:
        return self.artifact.cardinality if self.artifact is not None else None


@dataclass(frozen=True, slots=True)
class OutputPlan:
    """Normalized output declaration and its typed logical artifact key."""

    id: str
    kind: str
    path: str
    artifact_key: ArtifactKey

    @property
    def key(self) -> ArtifactKey:
        return self.artifact_key

    @property
    def artifact(self) -> ArtifactKey:
        return self.artifact_key

    @property
    def scope(self) -> str:
        return self.artifact_key.scope

    @property
    def cardinality(self) -> str:
        return self.artifact_key.cardinality

    @property
    def reference(self) -> str:
        return self.artifact_key.reference


@dataclass(frozen=True, slots=True)
class LoopSourceBinding:
    """Normalized loop source retaining its provider and typed reference."""

    source: str
    provider: str
    artifact: ArtifactReference | None = None
    virtual_input: str | None = None

    @property
    def from_(self) -> str:
        return self.source

    @property
    def from_value(self) -> str:
        return self.source

    @property
    def reference(self) -> ArtifactReference | None:
        return self.artifact

    @property
    def artifact_key(self) -> ArtifactKey | None:
        return self.artifact.key if self.artifact is not None else None

    @property
    def canonical_source(self) -> str | None:
        return self.artifact.canonical_source if self.artifact is not None else None


@dataclass(frozen=True, slots=True)
class CollectionExportPlan:
    """Explicit promotion of an item-scoped output to a collection."""

    id: str
    source: str
    cardinality: Literal["collection"]
    source_artifact: ArtifactKey
    artifact_key: ArtifactKey

    @property
    def from_(self) -> str:
        return self.source

    @property
    def from_value(self) -> str:
        return self.source

    @property
    def source_key(self) -> ArtifactKey:
        return self.source_artifact

    @property
    def canonical_source(self) -> str:
        return self.source_artifact.reference

    @property
    def key(self) -> ArtifactKey:
        return self.artifact_key

    @property
    def artifact(self) -> ArtifactKey:
        return self.artifact_key


@dataclass(frozen=True, slots=True)
class StepPlan:
    """Immutable normalized contract shared by top-level and loop-body steps."""

    canonical_id: str
    local_id: str
    lifecycle: str
    runner: str
    prompt: str
    skill: str
    inputs: tuple[InputBindingPlan, ...]
    outputs: tuple[OutputPlan, ...]
    dependencies: tuple[str, ...]
    explicit_dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "explicit_dependencies", tuple(self.explicit_dependencies))

    @property
    def id(self) -> str:
        return self.local_id

    @property
    def node_id(self) -> str:
        return self.canonical_id

    @property
    def type(self) -> Literal["step"]:
        return "step"

    @property
    def depends_on(self) -> tuple[str, ...]:
        return self.dependencies

    @property
    def input_bindings(self) -> tuple[InputBindingPlan, ...]:
        return self.inputs

    @property
    def output_declarations(self) -> tuple[OutputPlan, ...]:
        return self.outputs

    def output(self, output_id: str) -> OutputPlan | None:
        return next((item for item in self.outputs if item.id == output_id), None)


@dataclass(frozen=True, slots=True)
class LoopPlan:
    """Immutable normalized single-level loop container."""

    canonical_id: str
    local_id: str
    source: LoopSourceBinding
    max_items: int
    controller: str
    body: tuple[StepPlan, ...]
    exports: tuple[CollectionExportPlan, ...]
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", tuple(self.body))
        object.__setattr__(self, "exports", tuple(self.exports))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))

    @property
    def id(self) -> str:
        return self.local_id

    @property
    def node_id(self) -> str:
        return self.canonical_id

    @property
    def type(self) -> Literal["loop"]:
        return "loop"

    @property
    def depends_on(self) -> tuple[str, ...]:
        return self.dependencies

    @property
    def steps(self) -> tuple[StepPlan, ...]:
        return self.body


PlanNode = StepPlan | LoopPlan


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    """Validated, immutable workflow IR consumed by later execution stages."""

    schema_version: str
    workflow_id: str
    profile: str
    limits: WorkflowLimits
    nodes: tuple[PlanNode, ...]
    dependency_graph: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    artifact_graph: Mapping[str, ArtifactKey] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(
            self,
            "dependency_graph",
            MappingProxyType(
                {key: tuple(value) for key, value in self.dependency_graph.items()}
            ),
        )
        object.__setattr__(
            self,
            "artifact_graph",
            MappingProxyType(dict(self.artifact_graph)),
        )

    @property
    def id(self) -> str:
        return self.workflow_id

    @property
    def pipeline(self) -> tuple[PlanNode, ...]:
        return self.nodes

    @property
    def execution_order(self) -> tuple[str, ...]:
        """Return declaration order; dependencies never reorder this tuple."""

        return tuple(node.canonical_id for node in self.nodes)

    @property
    def artifacts(self) -> tuple[ArtifactKey, ...]:
        return tuple(dict.fromkeys(self.artifact_graph.values()))

    @property
    def workflow_digest(self) -> str:
        """Digest of the canonical normalized workflow definition.

        The implementation is defined below the IR classes so the same
        canonical representation can also be used by the run-identity
        builder.  Keeping this as a property avoids storing a digest that can
        become stale if a caller constructs an IR value directly.
        """

        return workflow_plan_digest(self)

    @property
    def digest(self) -> str:
        """Compatibility spelling for :attr:`workflow_digest`."""

        return self.workflow_digest

    def to_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-compatible normalized representation."""

        return workflow_plan_payload(self)

    @property
    def steps(self) -> tuple[StepPlan, ...]:
        result: list[StepPlan] = []
        for node in self.nodes:
            if isinstance(node, StepPlan):
                result.append(node)
            else:
                result.extend(node.body)
        return tuple(result)

    def artifact_for(self, reference: str) -> ArtifactKey | None:
        direct = self.artifact_graph.get(reference)
        if direct is not None:
            return direct
        payload, _expected = _normalization_expected_cardinality(reference)
        matches: list[ArtifactKey] = []
        for key in self.artifact_graph.values():
            parts = key.producer_node_id.split("/")
            local_producer = parts[1] if len(parts) == 2 else parts[-1]
            if key.scope == "workflow":
                aliases = {
                    f"artifact:{local_producer}.{key.output_id}",
                    f"artifact:{key.output_id}",
                }
            else:
                aliases = {
                    f"item-artifact:{local_producer}.{key.output_id}",
                    f"item-artifact:{key.producer_node_id}.{key.output_id}",
                }
            if payload in aliases:
                matches.append(key)
        unique = tuple(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else None

    def preflight_loop_sources(
        self,
        providers: object = None,
        *,
        provider_registry: object = None,
        registry: CapabilityRegistry | CapabilityRegistrySnapshot | None = None,
        hard_limits: WorkflowHardLimits | None = None,
    ) -> "WorkflowBoundsResult":
        """Snapshot bounded loop sources and validate their resource bounds."""

        return preflight_workflow_bounds(
            self,
            providers,
            provider_registry=provider_registry,
            registry=registry,
            hard_limits=hard_limits,
        )


class LoopSourceProvider(Protocol):
    """Trusted runtime provider for one registered loop source.

    Providers return a finite iterable of JSON-like item mappings.  The
    workflow config contains only the provider capability ID; the callable is
    supplied by the trusted runtime and is never loaded from JSON.
    """

    def snapshot(
        self,
        binding: LoopSourceBinding,
        limits: WorkflowEffectiveLimits,
    ) -> object:
        """Read a source once and return its source payload."""


@dataclass(frozen=True, slots=True)
class LoopItem:
    """One immutable, ordered item in a loop source snapshot."""

    item_id: str
    position: int
    payload: Mapping[str, object]
    size_bytes: int

    def __post_init__(self) -> None:
        if SAFE_IDENTIFIER_PATTERN.fullmatch(self.item_id) is None:
            raise ValueError("loop item id is not safe")
        if isinstance(self.position, bool) or not isinstance(self.position, int) or self.position < 0:
            raise ValueError("loop item position must be a non-negative integer")
        if not isinstance(self.payload, Mapping):
            raise TypeError("loop item payload must be a mapping")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("loop item size must be a non-negative integer")
        object.__setattr__(self, "payload", _freeze_metadata_value(dict(self.payload)))

    @property
    def id(self) -> str:
        return self.item_id

    @property
    def index(self) -> int:
        return self.position

    @property
    def value(self) -> Mapping[str, object]:
        return self.payload

    @property
    def data(self) -> Mapping[str, object]:
        return self.payload

    @property
    def bytes(self) -> int:
        return self.size_bytes


@dataclass(frozen=True, slots=True)
class LoopSourceSnapshot:
    """Frozen source data used by all loop iterations in one preflight."""

    loop_id: str
    provider: str
    source: str
    items: tuple[LoopItem, ...]
    digest: str
    size_bytes: int
    input_bytes: int

    def __post_init__(self) -> None:
        if SAFE_IDENTIFIER_PATTERN.fullmatch(self.loop_id) is None:
            raise ValueError("loop id is not safe")
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("loop source provider must be a non-empty string")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("loop source must be a non-empty string")
        object.__setattr__(self, "items", tuple(self.items))
        if re.fullmatch(r"[0-9a-f]{64}", self.digest) is None:
            raise ValueError("loop source digest must be lowercase hexadecimal")
        for field_name in ("size_bytes", "input_bytes"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")

    @property
    def snapshot_digest(self) -> str:
        return self.digest

    @property
    def source_digest(self) -> str:
        return self.digest

    @property
    def byte_size(self) -> int:
        return self.size_bytes

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(item.item_id for item in self.items)

    @property
    def payload_bytes(self) -> int:
        return self.input_bytes

    def item(self, item_id: str) -> LoopItem | None:
        return next((item for item in self.items if item.item_id == item_id), None)


@dataclass(frozen=True, slots=True)
class WorkflowBoundsResult:
    """Validated source snapshots and aggregate execution upper bounds."""

    plan: WorkflowPlan
    snapshots: Mapping[str, LoopSourceSnapshot]
    hard_limits: WorkflowHardLimits
    effective_limits: WorkflowEffectiveLimits
    node_count: int
    loop_count: int
    body_step_count: int
    loop_item_count: int
    snapshot_bytes: int
    input_bytes: int
    potential_step_executions: int
    snapshot_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", MappingProxyType(dict(self.snapshots)))
        for field_name in (
            "node_count",
            "loop_count",
            "body_step_count",
            "loop_item_count",
            "snapshot_bytes",
            "input_bytes",
            "potential_step_executions",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if re.fullmatch(r"[0-9a-f]{64}", self.snapshot_digest) is None:
            raise ValueError("workflow snapshot digest must be lowercase hexadecimal")

    @property
    def loop_sources(self) -> Mapping[str, LoopSourceSnapshot]:
        return self.snapshots

    @property
    def source_snapshots(self) -> Mapping[str, LoopSourceSnapshot]:
        return self.snapshots

    @property
    def item_count(self) -> int:
        return self.loop_item_count

    @property
    def total_snapshot_bytes(self) -> int:
        return self.snapshot_bytes

    @property
    def total_input_bytes(self) -> int:
        return self.input_bytes

    @property
    def potential_executions(self) -> int:
        return self.potential_step_executions

    @property
    def snapshot_digests(self) -> Mapping[str, str]:
        return MappingProxyType(
            {loop_id: snapshot.digest for loop_id, snapshot in self.snapshots.items()}
        )

    def snapshot_for(self, loop_id: str) -> LoopSourceSnapshot | None:
        return self.snapshots.get(loop_id)


    @property
    def digest(self) -> str:
        """Digest of the ordered source snapshots used by this plan."""

        return self.snapshot_digest


WorkflowPreflightResult = WorkflowBoundsResult
LoopSourceSnapshotResult = LoopSourceSnapshot


NormalizedWorkflow = WorkflowPlan
NormalizedWorkflowPlan = WorkflowPlan
WorkflowIR = WorkflowPlan
InputPlan = InputBindingPlan
OutputDeclarationPlan = OutputPlan
LoopSourcePlan = LoopSourceBinding
ArtifactRef = ArtifactReference


class ArtifactPathSafetyError(ValueError):
    """Raised when an artifact path cannot be proven to stay in its root."""


class ArtifactOutputValidationError(ValueError):
    """Raised when a declared output is missing, stale, or the wrong kind."""


_WINDOWS_RESERVED_ARTIFACT_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{index}" for index in range(1, 10)), *(f"lpt{index}" for index in range(1, 10))}
)


def _portable_artifact_path_parts(value: object, *, field_name: str = "artifact path") -> tuple[str, ...]:
    """Validate a config-controlled, portable POSIX-relative artifact path."""

    if isinstance(value, Path):
        text = value.as_posix()
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError(f"{field_name} must be a string path")
    if (
        not text
        or text.startswith("/")
        or text.startswith("\\")
        or "\\" in text
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in text)
    ):
        raise ValueError(f"{field_name} must be a safe repository-relative path")
    parts = tuple(text.split("/"))
    if not parts or any(
        part in {"", ".", ".."}
        or part.endswith(".")
        or SAFE_PATH_SEGMENT_PATTERN.fullmatch(part) is None
        or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_ARTIFACT_NAMES
        for part in parts
    ):
        raise ValueError(f"{field_name} must be a safe portable relative path")
    return parts


def _normalized_artifact_path_key(parts: Iterable[str]) -> str:
    """Return the collision key used by portable filesystems."""

    normalized: list[str] = []
    for part in parts:
        value = unicodedata.normalize("NFKC", part).casefold()
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("artifact path has an unsafe Unicode-normalized segment")
        normalized.append(value)
    return "/".join(normalized)


def _reject_runtime_path_syntax(path: Path | str) -> None:
    """Reject traversal/control syntax before ``Path`` normalizes it."""

    raw = os.fspath(path)
    if isinstance(raw, bytes):
        raise ArtifactPathSafetyError("artifact paths must be text paths")
    if not raw or ":" in raw or "\\" in raw or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in raw
    ):
        raise ArtifactPathSafetyError(f"unsafe artifact path syntax: {raw!r}")
    if not PurePosixPath(raw).is_absolute() and (
        PureWindowsPath(raw).drive or PureWindowsPath(raw).root
    ):
        raise ArtifactPathSafetyError(f"unsafe artifact path syntax: {raw!r}")
    parts = PurePosixPath(raw).parts
    if any(
        part in {"..", "."} or part.endswith(".")
        for part in parts
        if part != "/"
    ):
        raise ArtifactPathSafetyError(f"unsafe artifact path syntax: {raw!r}")


def _lstat_path(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactPathSafetyError(f"cannot inspect artifact path {path}: {exc}") from exc


class ArtifactPathGuard:
    """Validate artifact paths lexically and against current filesystem state.

    The guard never creates directories.  Callers use it both during
    side-effect-free preflight and immediately before a lock/open/replace.
    Existing symlink components are rejected even when their target remains
    below the artifact root; this keeps a later namespace swap fail closed.
    """

    def __init__(self, artifact_root: Path | str) -> None:
        if not isinstance(artifact_root, (Path, str)):
            raise TypeError("artifact_root must be a path")
        self.artifact_root = Path(artifact_root)
        self._root_absolute = Path(os.path.abspath(self.artifact_root))

    @property
    def root(self) -> Path:
        return self._root_absolute

    def _candidate(self, path: Path | str) -> Path:
        if isinstance(path, Path):
            raw_path = path
        elif isinstance(path, str):
            raw_path = Path(path)
        else:
            try:
                raw_path = Path(os.fspath(path))  # type: ignore[arg-type]
            except TypeError as exc:
                raise ArtifactPathSafetyError("artifact path must be path-like") from exc
        _reject_runtime_path_syntax(raw_path)
        candidate = raw_path if raw_path.is_absolute() else self._root_absolute / raw_path
        return Path(os.path.abspath(candidate))

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    def _reject_symlink_components(self, candidate: Path) -> None:
        if not self._is_relative_to(candidate, self._root_absolute):
            raise ArtifactPathSafetyError(f"artifact path escapes artifact root: {candidate}")
        root_state = _lstat_path(self._root_absolute)
        if root_state is not None and stat.S_ISLNK(root_state.st_mode):
            raise ArtifactPathSafetyError(
                f"Symlinked artifact root is not allowed: {self._root_absolute}"
            )
        # A repository/workdir may itself be reached through an operator-owned
        # symlink alias.  That ancestor is outside this guard's root and is
        # intentionally not rejected; only the root and descendants are part
        # of the artifact namespace boundary.
        current = self._root_absolute
        for component in candidate.relative_to(self._root_absolute).parts:
            current = current / component
            state = _lstat_path(current)
            if state is not None and stat.S_ISLNK(state.st_mode):
                raise ArtifactPathSafetyError(
                    f"Symlinked artifact path component is not allowed: {candidate}"
                )

    def validate(self, path: Path | str) -> Path:
        """Return an absolute path after containment and symlink checks."""

        candidate = self._candidate(path)
        if not self._is_relative_to(candidate, self._root_absolute):
            raise ArtifactPathSafetyError(f"artifact path escapes artifact root: {candidate}")

        # Inspect raw components before resolving.  Resolving first would hide
        # an in-root symlink and would make a replacement race look benign.
        self._reject_symlink_components(candidate)
        try:
            canonical_root = self._root_absolute.resolve(strict=False)
            canonical_candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ArtifactPathSafetyError(
                f"cannot resolve artifact path safely: {candidate}"
            ) from exc
        if not self._is_relative_to(canonical_candidate, canonical_root):
            raise ArtifactPathSafetyError(f"artifact path escapes artifact root: {candidate}")
        return candidate

    def validate_relative(self, relative_path: str | Path) -> Path:
        _portable_artifact_path_parts(relative_path)
        return self.validate(relative_path)

    def validate_namespace(self, namespace: "ArtifactNamespace") -> Path:
        if not isinstance(namespace, ArtifactNamespace):
            raise TypeError("namespace must be an ArtifactNamespace")
        return self.validate_relative(namespace.relative_path)

    def validate_root(self, *, require_directory: bool = False) -> Path:
        root = self.validate(self._root_absolute)
        state = _lstat_path(root)
        if state is not None and stat.S_ISLNK(state.st_mode):
            raise ArtifactPathSafetyError(f"artifact root must not be a symlink: {root}")
        if require_directory and state is not None and not stat.S_ISDIR(state.st_mode):
            raise ArtifactPathSafetyError(f"artifact root must be a directory: {root}")
        return root

    def ensure_directory(self, path: Path | str) -> Path:
        """Create a scope only after validation, then validate it again."""

        target = self.validate(path)
        state = _lstat_path(target)
        if state is not None and not stat.S_ISDIR(state.st_mode):
            raise ArtifactPathSafetyError(f"artifact scope must be a directory: {target}")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ArtifactPathSafetyError(f"cannot create artifact scope: {target}") from exc
        target = self.validate(target)
        state = _lstat_path(target)
        if state is None or not stat.S_ISDIR(state.st_mode):
            raise ArtifactPathSafetyError(f"artifact scope is not a directory: {target}")
        return target


@dataclass(frozen=True, slots=True)
class ArtifactNamespace:
    """One output's stable logical-to-physical namespace mapping."""

    artifact_key: ArtifactKey
    node_instance_id: str
    relative_path: str
    scope_relative_path: str
    kind: Literal["file", "directory"]
    item_id: str | None = None
    position: int | None = None
    is_export: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.node_instance_id, str) or not self.node_instance_id:
            raise ValueError("artifact node instance id must be a non-empty string")
        _portable_artifact_path_parts(self.relative_path)
        if self.scope_relative_path:
            _portable_artifact_path_parts(self.scope_relative_path, field_name="artifact scope")
        if self.kind not in {"file", "directory"}:
            raise ValueError(f"unsupported artifact output kind: {self.kind!r}")
        if self.artifact_key.scope == "loop_item":
            if self.item_id is None or SAFE_IDENTIFIER_PATTERN.fullmatch(self.item_id) is None:
                raise ValueError("loop item artifact namespace requires a safe item id")
            if (
                isinstance(self.position, bool)
                or not isinstance(self.position, int)
                or self.position < 0
            ):
                raise ValueError("loop item artifact namespace requires a non-negative position")
        elif self.item_id is not None or self.position is not None:
            raise ValueError("workflow artifact namespace cannot have loop item metadata")
        if self.is_export and self.artifact_key.cardinality != "collection":
            raise ValueError("only collection artifacts may be export namespaces")

    @property
    def key(self) -> ArtifactKey:
        return self.artifact_key

    @property
    def artifact(self) -> ArtifactKey:
        return self.artifact_key

    @property
    def path(self) -> str:
        return self.relative_path

    @property
    def artifact_path(self) -> str:
        return self.relative_path

    @property
    def namespace(self) -> str:
        return self.scope_relative_path

    @property
    def scope(self) -> str:
        return self.artifact_key.scope

    @property
    def cardinality(self) -> str:
        return self.artifact_key.cardinality

    @property
    def producer_node_id(self) -> str:
        return self.artifact_key.producer_node_id

    @property
    def output_id(self) -> str:
        return self.artifact_key.output_id

    @property
    def item_position(self) -> int | None:
        return self.position

    @property
    def normalized_path(self) -> str:
        return _normalized_artifact_path_key(self.relative_path.split("/"))

    def absolute_path(self, artifact_root: Path | str) -> Path:
        return ArtifactPathGuard(artifact_root).validate_relative(self.relative_path)


@dataclass(frozen=True, slots=True)
class ArtifactNamespacePlan:
    """All output namespaces computed before any artifact is created."""

    entries: tuple[ArtifactNamespace, ...]
    collection_exports: tuple[ArtifactNamespace, ...] = ()
    artifact_root: Path | None = None
    path_index: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        exports = tuple(self.collection_exports)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "collection_exports", exports)
        object.__setattr__(self, "path_index", MappingProxyType(dict(self.path_index)))

    @property
    def namespaces(self) -> tuple[ArtifactNamespace, ...]:
        return self.entries

    @property
    def all_entries(self) -> tuple[ArtifactNamespace, ...]:
        return self.entries + self.collection_exports

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.relative_path for item in self.all_entries)

    @property
    def by_instance(self) -> Mapping[str, tuple[ArtifactNamespace, ...]]:
        result: dict[str, list[ArtifactNamespace]] = {}
        for item in self.all_entries:
            result.setdefault(item.node_instance_id, []).append(item)
        return MappingProxyType({key: tuple(value) for key, value in result.items()})

    @property
    def by_artifact(self) -> Mapping[str, tuple[ArtifactNamespace, ...]]:
        result: dict[str, list[ArtifactNamespace]] = {}
        for item in self.all_entries:
            result.setdefault(item.artifact_key.reference, []).append(item)
        return MappingProxyType({key: tuple(value) for key, value in result.items()})

    def for_instance(self, node_instance_id: str) -> tuple[ArtifactNamespace, ...]:
        return self.by_instance.get(node_instance_id, ())

    def for_artifact(self, reference: str | ArtifactKey) -> tuple[ArtifactNamespace, ...]:
        key = reference.reference if isinstance(reference, ArtifactKey) else reference
        return self.by_artifact.get(key, ())

    def validate_runtime_paths(self, artifact_root: Path | str | None = None) -> "ArtifactNamespacePlan":
        root = artifact_root if artifact_root is not None else self.artifact_root
        if root is None:
            raise ValueError("artifact_root is required for runtime path validation")
        guard = ArtifactPathGuard(root)
        guard.validate_root()
        for namespace in self.all_entries:
            guard.validate(namespace.relative_path)
        return self

    recheck = validate_runtime_paths
    validate_runtime = validate_runtime_paths


# Descriptive aliases used by callers that refer to this result as a map or a
# preflight rather than as a plan.
ArtifactNamespaceResult = ArtifactNamespacePlan
ArtifactNamespaceMap = ArtifactNamespacePlan
ArtifactPathError = ArtifactPathSafetyError


@dataclass(frozen=True, slots=True)
class ArtifactFingerprint:
    """Bounded identity and content digest observed for one output path."""

    kind: Literal["file", "directory"]
    size_bytes: int
    mtime_ns: int
    device: int
    inode: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"file", "directory"}:
            raise ValueError(f"unsupported artifact fingerprint kind: {self.kind!r}")
        for field_name in ("size_bytes", "mtime_ns", "device", "inode"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("artifact fingerprint sha256 must be lowercase hexadecimal")

    @property
    def identity(self) -> tuple[object, ...]:
        """The fields used to decide whether an old output was replaced."""

        return (
            self.kind,
            self.device,
            self.inode,
            self.size_bytes,
            self.mtime_ns,
            self.sha256,
        )

    @property
    def freshness(self) -> str:
        return self.sha256

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "device": self.device,
            "inode": self.inode,
            "sha256": self.sha256,
        }


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("artifact write made no progress")
        view = view[written:]


def _read_regular_artifact(
    guard: ArtifactPathGuard,
    path: Path,
    *,
    max_bytes: int | None = None,
    collect_bytes: bool = False,
) -> ArtifactFingerprint | tuple[bytes, ArtifactFingerprint]:
    """Read a regular artifact through a no-follow descriptor and hash it."""

    target = guard.validate(path)
    before = _lstat_path(target)
    if before is None:
        raise ArtifactOutputValidationError(f"required artifact output is missing: {target}")
    if stat.S_ISLNK(before.st_mode):
        raise ArtifactPathSafetyError(f"Symlinked artifact output is not allowed: {target}")
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactOutputValidationError(f"artifact output must be a regular file: {target}")
    if max_bytes is not None and before.st_size > max_bytes:
        raise ArtifactOutputValidationError(
            f"artifact output exceeds {max_bytes} bytes: {target}"
        )

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    digest = hashlib.sha256()
    content = bytearray() if collect_bytes else None
    size = 0
    try:
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            raise ArtifactPathSafetyError(f"cannot open artifact output: {target}") from exc
        opened = os.fstat(descriptor)
        if (
            stat.S_ISLNK(opened.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise ArtifactPathSafetyError(
                f"artifact output changed between validation and open: {target}"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise ArtifactOutputValidationError(
                    f"artifact output exceeds {max_bytes} bytes: {target}"
                )
            digest.update(chunk)
            if content is not None:
                content.extend(chunk)
        after = _lstat_path(target)
        if after is None or stat.S_ISLNK(after.st_mode):
            raise ArtifactPathSafetyError(
                f"artifact output disappeared or became symlinked: {target}"
            )
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ArtifactPathSafetyError(
                f"artifact output changed while being read: {target}"
            )
        guard.validate(target)
        fingerprint = ArtifactFingerprint(
            kind="file",
            size_bytes=size,
            mtime_ns=after.st_mtime_ns,
            device=after.st_dev,
            inode=after.st_ino,
            sha256=digest.hexdigest(),
        )
        if content is not None:
            return bytes(content), fingerprint
        return fingerprint
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_artifact_bytes(
    guard: ArtifactPathGuard,
    path: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes, ArtifactFingerprint]:
    value = _read_regular_artifact(
        guard,
        path,
        max_bytes=max_bytes,
        collect_bytes=True,
    )
    assert isinstance(value, tuple)
    return value


def _runtime_component_key(name: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
    ):
        raise ArtifactPathSafetyError(f"unsafe artifact directory entry: {name!r}")
    return unicodedata.normalize("NFKC", name).casefold()


def _read_directory_artifact(
    guard: ArtifactPathGuard,
    path: Path,
    *,
    max_bytes: int | None = None,
) -> ArtifactFingerprint:
    target = guard.validate(path)
    before = _lstat_path(target)
    if before is None:
        raise ArtifactOutputValidationError(f"required artifact output is missing: {target}")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise ArtifactOutputValidationError(f"artifact output must be a directory: {target}")

    records: list[dict[str, object]] = []
    total_size = 0
    normalized_names: dict[str, str] = {}
    try:
        entries = sorted(os.scandir(target), key=lambda item: item.name)
    except OSError as exc:
        raise ArtifactPathSafetyError(f"cannot inspect artifact directory: {target}") from exc

    for entry in entries:
        name_key = _runtime_component_key(entry.name)
        previous = normalized_names.get(name_key)
        if previous is not None and previous != entry.name:
            raise ArtifactPathSafetyError(
                f"normalized artifact directory entry collision: {previous!r}, {entry.name!r}"
            )
        normalized_names[name_key] = entry.name
        child = target / entry.name
        guard.validate(child)
        child_state = _lstat_path(child)
        if child_state is None or stat.S_ISLNK(child_state.st_mode):
            raise ArtifactPathSafetyError(f"Symlinked or missing artifact directory entry: {child}")
        if stat.S_ISDIR(child_state.st_mode):
            fingerprint = _read_directory_artifact(guard, child, max_bytes=max_bytes)
        elif stat.S_ISREG(child_state.st_mode):
            fingerprint = _read_regular_artifact(guard, child, max_bytes=max_bytes)
        else:
            raise ArtifactOutputValidationError(
                f"artifact directory contains a non-regular entry: {child}"
            )
        total_size += fingerprint.size_bytes
        if max_bytes is not None and total_size > max_bytes:
            raise ArtifactOutputValidationError(
                f"artifact directory exceeds {max_bytes} bytes: {target}"
            )
        records.append(
            {
                "name": name_key,
                "kind": fingerprint.kind,
                "size_bytes": fingerprint.size_bytes,
                "sha256": fingerprint.sha256,
            }
        )

    after = _lstat_path(target)
    if after is None or stat.S_ISLNK(after.st_mode) or not stat.S_ISDIR(after.st_mode):
        raise ArtifactPathSafetyError(f"artifact directory changed while being read: {target}")
    if after.st_dev != before.st_dev or after.st_ino != before.st_ino:
        raise ArtifactPathSafetyError(f"artifact directory was replaced while being read: {target}")
    canonical = json.dumps(
        {"entries": records},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    guard.validate(target)
    return ArtifactFingerprint(
        kind="directory",
        size_bytes=total_size,
        mtime_ns=after.st_mtime_ns,
        device=after.st_dev,
        inode=after.st_ino,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def fingerprint_artifact(
    artifact_root: Path | str,
    path: Path | str,
    *,
    max_bytes: int | None = None,
) -> ArtifactFingerprint:
    """Return a symlink-safe fingerprint for a file or directory output."""

    guard = ArtifactPathGuard(artifact_root)
    target = guard.validate(path)
    state = _lstat_path(target)
    if state is None:
        raise ArtifactOutputValidationError(f"required artifact output is missing: {target}")
    if stat.S_ISLNK(state.st_mode):
        raise ArtifactPathSafetyError(f"Symlinked artifact output is not allowed: {target}")
    if stat.S_ISREG(state.st_mode):
        return _read_regular_artifact(guard, target, max_bytes=max_bytes)
    if stat.S_ISDIR(state.st_mode):
        return _read_directory_artifact(guard, target, max_bytes=max_bytes)
    raise ArtifactOutputValidationError(f"artifact output has unsupported type: {target}")


def _fingerprint_if_present(
    guard: ArtifactPathGuard,
    path: Path | str,
    *,
    max_bytes: int | None = None,
) -> ArtifactFingerprint | None:
    target = guard.validate(path)
    state = _lstat_path(target)
    if state is None:
        return None
    return fingerprint_artifact(guard.root, target, max_bytes=max_bytes)


def _artifact_freshness_token(
    *,
    run_identity: str,
    node_instance_id: str,
    relative_path: str,
    fingerprint: ArtifactFingerprint,
) -> str:
    payload = {
        "run_identity": run_identity,
        "node_instance_id": node_instance_id,
        "relative_path": relative_path,
        "fingerprint": fingerprint.to_dict(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactManifestEntry:
    """Durable output contract including producer identity and freshness."""

    run_identity: str
    node_instance_id: str
    producer_node_id: str
    output_id: str
    scope: Literal["workflow", "loop_item"]
    cardinality: Literal["scalar", "collection"]
    kind: Literal["file", "directory"]
    relative_path: str
    item_id: str | None
    item_position: int | None
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    freshness: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_identity, str) or not self.run_identity:
            raise ValueError("manifest run identity must be a non-empty string")
        if not isinstance(self.node_instance_id, str) or not self.node_instance_id:
            raise ValueError("manifest node instance id must be a non-empty string")
        if not isinstance(self.producer_node_id, str) or not self.producer_node_id:
            raise ValueError("manifest producer node id must be a non-empty string")
        if SAFE_IDENTIFIER_PATTERN.fullmatch(self.output_id) is None:
            raise ValueError("manifest output id is not safe")
        if not isinstance(self.scope, str) or self.scope not in ARTIFACT_SCOPES:
            raise ValueError(f"unsupported manifest scope: {self.scope!r}")
        if not isinstance(self.cardinality, str) or self.cardinality not in ARTIFACT_CARDINALITIES:
            raise ValueError(f"unsupported manifest cardinality: {self.cardinality!r}")
        if not isinstance(self.kind, str) or self.kind not in {"file", "directory"}:
            raise ValueError(f"unsupported manifest kind: {self.kind!r}")
        _portable_artifact_path_parts(self.relative_path, field_name="manifest relative path")
        if self.scope == "loop_item":
            if self.item_id is None or SAFE_IDENTIFIER_PATTERN.fullmatch(self.item_id) is None:
                raise ValueError("item-scoped manifest requires a safe item id")
            if (
                isinstance(self.item_position, bool)
                or not isinstance(self.item_position, int)
                or self.item_position < 0
            ):
                raise ValueError("item-scoped manifest requires a non-negative item position")
        elif self.item_id is not None or self.item_position is not None:
            raise ValueError("workflow-scoped manifest cannot contain item metadata")
        for field_name in ("size_bytes", "device", "inode", "mtime_ns"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"manifest {field_name} must be a non-negative integer")
        for field_name in ("sha256", "freshness"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"manifest {field_name} must be lowercase hexadecimal")

    @classmethod
    def from_namespace(
        cls,
        namespace: ArtifactNamespace,
        *,
        run_identity: str,
        fingerprint: ArtifactFingerprint,
    ) -> "ArtifactManifestEntry":
        if not isinstance(namespace, ArtifactNamespace):
            raise TypeError("namespace must be an ArtifactNamespace")
        if fingerprint.kind != namespace.kind:
            raise ArtifactOutputValidationError(
                f"artifact output kind mismatch for {namespace.relative_path}: "
                f"expected {namespace.kind}, got {fingerprint.kind}"
            )
        return cls(
            run_identity=run_identity,
            node_instance_id=namespace.node_instance_id,
            producer_node_id=namespace.producer_node_id,
            output_id=namespace.output_id,
            scope=namespace.scope,
            cardinality=namespace.cardinality,
            kind=namespace.kind,
            relative_path=namespace.relative_path,
            item_id=namespace.item_id,
            item_position=namespace.position,
            size_bytes=fingerprint.size_bytes,
            sha256=fingerprint.sha256,
            device=fingerprint.device,
            inode=fingerprint.inode,
            mtime_ns=fingerprint.mtime_ns,
            freshness=_artifact_freshness_token(
                run_identity=run_identity,
                node_instance_id=namespace.node_instance_id,
                relative_path=namespace.relative_path,
                fingerprint=fingerprint,
            ),
        )

    @property
    def identity(self) -> tuple[str, str, str | None]:
        return (self.node_instance_id, self.output_id, self.item_id)

    @property
    def artifact_key(self) -> ArtifactKey:
        return ArtifactKey(
            producer_node_id=self.producer_node_id,
            output_id=self.output_id,
            scope=self.scope,
            cardinality=self.cardinality,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_identity": self.run_identity,
            "node_instance_id": self.node_instance_id,
            "producer_node_id": self.producer_node_id,
            "output_id": self.output_id,
            "scope": self.scope,
            "cardinality": self.cardinality,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "item_id": self.item_id,
            "item_position": self.item_position,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "device": self.device,
            "inode": self.inode,
            "mtime_ns": self.mtime_ns,
            "freshness": self.freshness,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ArtifactManifestEntry":
        if not isinstance(value, Mapping):
            raise ValueError("artifact manifest entry must be an object")
        required = {
            "run_identity",
            "node_instance_id",
            "producer_node_id",
            "output_id",
            "scope",
            "cardinality",
            "kind",
            "relative_path",
            "item_id",
            "item_position",
            "size_bytes",
            "sha256",
            "device",
            "inode",
            "mtime_ns",
            "freshness",
        }
        unknown = set(value) - required
        missing = required - set(value)
        if unknown:
            raise ValueError("artifact manifest entry has unsupported keys: " + ", ".join(sorted(unknown)))
        if missing:
            raise ValueError("artifact manifest entry is missing keys: " + ", ".join(sorted(missing)))
        return cls(
            run_identity=value["run_identity"],  # type: ignore[arg-type]
            node_instance_id=value["node_instance_id"],  # type: ignore[arg-type]
            producer_node_id=value["producer_node_id"],  # type: ignore[arg-type]
            output_id=value["output_id"],  # type: ignore[arg-type]
            scope=value["scope"],  # type: ignore[arg-type]
            cardinality=value["cardinality"],  # type: ignore[arg-type]
            kind=value["kind"],  # type: ignore[arg-type]
            relative_path=value["relative_path"],  # type: ignore[arg-type]
            item_id=value["item_id"],  # type: ignore[arg-type]
            item_position=value["item_position"],  # type: ignore[arg-type]
            size_bytes=value["size_bytes"],  # type: ignore[arg-type]
            sha256=value["sha256"],  # type: ignore[arg-type]
            device=value["device"],  # type: ignore[arg-type]
            inode=value["inode"],  # type: ignore[arg-type]
            mtime_ns=value["mtime_ns"],  # type: ignore[arg-type]
            freshness=value["freshness"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    """Atomic manifest of outputs produced by one workflow run."""

    run_identity: str
    entries: tuple[ArtifactManifestEntry, ...]
    schema_version: str = ARTIFACT_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ARTIFACT_MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported artifact manifest schema: {self.schema_version!r}")
        if not isinstance(self.run_identity, str) or not self.run_identity:
            raise ValueError("artifact manifest run identity must be a non-empty string")
        entries = tuple(self.entries)
        seen_identities: set[tuple[str, str, str | None]] = set()
        seen_paths: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, ArtifactManifestEntry):
                raise TypeError("artifact manifest entries must be ArtifactManifestEntry values")
            if entry.run_identity != self.run_identity:
                raise ValueError("artifact manifest entry run identity does not match manifest")
            if entry.identity in seen_identities:
                raise ValueError("artifact manifest contains duplicate output identity")
            normalized_path = _normalized_artifact_path_key(entry.relative_path.split("/"))
            if normalized_path in seen_paths:
                raise ValueError("artifact manifest contains a normalized duplicate output path")
            if any(
                normalized_path.startswith(previous_key + "/")
                or previous_key.startswith(normalized_path + "/")
                for previous_key in seen_paths
            ):
                raise ValueError("artifact manifest contains overlapping output paths")
            seen_identities.add(entry.identity)
            seen_paths[normalized_path] = entry.relative_path
        object.__setattr__(self, "entries", entries)

    @property
    def output_entries(self) -> tuple[ArtifactManifestEntry, ...]:
        return self.entries

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_identity": self.run_identity,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, value: object) -> "ArtifactManifest":
        if not isinstance(value, Mapping):
            raise ValueError("artifact manifest must be an object")
        required = {"schema_version", "run_identity", "entries"}
        unknown = set(value) - required
        missing = required - set(value)
        if unknown:
            raise ValueError("artifact manifest has unsupported keys: " + ", ".join(sorted(unknown)))
        if missing:
            raise ValueError("artifact manifest is missing keys: " + ", ".join(sorted(missing)))
        raw_entries = value["entries"]
        if not isinstance(raw_entries, list):
            raise ValueError("artifact manifest entries must be an array")
        return cls(
            schema_version=value["schema_version"],  # type: ignore[arg-type]
            run_identity=value["run_identity"],  # type: ignore[arg-type]
            entries=tuple(ArtifactManifestEntry.from_dict(item) for item in raw_entries),
        )

    def validate(
        self,
        artifact_root: Path | str,
        *,
        expected_namespaces: ArtifactNamespacePlan | Iterable[ArtifactNamespace] | None = None,
    ) -> "ArtifactManifest":
        return validate_artifact_manifest(
            artifact_root,
            self,
            expected_run_identity=self.run_identity,
            expected_namespaces=expected_namespaces,
        )


class ArtifactScopeLock:
    """Exclusive lock for one prevalidated artifact scope."""

    def __init__(
        self,
        guard: ArtifactPathGuard,
        scope: ArtifactNamespace | Path | str,
        *,
        owner: str = "workflow",
        filename: str = ARTIFACT_SCOPE_LOCK_FILENAME,
    ) -> None:
        self.guard = guard
        self.scope = scope
        self.owner = owner
        self.filename = filename
        self.scope_path: Path | None = None
        self.lock_path: Path | None = None
        self._descriptor: int | None = None

    def _scope_target(self) -> Path:
        if isinstance(self.scope, ArtifactNamespace):
            if self.scope.scope_relative_path:
                return self.guard.validate(self.scope.scope_relative_path)
            return self.guard.validate_root()
        if isinstance(self.scope, str) and not self.scope:
            return self.guard.validate_root()
        return self.guard.validate(self.scope)  # type: ignore[arg-type]

    def __enter__(self) -> "ArtifactScopeLock":
        scope_path = self.guard.ensure_directory(self._scope_target())
        lock_path = self.guard.validate(scope_path / self.filename)
        # Recheck both parent and lock path immediately before O_EXCL open.
        self.guard.validate(scope_path)
        self.guard.validate(lock_path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except FileExistsError as exc:
            raise RuntimeError(f"artifact scope is already locked: {scope_path}") from exc
        except OSError as exc:
            raise ArtifactPathSafetyError(f"cannot lock artifact scope: {scope_path}") from exc

        self.scope_path = scope_path
        self.lock_path = lock_path
        self._descriptor = descriptor
        try:
            _write_all(
                descriptor,
                f"owner={self.owner}\npid={os.getpid()}\n".encode("utf-8"),
            )
            os.fsync(descriptor)
            os.close(descriptor)
            self._descriptor = None
            # A replacement of a checked component is rejected before the
            # caller's execution port is entered.
            self.guard.validate(scope_path)
            self.guard.validate(lock_path)
        except BaseException:
            self._close_descriptor()
            self._remove_lock_if_safe()
            raise
        return self

    def _close_descriptor(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def _remove_lock_if_safe(self) -> None:
        lock_path = self.lock_path
        if lock_path is None:
            return
        try:
            self.guard.validate(lock_path)
        except ArtifactPathSafetyError:
            # Never follow a component that changed after the lock was
            # acquired while cleaning up an abandoned lock.
            return
        state = _lstat_path(lock_path)
        if state is not None and stat.S_ISREG(state.st_mode):
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._close_descriptor()
        self._remove_lock_if_safe()
        self.scope_path = None
        self.lock_path = None


class ArtifactManifestStore:
    """Read and atomically write symlink-safe output manifests."""

    def __init__(self, artifact_root: Path | str) -> None:
        self.guard = ArtifactPathGuard(artifact_root)

    @property
    def artifact_root(self) -> Path:
        return self.guard.root

    def manifest_path(self, relative_path: str | Path = DEFAULT_ARTIFACT_MANIFEST_PATH) -> Path:
        return self.guard.validate(relative_path)

    def ensure_scope(self, scope: ArtifactNamespace | Path | str) -> Path:
        if isinstance(scope, ArtifactNamespace):
            scope = scope.scope_relative_path or self.artifact_root
        return self.guard.ensure_directory(scope)

    def scope_lock(
        self,
        scope: ArtifactNamespace | Path | str,
        *,
        owner: str = "workflow",
    ) -> ArtifactScopeLock:
        return ArtifactScopeLock(self.guard, scope, owner=owner)

    lock = scope_lock

    @contextmanager
    def locked_scope(
        self,
        scope: ArtifactNamespace | Path | str,
        *,
        owner: str = "workflow",
    ) -> Iterator[Path]:
        with self.scope_lock(scope, owner=owner) as lock:
            assert lock.scope_path is not None
            yield lock.scope_path

    def write(
        self,
        manifest: ArtifactManifest,
        *,
        relative_path: str | Path = DEFAULT_ARTIFACT_MANIFEST_PATH,
    ) -> Path:
        if not isinstance(manifest, ArtifactManifest):
            raise TypeError("manifest must be an ArtifactManifest")
        return self.atomic_write_bytes(self.manifest_path(relative_path), manifest.to_json_bytes())

    def read(
        self,
        *,
        relative_path: str | Path = DEFAULT_ARTIFACT_MANIFEST_PATH,
    ) -> ArtifactManifest:
        path = self.manifest_path(relative_path)
        content, _raw = _read_regular_artifact_bytes(
            self.guard,
            path,
            max_bytes=MAX_ARTIFACT_MANIFEST_BYTES,
        )
        try:
            payload = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=_manifest_object_pairs,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"artifact manifest is not valid JSON: {path}") from exc
        return ArtifactManifest.from_dict(payload)

    def atomic_write_bytes(self, path: Path | str, value: bytes) -> Path:
        if not isinstance(value, bytes):
            raise TypeError("artifact writes require bytes")
        target = self.guard.validate(path)
        parent = self.guard.ensure_directory(target.parent)
        # This is deliberately repeated after directory creation and directly
        # before the temporary file is opened.  A validation-time symlink swap
        # therefore stops the write before the execution port is reached.
        self.guard.validate(parent)
        self.guard.validate(target)
        descriptor: int | None = None
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=str(parent),
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, value)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            self.guard.validate(parent)
            self.guard.validate(target)
            os.replace(temporary_path, target)
            temporary_path = None
            self.guard.validate(target)
            try:
                directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return target
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _manifest_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate artifact manifest key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ArtifactOutputExpectation:
    """Pre-execution output observation used for freshness validation."""

    namespace: ArtifactNamespace
    run_identity: str
    before: ArtifactFingerprint | None

    @classmethod
    def capture(
        cls,
        artifact_root: Path | str,
        namespace: ArtifactNamespace,
        *,
        run_identity: str,
        max_bytes: int | None = None,
    ) -> "ArtifactOutputExpectation":
        if not isinstance(namespace, ArtifactNamespace):
            raise TypeError("namespace must be an ArtifactNamespace")
        guard = ArtifactPathGuard(artifact_root)
        guard.validate_namespace(namespace)
        return cls(
            namespace=namespace,
            run_identity=run_identity,
            before=_fingerprint_if_present(
                guard,
                namespace.relative_path,
                max_bytes=max_bytes,
            ),
        )

    def validate(self, artifact_root: Path | str, *, max_bytes: int | None = None) -> ArtifactManifestEntry:
        guard = ArtifactPathGuard(artifact_root)
        guard.validate_namespace(self.namespace)
        current = fingerprint_artifact(
            guard.root,
            self.namespace.relative_path,
            max_bytes=max_bytes,
        )
        if current.kind != self.namespace.kind:
            raise ArtifactOutputValidationError(
                f"artifact output kind mismatch for {self.namespace.relative_path}: "
                f"expected {self.namespace.kind}, got {current.kind}"
            )
        if self.before is not None and current.identity == self.before.identity:
            raise ArtifactOutputValidationError(
                f"stale artifact output was not replaced: {self.namespace.relative_path}"
            )
        return ArtifactManifestEntry.from_namespace(
            self.namespace,
            run_identity=self.run_identity,
            fingerprint=current,
        )


class ArtifactOutputValidator:
    """Capture declared outputs before execution and validate them after it."""

    def __init__(
        self,
        artifact_root: Path | str,
        namespaces: ArtifactNamespacePlan | Iterable[ArtifactNamespace],
        *,
        run_identity: str,
        include_exports: bool = False,
        max_bytes: int | None = None,
    ) -> None:
        self.store = ArtifactManifestStore(artifact_root)
        if isinstance(namespaces, ArtifactNamespacePlan):
            raw_namespaces = namespaces.all_entries if include_exports else namespaces.entries
        else:
            raw_namespaces = tuple(namespaces)
        self.namespaces = tuple(raw_namespaces)
        self.run_identity = run_identity
        self.max_bytes = max_bytes
        seen: set[tuple[str, str, str | None]] = set()
        for namespace in self.namespaces:
            if not isinstance(namespace, ArtifactNamespace):
                raise TypeError("namespaces must contain ArtifactNamespace values")
            identity = (namespace.node_instance_id, namespace.output_id, namespace.item_id)
            if identity in seen:
                raise ValueError("duplicate artifact output expectation")
            seen.add(identity)
            self.store.guard.validate_namespace(namespace)
        self.expectations = tuple(
            ArtifactOutputExpectation.capture(
                self.store.artifact_root,
                namespace,
                run_identity=run_identity,
                max_bytes=max_bytes,
            )
            for namespace in self.namespaces
        )
        self._validated_entries: tuple[ArtifactManifestEntry, ...] | None = None

    def validate_one(self, index: int) -> ArtifactManifestEntry:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self.expectations):
            raise IndexError("artifact output expectation index is out of range")
        return self.expectations[index].validate(
            self.store.artifact_root,
            max_bytes=self.max_bytes,
        )

    def validate_all(self) -> ArtifactManifest:
        entries: list[ArtifactManifestEntry] = []
        errors: list[str] = []
        for index in range(len(self.expectations)):
            try:
                entries.append(self.validate_one(index))
            except (ArtifactPathSafetyError, ArtifactOutputValidationError, OSError, ValueError) as exc:
                errors.append(str(exc))
        if errors:
            raise ArtifactOutputValidationError("; ".join(errors))
        self._validated_entries = tuple(entries)
        return ArtifactManifest(run_identity=self.run_identity, entries=tuple(entries))

    validate_required_outputs = validate_all

    def manifest(self) -> ArtifactManifest:
        if self._validated_entries is None:
            return self.validate_all()
        return ArtifactManifest(run_identity=self.run_identity, entries=self._validated_entries)

    def write_manifest(
        self,
        *,
        relative_path: str | Path = DEFAULT_ARTIFACT_MANIFEST_PATH,
    ) -> Path:
        return self.store.write(self.manifest(), relative_path=relative_path)


OutputManifestEntry = ArtifactManifestEntry
OutputManifest = ArtifactManifest
OutputManifestStore = ArtifactManifestStore
OutputExpectation = ArtifactOutputExpectation
RequiredOutputValidator = ArtifactOutputValidator


@dataclass(frozen=True, slots=True)
class _NormalizationNodeInfo:
    local_id: str
    canonical_id: str
    config: WorkflowNode
    index: int
    parent_loop_id: str | None = None


@dataclass(frozen=True, slots=True)
class _NormalizationContext:
    kind: Literal["top", "body"]
    top_index: int
    loop_id: str | None = None
    body_index: int | None = None


def _normalization_expected_cardinality(source: str) -> tuple[str, str | None]:
    """Strip optional ``[*]`` / ``[scalar]`` annotations from a reference."""

    for suffix, cardinality in (
        ("[*]", "collection"),
        ("[collection]", "collection"),
        ("[scalar]", "scalar"),
    ):
        if source.endswith(suffix):
            return source[: -len(suffix)], cardinality
    return source, None


def _normalization_identifier(value: object) -> bool:
    return isinstance(value, str) and SAFE_IDENTIFIER_PATTERN.fullmatch(value) is not None


def _normalization_reference_parts(payload: str) -> tuple[str, str, str | None] | None:
    """Parse qualified artifact payloads without treating paths as filesystem paths.

    The returned tuple is ``(kind, node-or-step-id, output-id)`` where kind is
    ``top`` for a top-level producer, ``body`` for a canonical body producer,
    or ``short`` for an unqualified compatibility spelling.
    """

    if not payload:
        return None
    if payload.startswith("nodes/"):
        parts = payload.split("/")
        if len(parts) == 3 and parts[0] == "nodes":
            node_id, output_id = parts[1], parts[2]
            return ("top", node_id, output_id)
        if len(parts) == 5 and parts[0] == "nodes" and parts[2] == "body":
            return ("body", f"{parts[1]}:{parts[3]}", parts[4])
        # Also accept the dotted output spelling after a canonical node path.
        if "." in payload:
            node_path, output_id = payload.rsplit(".", 1)
            parts = node_path.split("/")
            if len(parts) == 2 and parts[0] == "nodes":
                return ("top", parts[1], output_id)
            if len(parts) == 4 and parts[0] == "nodes" and parts[2] == "body":
                return ("body", f"{parts[1]}:{parts[3]}", output_id)
        return None
    if "." in payload:
        parts = payload.split(".")
        if len(parts) == 4 and parts[1] == "body":
            loop_id, body_id, output_id = parts[0], parts[2], parts[3]
            return ("body", f"{loop_id}:{body_id}", output_id)
        if len(parts) != 2:
            return None
        left, right = parts
        if not left or not right:
            return None
        return ("top", left, right)
    return ("short", payload, None)


def normalize_workflow_config(
    config: WorkflowConfig,
    *,
    source_path: Path | str | None = None,
) -> WorkflowPlan:
    """Normalize a parsed v1 DTO into a typed, declaration-ordered workflow IR.

    This stage performs only structural and artifact/dependency validation. It
    does not read loop sources, resolve runner commands, inspect resources, or
    execute lifecycle policy. All diagnostics are reported before a plan is
    returned, so callers can keep the executor behind a fail-closed boundary.
    """

    if not isinstance(config, WorkflowConfig):
        raise TypeError("config must be a WorkflowConfig")

    issues = _IssueCollector()
    source_path_value = Path(source_path) if source_path is not None else None

    top_infos: dict[str, _NormalizationNodeInfo] = {}
    top_order: list[_NormalizationNodeInfo] = []
    body_infos: dict[str, dict[str, _NormalizationNodeInfo]] = {}
    body_order: dict[str, tuple[_NormalizationNodeInfo, ...]] = {}
    body_output_keys: dict[tuple[str, str, str], ArtifactKey] = {}
    top_output_keys: dict[tuple[str, str], ArtifactKey] = {}
    export_keys: dict[tuple[str, str], ArtifactKey] = {}
    artifact_graph: dict[str, ArtifactKey] = {}
    body_output_short: dict[tuple[str, str], list[tuple[str, ArtifactKey]]] = {}

    def add_duplicate(path: str, label: str, value: str) -> None:
        issues.add("duplicate_id", path, f"duplicate {label} {value!r}")

    def register_top_nodes() -> None:
        for node_index, node in enumerate(config.nodes):
            path = f"/nodes/{node_index}"
            if not isinstance(node, (StepConfig, LoopConfig)):
                issues.add("wrong_type", path, "workflow node is not a supported DTO")
                continue
            local_id = node.id
            if not _normalization_identifier(local_id):
                issues.add("invalid_identifier", f"{path}/id", "node id is not a safe identifier")
                continue
            if local_id in top_infos:
                add_duplicate(f"{path}/id", "node id", local_id)
                continue
            info = _NormalizationNodeInfo(
                local_id=local_id,
                canonical_id=f"nodes/{local_id}",
                config=node,
                index=node_index,
            )
            top_infos[local_id] = info
            top_order.append(info)
            if isinstance(node, LoopConfig):
                body_map: dict[str, _NormalizationNodeInfo] = {}
                body_list: list[_NormalizationNodeInfo] = []
                for body_index, step in enumerate(node.body):
                    body_path = f"{path}/body/{body_index}"
                    if not isinstance(step, StepConfig):
                        issues.add("wrong_type", body_path, "loop body node must be a step")
                        continue
                    body_id = step.id
                    if not _normalization_identifier(body_id):
                        issues.add("invalid_identifier", f"{body_path}/id", "step id is not a safe identifier")
                        continue
                    if body_id in body_map:
                        add_duplicate(f"{body_path}/id", "body step id", body_id)
                        continue
                    body_info = _NormalizationNodeInfo(
                        local_id=body_id,
                        canonical_id=f"{info.canonical_id}/body/{body_id}",
                        config=step,
                        index=body_index,
                        parent_loop_id=local_id,
                    )
                    body_map[body_id] = body_info
                    body_list.append(body_info)
                body_infos[local_id] = body_map
                body_order[local_id] = tuple(body_list)

    def register_step_outputs(
        info: _NormalizationNodeInfo,
        *,
        loop_id: str | None,
        config_path: str,
    ) -> None:
        step = info.config
        if not isinstance(step, StepConfig):
            return
        seen: set[str] = set()
        for output_index, output in enumerate(step.outputs):
            output_path = f"{config_path}/outputs/{output_index}"
            if not isinstance(output, OutputDeclaration):
                issues.add("wrong_type", output_path, "output is not an OutputDeclaration")
                continue
            if output.id in seen:
                add_duplicate(f"{output_path}/id", "output id", output.id)
                continue
            seen.add(output.id)
            if not _normalization_identifier(output.id):
                issues.add("invalid_identifier", f"{output_path}/id", "output id is not a safe identifier")
                continue
            scope: Literal["workflow", "loop_item"] = "loop_item" if loop_id is not None else "workflow"
            key = ArtifactKey(
                producer_node_id=info.canonical_id,
                output_id=output.id,
                scope=scope,
                cardinality="scalar",
            )
            if loop_id is None:
                top_output_keys[(info.local_id, output.id)] = key
            else:
                body_output_keys[(loop_id, info.local_id, output.id)] = key
                body_output_short.setdefault((info.local_id, output.id), []).append((loop_id, key))
            artifact_graph[key.reference] = key

    def register_outputs() -> None:
        for info in top_order:
            path = f"/nodes/{info.index}"
            if isinstance(info.config, StepConfig):
                register_step_outputs(info, loop_id=None, config_path=path)
            else:
                for body_info in body_order.get(info.local_id, ()):
                    body_index = body_info.index
                    register_step_outputs(
                        body_info,
                        loop_id=info.local_id,
                        config_path=f"{path}/body/{body_index}",
                    )
                loop = info.config
                seen: set[str] = set()
                for export_index, export in enumerate(loop.exports):
                    export_path = f"{path}/exports/{export_index}"
                    if not isinstance(export, CollectionExport):
                        issues.add("wrong_type", export_path, "loop export is not a CollectionExport")
                        continue
                    if export.id in seen:
                        add_duplicate(f"{export_path}/id", "export id", export.id)
                        continue
                    seen.add(export.id)
                    if not _normalization_identifier(export.id):
                        issues.add("invalid_identifier", f"{export_path}/id", "export id is not a safe identifier")
                        continue
                    key = ArtifactKey(
                        producer_node_id=info.canonical_id,
                        output_id=export.id,
                        scope="workflow",
                        cardinality="collection",
                    )
                    export_keys[(info.local_id, export.id)] = key
                    artifact_graph[key.reference] = key

    register_top_nodes()
    register_outputs()

    def emit_reference_error(code: str, path: str, message: str) -> None:
        issues.add(code, path, message)

    def validate_cardinality(
        key: ArtifactKey,
        expected: str | None,
        path: str,
    ) -> bool:
        if expected is not None and key.cardinality != expected:
            emit_reference_error(
                "cardinality_mismatch",
                path,
                f"artifact has cardinality {key.cardinality!r}, expected {expected!r}",
            )
            return False
        return True

    def find_top_output(node_id: str, output_id: str) -> ArtifactKey | None:
        key = top_output_keys.get((node_id, output_id))
        if key is not None:
            return key
        return export_keys.get((node_id, output_id))

    def find_short_output(output_id: str) -> tuple[ArtifactKey | None, bool]:
        matches = [
            key
            for (node_id, candidate), key in (*top_output_keys.items(), *export_keys.items())
            if candidate == output_id
        ]
        unique = tuple(dict.fromkeys(matches))
        if len(unique) == 1:
            return unique[0], False
        if len(unique) > 1:
            return None, True
        return None, False

    def find_body_output(
        loop_id: str,
        body_id: str,
        output_id: str,
    ) -> ArtifactKey | None:
        return body_output_keys.get((loop_id, body_id, output_id))

    def check_reachability(
        key: ArtifactKey,
        context: _NormalizationContext,
        path: str,
    ) -> bool:
        producer = key.producer_node_id
        if key.scope == "loop_item":
            if context.kind != "body" or context.loop_id is None:
                emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "loop-item artifact cannot be consumed outside its loop item scope",
                )
                return False
            owner = producer.split("/body/", 1)
            if len(owner) != 2 or owner[0] != f"nodes/{context.loop_id}":
                emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "artifact belongs to a different loop item scope",
                )
                return False
            producer_step = owner[1]
            producer_info = body_infos[context.loop_id].get(producer_step)
            if producer_info is None or context.body_index is None:
                emit_reference_error("undefined_reference", path, "artifact producer is not a declared loop step")
                return False
            if producer_info.index >= context.body_index:
                emit_reference_error(
                    "unreachable_dependency",
                    path,
                    "artifact producer must be declared before its consumer in the loop body",
                )
                return False
            return True

        producer_info = next(
            (item for item in top_order if item.canonical_id == producer),
            None,
        )
        if producer_info is None:
            emit_reference_error("undefined_reference", path, "artifact producer is not declared")
            return False
        if producer_info.index >= context.top_index:
            emit_reference_error(
                "unreachable_dependency",
                path,
                "artifact producer must be declared before its consumer",
            )
            return False
        return True

    def resolve_artifact_reference(
        source: str,
        *,
        context: _NormalizationContext,
        path: str,
        allow_item_artifact: bool = True,
    ) -> ArtifactReference | None:
        payload, expected = _normalization_expected_cardinality(source)
        syntax: Literal["artifact", "item-artifact"] | None = None
        if payload.startswith("artifact:"):
            syntax = "artifact"
            payload = payload[len("artifact:") :]
        elif payload.startswith("item-artifact:"):
            syntax = "item-artifact"
            payload = payload[len("item-artifact:") :]
        else:
            emit_reference_error(
                "undefined_reference",
                path,
                "artifact reference must start with 'artifact:' or 'item-artifact:'",
            )
            return None

        if syntax == "item-artifact":
            if not allow_item_artifact or context.kind != "body" or context.loop_id is None:
                emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "item-artifact reference is only valid inside its owning loop body",
                )
                return None
            if payload.startswith("nodes/") or ".body." in payload:
                parsed = _normalization_reference_parts(payload)
                if parsed is None or parsed[0] != "body" or ":" not in parsed[1]:
                    emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "item-artifact reference must identify a step in the current loop body",
                    )
                    return None
                owner, body_id = parsed[1].split(":", 1)
                if owner != context.loop_id:
                    emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "item-artifact reference belongs to a different loop",
                    )
                    return None
                output_id = parsed[2]
                if output_id is None:
                    emit_reference_error(
                        "undefined_reference",
                        path,
                        "item-artifact output is not declared",
                    )
                    return None
                key = find_body_output(owner, body_id, output_id)
                if key is None:
                    emit_reference_error(
                        "undefined_reference",
                        path,
                        "item-artifact producer or output is not declared",
                    )
                    return None
                if not validate_cardinality(key, expected, path):
                    return None
                # Preserve a known edge after reporting an ordering/scope
                # problem so the graph pass can also identify cycles formed
                # through artifact references. No plan is returned while the
                # reachability diagnostic remains present.
                check_reachability(key, context, path)
                return ArtifactReference(source=source, key=key, expected_cardinality=expected)
            if "." not in payload:
                emit_reference_error(
                    "undefined_reference",
                    path,
                    "item-artifact reference must name step and output",
                )
                return None
            body_id, output_id = payload.split(".", 1)
            if (
                not _normalization_identifier(body_id)
                or not _normalization_identifier(output_id)
            ):
                emit_reference_error(
                    "undefined_reference",
                    path,
                    "item-artifact reference is not well formed",
                )
                return None
            key = find_body_output(context.loop_id, body_id, output_id)
            if key is None:
                emit_reference_error(
                    "undefined_reference",
                    path,
                    "item-artifact producer or output is not declared",
                )
                return None
            if not validate_cardinality(key, expected, path):
                return None
            check_reachability(key, context, path)
            return ArtifactReference(source=source, key=key, expected_cardinality=expected)

        parsed = _normalization_reference_parts(payload)
        if parsed is None:
            emit_reference_error("undefined_reference", path, "artifact reference is not well formed")
            return None
        kind, node_or_body, output_id = parsed
        key: ArtifactKey | None = None
        if kind == "body":
            if ":" not in node_or_body:
                emit_reference_error("undefined_reference", path, "body artifact reference is not well formed")
                return None
            loop_id, body_id = node_or_body.split(":", 1)
            key = find_body_output(loop_id, body_id, output_id or "")
            if key is not None:
                emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "loop-body output must be exported before it can cross the loop boundary",
                )
                emit_reference_error(
                    "cardinality_mismatch",
                    path,
                    "loop-body scalar output cannot be used as a workflow-scoped artifact",
                )
                return None
            emit_reference_error(
                "undefined_reference",
                path,
                "body artifact producer or output is not declared",
            )
            return None
        if kind == "short":
            key, ambiguous = find_short_output(node_or_body)
            if ambiguous:
                emit_reference_error("ambiguous_reference", path, "short artifact reference names multiple outputs")
                return None
            if key is None:
                body_matches = [
                    body_key
                    for values in body_output_short.values()
                    for _loop_id, body_key in values
                    if body_key.output_id == node_or_body
                ]
                if body_matches:
                    emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "loop-body output must be referenced through an explicit collection export",
                    )
                else:
                    emit_reference_error("undefined_reference", path, "artifact output is not declared")
                return None
        else:
            if (
                output_id is None
                or not _normalization_identifier(node_or_body)
                or not _normalization_identifier(output_id)
            ):
                emit_reference_error(
                    "undefined_reference",
                    path,
                    "artifact reference is not well formed",
                )
                return None
            key = find_top_output(node_or_body, output_id)
            if key is None:
                if any(
                    body_step_id == node_or_body and body_output == output_id
                    for (_loop_id, body_step_id, body_output), _body_key in body_output_keys.items()
                ):
                    emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "loop-body output must be referenced through an explicit collection export",
                    )
                    return None
                if node_or_body in body_infos:
                    emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "loop-body output must be referenced through an explicit collection export",
                    )
                else:
                    emit_reference_error("undefined_reference", path, "artifact producer or output is not declared")
                return None

        assert key is not None
        if not validate_cardinality(key, expected, path):
            return None
        check_reachability(key, context, path)
        return ArtifactReference(source=source, key=key, expected_cardinality=expected)

    def resolve_body_export_source(
        loop_id: str,
        source: str,
        path: str,
    ) -> ArtifactReference | None:
        payload, expected = _normalization_expected_cardinality(source)
        key: ArtifactKey | None = None
        if payload.startswith("item-artifact:"):
            payload = payload[len("item-artifact:") :]
        elif payload.startswith("artifact:"):
            # An artifact prefix is accepted only when it still identifies a
            # body output; it can never turn an item scalar into a workflow
            # scalar implicitly.
            payload = payload[len("artifact:") :]
        if payload.startswith("nodes/"):
            parsed = _normalization_reference_parts(payload)
            if parsed is None or parsed[0] != "body":
                emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "collection export source must identify a step in this loop body",
                )
                return None
            owner, body_id = parsed[1].split(":", 1)
            if owner != loop_id:
                emit_reference_error("cross_scope_reference", path, "collection export source belongs to another loop")
                return None
            output_id = parsed[2]
        else:
            dotted_parts = payload.split(".")
            if len(dotted_parts) == 4 and dotted_parts[1] == "body":
                owner, body_id, output_id = dotted_parts[0], dotted_parts[2], dotted_parts[3]
                if owner != loop_id:
                    emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "collection export source belongs to another loop",
                    )
                    return None
            elif "." in payload:
                body_id, output_id = payload.split(".", 1)
            else:
                body_id, output_id = "", payload
            if body_id and not _normalization_identifier(body_id):
                emit_reference_error("undefined_reference", path, "collection export source is not well formed")
                return None
            if not _normalization_identifier(output_id):
                emit_reference_error("undefined_reference", path, "collection export source is not well formed")
                return None
            if not body_id:
                candidates = [
                    key
                    for (candidate_step, candidate_output), values in body_output_short.items()
                    if candidate_output == output_id
                    for candidate_loop, key in values
                    if candidate_loop == loop_id
                ]
                unique = tuple(dict.fromkeys(candidates))
                if len(unique) != 1:
                    code = "ambiguous_reference" if len(unique) > 1 else "undefined_reference"
                    emit_reference_error(code, path, "collection export output is not uniquely declared")
                    return None
                key = unique[0]
            else:
                key = find_body_output(loop_id, body_id, output_id)
        if key is None:
            key = find_body_output(loop_id, body_id, output_id)
        if key is None:
            emit_reference_error(
                "undefined_reference",
                path,
                "collection export source is not declared in this loop body",
            )
            return None
        if key.cardinality != "scalar":
            emit_reference_error("cardinality_mismatch", path, "collection export source must be an item scalar")
            return None
        if expected is not None and expected != "scalar":
            emit_reference_error("cardinality_mismatch", path, "collection export source must be scalar")
            return None
        return ArtifactReference(source=source, key=key, expected_cardinality="scalar")

    def resolve_explicit_dependencies(
        raw_dependencies: tuple[str, ...],
        *,
        context: _NormalizationContext,
        path: str,
    ) -> tuple[str, ...]:
        resolved: list[str] = []
        seen: set[str] = set()
        sibling_map = top_infos if context.kind == "top" else body_infos.get(context.loop_id or "", {})
        for dependency_index, dependency in enumerate(raw_dependencies):
            dependency_path = f"{path}/{dependency_index}"
            if dependency in seen:
                add_duplicate(dependency_path, "dependency", dependency)
                continue
            seen.add(dependency)
            target = sibling_map.get(dependency)
            if target is None:
                other_scope = (
                    any(dependency in mapping for mapping in body_infos.values())
                    if context.kind == "top"
                    else dependency in top_infos
                    or any(
                        dependency in mapping
                        for loop_id, mapping in body_infos.items()
                        if loop_id != context.loop_id
                    )
                )
                emit_reference_error(
                    "cross_scope_reference" if other_scope else "undefined_reference",
                    dependency_path,
                    "dependency must reference a node in the same container",
                )
                continue
            if context.kind == "top":
                if target.index >= context.top_index:
                    emit_reference_error(
                        "unreachable_dependency",
                        dependency_path,
                        "dependency must be declared before its consumer",
                    )
                    # Keep the edge in the temporary graph so a mutual
                    # forward reference is also diagnosed as a cycle. The
                    # plan is discarded below because any diagnostic is
                    # fatal; retaining it here is only for complete static
                    # diagnostics and never causes automatic reordering.
                    resolved.append(target.canonical_id)
                    continue
            elif context.body_index is None or target.index >= context.body_index:
                emit_reference_error(
                    "unreachable_dependency",
                    dependency_path,
                    "dependency must be declared before its consumer in the loop body",
                )
                resolved.append(target.canonical_id)
                continue
            resolved.append(target.canonical_id)
        return tuple(resolved)

    def normalize_step(
        info: _NormalizationNodeInfo,
        *,
        context: _NormalizationContext,
        path: str,
    ) -> StepPlan:
        step = info.config
        assert isinstance(step, StepConfig)
        explicit = resolve_explicit_dependencies(
            step.depends_on,
            context=context,
            path=f"{path}/depends_on",
        )
        inputs: list[InputBindingPlan] = []
        implicit: list[str] = []
        input_names: set[str] = set()
        for input_index, binding in enumerate(step.inputs):
            input_path = f"{path}/inputs/{input_index}"
            if not isinstance(binding, InputBinding):
                issues.add("wrong_type", input_path, "input is not an InputBinding")
                continue
            if binding.name in input_names:
                add_duplicate(f"{input_path}/name", "input name", binding.name)
                continue
            input_names.add(binding.name)
            if binding.source.startswith("$"):
                if binding.source == "$loop_item" and context.kind != "body":
                    emit_reference_error(
                        "cross_scope_reference",
                        f"{input_path}/from",
                        "$loop_item is only available inside a loop body",
                    )
                inputs.append(
                    InputBindingPlan(
                        name=binding.name,
                        source=binding.source,
                        virtual_input=binding.source,
                    )
                )
                continue
            reference = resolve_artifact_reference(
                binding.source,
                context=context,
                path=f"{input_path}/from",
            )
            if reference is None:
                continue
            inputs.append(
                InputBindingPlan(
                    name=binding.name,
                    source=binding.source,
                    artifact=reference,
                )
            )
            producer = reference.key.producer_node_id
            if producer not in implicit:
                implicit.append(producer)
        dependencies = list(explicit)
        for dependency in implicit:
            if dependency not in dependencies:
                dependencies.append(dependency)
        output_plans: list[OutputPlan] = []
        loop_id = context.loop_id
        for output in step.outputs:
            key = (
                body_output_keys.get((loop_id or "", info.local_id, output.id))
                if loop_id is not None
                else top_output_keys.get((info.local_id, output.id))
            )
            if key is not None:
                output_plans.append(
                    OutputPlan(id=output.id, kind=output.kind, path=output.path, artifact_key=key)
                )
        return StepPlan(
            canonical_id=info.canonical_id,
            local_id=info.local_id,
            lifecycle=step.lifecycle,
            runner=step.runner,
            prompt=step.prompt,
            skill=step.skill,
            inputs=tuple(inputs),
            outputs=tuple(output_plans),
            dependencies=tuple(dependencies),
            explicit_dependencies=explicit,
        )

    normalized_nodes: list[PlanNode] = []

    for info in top_order:
        path = f"/nodes/{info.index}"
        if isinstance(info.config, StepConfig):
            context = _NormalizationContext(kind="top", top_index=info.index)
            plan = normalize_step(info, context=context, path=path)
            normalized_nodes.append(plan)
            continue

        loop = info.config
        source_reference: ArtifactReference | None = None
        source_virtual: str | None = None
        if loop.source.source.startswith("$"):
            source_virtual = loop.source.source
        else:
            source_reference = resolve_artifact_reference(
                loop.source.source,
                context=_NormalizationContext(kind="top", top_index=info.index),
                path=f"{path}/source/from",
                allow_item_artifact=False,
            )
        source_binding = LoopSourceBinding(
            source=loop.source.source,
            provider=loop.source.provider,
            artifact=source_reference,
            virtual_input=source_virtual,
        )
        loop_dependencies: list[str] = []
        if source_reference is not None:
            loop_dependencies.append(source_reference.key.producer_node_id)

        body_plans: list[StepPlan] = []
        for body_info in body_order.get(info.local_id, ()):
            body_plan = normalize_step(
                body_info,
                context=_NormalizationContext(
                    kind="body",
                    top_index=info.index,
                    loop_id=info.local_id,
                    body_index=body_info.index,
                ),
                path=f"{path}/body/{body_info.index}",
            )
            body_plans.append(body_plan)
            for input_binding in body_plan.inputs:
                if (
                    input_binding.artifact is not None
                    and input_binding.artifact.key.scope == "workflow"
                    and input_binding.artifact.key.producer_node_id not in loop_dependencies
                ):
                    loop_dependencies.append(input_binding.artifact.key.producer_node_id)

        export_plans: list[CollectionExportPlan] = []
        for export_index, export in enumerate(loop.exports):
            export_key = export_keys.get((info.local_id, export.id))
            if export_key is None:
                continue
            source_reference_for_export = resolve_body_export_source(
                info.local_id,
                export.source,
                f"{path}/exports/{export_index}/from",
            )
            if source_reference_for_export is None:
                continue
            export_plans.append(
                CollectionExportPlan(
                    id=export.id,
                    source=export.source,
                    cardinality="collection",
                    source_artifact=source_reference_for_export.key,
                    artifact_key=export_key,
                )
            )

        loop_plan = LoopPlan(
            canonical_id=info.canonical_id,
            local_id=info.local_id,
            source=source_binding,
            max_items=loop.max_items,
            controller=loop.controller,
            body=tuple(body_plans),
            exports=tuple(export_plans),
            dependencies=tuple(loop_dependencies),
        )
        normalized_nodes.append(loop_plan)

    dependency_graph: dict[str, tuple[str, ...]] = {}
    for node in normalized_nodes:
        dependency_graph[node.canonical_id] = node.dependencies
        if isinstance(node, LoopPlan):
            for body_step in node.body:
                dependency_graph[body_step.canonical_id] = body_step.dependencies

    # A graph pass remains useful even though declaration-order checks above
    # reject forward edges: it gives callers a stable cycle diagnostic for
    # manually constructed DTOs and makes the no-scheduler contract explicit.
    visit_state: dict[str, int] = {}
    cycle_signatures: set[tuple[str, ...]] = set()

    def visit(node_id: str, stack: list[str]) -> None:
        state = visit_state.get(node_id, 0)
        if state == 2:
            return
        if state == 1:
            try:
                start = stack.index(node_id)
            except ValueError:
                start = 0
            cycle = tuple(stack[start:] + [node_id])
            signature = tuple(sorted(set(cycle)))
            if signature not in cycle_signatures:
                cycle_signatures.add(signature)
                issues.add(
                    "dependency_cycle",
                    "/nodes",
                    "dependency cycle: " + " -> ".join(cycle),
                )
            return
        visit_state[node_id] = 1
        stack.append(node_id)
        for dependency in dependency_graph.get(node_id, ()):
            if dependency in dependency_graph:
                visit(dependency, stack)
        stack.pop()
        visit_state[node_id] = 2

    for node_id in dependency_graph:
        visit(node_id, [])

    issues.raise_if_any(source_path=source_path_value)
    return WorkflowPlan(
        schema_version=config.schema_version,
        workflow_id=config.id,
        profile=config.profile,
        limits=config.limits,
        nodes=tuple(normalized_nodes),
        dependency_graph=dependency_graph,
        artifact_graph=artifact_graph,
    )


class WorkflowNormalizer:
    """Object boundary for callers that prefer an explicit normalizer."""

    def normalize(
        self,
        config: WorkflowConfig,
        *,
        source_path: Path | str | None = None,
    ) -> WorkflowPlan:
        return normalize_workflow_config(config, source_path=source_path)

    normalise = normalize


def normalize_workflow(
    config: WorkflowConfig,
    *,
    source_path: Path | str | None = None,
) -> WorkflowPlan:
    return normalize_workflow_config(config, source_path=source_path)


normalise_workflow_config = normalize_workflow_config
build_workflow_plan = normalize_workflow_config


def _artifact_snapshot_mapping(
    source_snapshots: WorkflowBoundsResult | Mapping[str, LoopSourceSnapshot] | None,
    *,
    plan: WorkflowPlan,
) -> Mapping[str, LoopSourceSnapshot]:
    if source_snapshots is None:
        return {}
    if isinstance(source_snapshots, WorkflowBoundsResult):
        if source_snapshots.plan.workflow_id != plan.workflow_id:
            raise ValueError("artifact namespace snapshots belong to another workflow")
        return source_snapshots.snapshots
    if isinstance(source_snapshots, Mapping):
        return source_snapshots
    raise TypeError(
        "source_snapshots must be a WorkflowBoundsResult, mapping, or None"
    )


def _item_scope_path(
    loop: LoopPlan,
    item: LoopItem,
    body_step: StepPlan,
    *,
    item_namespace: Literal["default", "work-items"],
    output_path: str | None = None,
) -> str:
    if item_namespace == "default":
        return (
            f"loops/{loop.local_id}/items/{item.item_id}"
            f"/steps/{body_step.local_id}"
        )
    if item_namespace == "work-items":
        # The legacy implementation subpipeline owns a role/iteration scope
        # such as ``work-items/WB-01/iterations/0000/coder``.  A standard
        # workflow can preserve that contract by placing the scope prefix in
        # the declared output path.  Keep the original step-directory rule
        # for the simpler one-segment form used by generic workflows.
        if output_path is not None:
            output_parts = _portable_artifact_path_parts(
                output_path,
                field_name="output path",
            )
            if len(output_parts) > 1:
                return f"work-items/{item.item_id}/{'/'.join(output_parts[:-1])}"
        return f"work-items/{item.item_id}/steps/{body_step.local_id}"
    raise ValueError("item_namespace must be 'default' or 'work-items'")


def _join_artifact_relative_path(scope: str, output_path: str) -> str:
    _portable_artifact_path_parts(output_path, field_name="output path")
    if not scope:
        return output_path
    _portable_artifact_path_parts(scope, field_name="artifact scope")
    return f"{scope}/{output_path}"


def build_artifact_namespace_plan(
    plan: WorkflowPlan | WorkflowConfig,
    source_snapshots: WorkflowBoundsResult | Mapping[str, LoopSourceSnapshot] | None = None,
    *,
    artifact_root: Path | str | None = None,
    item_namespace: Literal["default", "work-items"] = "default",
) -> ArtifactNamespacePlan:
    """Precompute collision-free physical paths for every declared output.

    Top-level outputs retain the existing artifact-root-relative contract.
    Loop outputs use a stable item identifier and a step directory; the source
    position is recorded as metadata only and never participates in the path.
    The compatibility ``work-items`` namespace also accepts a multi-segment
    output declaration whose parent segments name the legacy item scope.
    No directory, lock, or manifest is created by this function.
    """

    if isinstance(plan, WorkflowConfig):
        plan = normalize_workflow_config(plan)
    if not isinstance(plan, WorkflowPlan):
        raise TypeError("plan must be a WorkflowPlan or WorkflowConfig")
    if item_namespace not in {"default", "work-items"}:
        raise ValueError("item_namespace must be 'default' or 'work-items'")

    snapshots = _artifact_snapshot_mapping(source_snapshots, plan=plan)
    issues = _IssueCollector()
    entries: list[ArtifactNamespace] = []
    collection_exports: list[ArtifactNamespace] = []
    normalized_paths: dict[str, ArtifactNamespace] = {}
    root_guard: ArtifactPathGuard | None = None
    if artifact_root is not None:
        root_guard = ArtifactPathGuard(artifact_root)
        try:
            root_guard.validate_root(require_directory=True)
        except (ArtifactPathSafetyError, OSError, RuntimeError) as exc:
            issues.add("unsafe_path", "", str(exc))

    def add_namespace(
        namespace: ArtifactNamespace,
        *,
        config_path: str,
        export: bool = False,
    ) -> None:
        try:
            normalized = namespace.normalized_path
        except (TypeError, ValueError) as exc:
            issues.add("unsafe_path", config_path, str(exc))
            return
        if root_guard is not None:
            try:
                root_guard.validate(namespace.relative_path)
            except (ArtifactPathSafetyError, OSError, RuntimeError) as exc:
                issues.add("unsafe_path", config_path, str(exc))
                return
        previous = normalized_paths.get(normalized)
        if previous is not None:
            issues.add(
                "namespace_collision",
                config_path,
                f"artifact namespace collides with {previous.relative_path!r}",
            )
        else:
            for previous_key, previous_namespace in normalized_paths.items():
                if normalized.startswith(previous_key + "/") or previous_key.startswith(normalized + "/"):
                    issues.add(
                        "namespace_collision",
                        config_path,
                        f"artifact namespace overlaps {previous_namespace.relative_path!r}",
                    )
                    break
        normalized_paths.setdefault(normalized, namespace)
        (collection_exports if export else entries).append(namespace)

    for node_index, node in enumerate(plan.nodes):
        node_path = f"/nodes/{node_index}"
        if isinstance(node, StepPlan):
            for output_index, output in enumerate(node.outputs):
                try:
                    relative_path = _join_artifact_relative_path("", output.path)
                    namespace = ArtifactNamespace(
                        artifact_key=output.artifact_key,
                        node_instance_id=node.canonical_id,
                        relative_path=relative_path,
                        scope_relative_path="",
                        kind=output.kind,  # type: ignore[arg-type]
                    )
                except (TypeError, ValueError) as exc:
                    issues.add("unsafe_path", f"{node_path}/outputs/{output_index}/path", str(exc))
                    continue
                add_namespace(
                    namespace,
                    config_path=f"{node_path}/outputs/{output_index}/path",
                )
            continue

        if not isinstance(node, LoopPlan):
            issues.add("wrong_type", node_path, "workflow plan contains an unsupported node")
            continue
        snapshot = snapshots.get(node.local_id)
        if snapshot is None:
            issues.add(
                "source_snapshot_required",
                f"{node_path}/source",
                "loop source snapshot is required before artifact namespaces are computed",
            )
            continue
        if not isinstance(snapshot, LoopSourceSnapshot):
            issues.add(
                "wrong_type",
                f"{node_path}/source",
                "loop source snapshot must be a LoopSourceSnapshot",
            )
            continue
        for item_index, item in enumerate(snapshot.items):
            if not isinstance(item, LoopItem):
                issues.add(
                    "wrong_type",
                    f"{node_path}/source/items/{item_index}",
                    "loop source snapshot contains an invalid item",
                )
                continue
            for body_index, body_step in enumerate(node.body):
                for output_index, output in enumerate(body_step.outputs):
                    config_path = (
                        f"{node_path}/body/{body_index}/outputs/{output_index}/path"
                    )
                    try:
                        output_parts = _portable_artifact_path_parts(
                            output.path,
                            field_name="output path",
                        )
                        scope = _item_scope_path(
                            node,
                            item,
                            body_step,
                            item_namespace=item_namespace,
                            output_path=output.path,
                        )
                        scope_relative_output = (
                            output_parts[-1]
                            if item_namespace == "work-items" and len(output_parts) > 1
                            else output.path
                        )
                        relative_path = _join_artifact_relative_path(
                            scope,
                            scope_relative_output,
                        )
                        namespace = ArtifactNamespace(
                            artifact_key=output.artifact_key,
                            node_instance_id=f"{body_step.canonical_id}@{item.item_id}",
                            relative_path=relative_path,
                            scope_relative_path=scope,
                            kind=output.kind,  # type: ignore[arg-type]
                            item_id=item.item_id,
                            position=item.position,
                        )
                    except (TypeError, ValueError) as exc:
                        issues.add("unsafe_path", config_path, str(exc))
                        continue
                    add_namespace(namespace, config_path=config_path)

        for export_index, export in enumerate(node.exports):
            config_path = f"{node_path}/exports/{export_index}"
            try:
                scope = f"loops/{node.local_id}/exports/{export.id}"
                relative_path = _join_artifact_relative_path(scope, "manifest.json")
                namespace = ArtifactNamespace(
                    artifact_key=export.artifact_key,
                    node_instance_id=f"{node.canonical_id}@collection",
                    relative_path=relative_path,
                    scope_relative_path=scope,
                    kind="file",
                    is_export=True,
                )
            except (TypeError, ValueError) as exc:
                issues.add("unsafe_path", config_path, str(exc))
                continue
            add_namespace(namespace, config_path=config_path, export=True)

    issues.raise_if_any()
    root_value = Path(artifact_root).absolute() if artifact_root is not None else None
    return ArtifactNamespacePlan(
        entries=tuple(entries),
        collection_exports=tuple(collection_exports),
        artifact_root=root_value,
        path_index={key: value.relative_path for key, value in normalized_paths.items()},
    )


precompute_artifact_namespaces = build_artifact_namespace_plan
validate_artifact_namespaces = build_artifact_namespace_plan
build_artifact_namespaces = build_artifact_namespace_plan


def _namespace_identity(namespace: ArtifactNamespace) -> tuple[str, str, str | None]:
    return (namespace.node_instance_id, namespace.output_id, namespace.item_id)


def _manifest_identity(entry: ArtifactManifestEntry) -> tuple[str, str, str | None]:
    return (entry.node_instance_id, entry.output_id, entry.item_id)


def _expected_namespace_tuple(
    expected_namespaces: ArtifactNamespacePlan | Iterable[ArtifactNamespace],
) -> tuple[ArtifactNamespace, ...]:
    if isinstance(expected_namespaces, ArtifactNamespacePlan):
        return expected_namespaces.all_entries
    result = tuple(expected_namespaces)
    if any(not isinstance(item, ArtifactNamespace) for item in result):
        raise TypeError("expected_namespaces must contain ArtifactNamespace values")
    return result


def validate_artifact_manifest(
    artifact_root: Path | str,
    manifest: ArtifactManifest | Mapping[str, object],
    *,
    expected_run_identity: str | None = None,
    expected_namespaces: ArtifactNamespacePlan | Iterable[ArtifactNamespace] | None = None,
    max_bytes: int | None = None,
) -> ArtifactManifest:
    """Validate a manifest against the current producer paths and contents."""

    if isinstance(manifest, Mapping):
        manifest = ArtifactManifest.from_dict(manifest)
    if not isinstance(manifest, ArtifactManifest):
        raise TypeError("manifest must be an ArtifactManifest or mapping")
    expected_identity = (
        manifest.run_identity
        if expected_run_identity is None
        else expected_run_identity
    )
    if manifest.run_identity != expected_identity:
        raise ArtifactOutputValidationError("artifact manifest run identity mismatch")

    guard = ArtifactPathGuard(artifact_root)
    guard.validate_root(require_directory=True)
    expected: tuple[ArtifactNamespace, ...] | None = None
    expected_by_identity: dict[tuple[str, str, str | None], ArtifactNamespace] = {}
    if expected_namespaces is not None:
        expected = _expected_namespace_tuple(expected_namespaces)
        for namespace in expected:
            identity = _namespace_identity(namespace)
            if identity in expected_by_identity:
                raise ArtifactOutputValidationError("duplicate expected artifact output identity")
            expected_by_identity[identity] = namespace

    errors: list[str] = []
    manifest_identities = {_manifest_identity(entry) for entry in manifest.entries}
    if expected is not None:
        expected_identities = set(expected_by_identity)
        missing = expected_identities - manifest_identities
        unexpected = manifest_identities - expected_identities
        if missing:
            errors.append("artifact manifest is missing required output identities")
        if unexpected:
            errors.append("artifact manifest contains unexpected output identities")

    for entry in manifest.entries:
        namespace = expected_by_identity.get(_manifest_identity(entry))
        if expected is not None and namespace is None:
            continue
        try:
            guard.validate(entry.relative_path)
            current = fingerprint_artifact(
                guard.root,
                entry.relative_path,
                max_bytes=max_bytes,
            )
            if namespace is not None:
                if (
                    entry.producer_node_id != namespace.producer_node_id
                    or entry.scope != namespace.scope
                    or entry.cardinality != namespace.cardinality
                    or entry.kind != namespace.kind
                    or entry.relative_path != namespace.relative_path
                    or entry.item_position != namespace.position
                ):
                    raise ArtifactOutputValidationError(
                        f"artifact manifest identity does not match declared namespace: "
                        f"{entry.relative_path}"
                    )
                current_entry = ArtifactManifestEntry.from_namespace(
                    namespace,
                    run_identity=manifest.run_identity,
                    fingerprint=current,
                )
            else:
                current_entry = ArtifactManifestEntry(
                    run_identity=manifest.run_identity,
                    node_instance_id=entry.node_instance_id,
                    producer_node_id=entry.producer_node_id,
                    output_id=entry.output_id,
                    scope=entry.scope,
                    cardinality=entry.cardinality,
                    kind=current.kind,
                    relative_path=entry.relative_path,
                    item_id=entry.item_id,
                    item_position=entry.item_position,
                    size_bytes=current.size_bytes,
                    sha256=current.sha256,
                    device=current.device,
                    inode=current.inode,
                    mtime_ns=current.mtime_ns,
                    freshness=_artifact_freshness_token(
                        run_identity=manifest.run_identity,
                        node_instance_id=entry.node_instance_id,
                        relative_path=entry.relative_path,
                        fingerprint=current,
                    ),
                )
            if current_entry.kind != entry.kind:
                raise ArtifactOutputValidationError(
                    f"artifact manifest kind mismatch: {entry.relative_path}"
                )
            if current_entry.to_dict() != entry.to_dict():
                raise ArtifactOutputValidationError(
                    f"artifact manifest is stale for output: {entry.relative_path}"
                )
        except (ArtifactPathSafetyError, ArtifactOutputValidationError, OSError, ValueError) as exc:
            errors.append(str(exc))

    if errors:
        raise ArtifactOutputValidationError("; ".join(errors))
    return manifest


validate_output_manifest = validate_artifact_manifest
validate_required_output_manifest = validate_artifact_manifest


class _LoopSourceInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _canonical_source_value(
    value: object,
    *,
    depth: int,
    max_depth: int,
) -> object:
    """Copy a provider value into a bounded JSON-compatible value."""

    if depth > max_depth:
        raise _LoopSourceInputError(
            "resource_limit_exceeded",
            f"loop source item nesting exceeds {max_depth} levels",
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _LoopSourceInputError(
                "invalid_loop_item",
                "loop source items must not contain non-finite numbers",
            )
        return value
    if isinstance(value, Mapping):
        duplicate_keys = getattr(value, "duplicate_keys", ())
        if duplicate_keys:
            raise _LoopSourceInputError(
                "duplicate_key",
                f"loop source item contains duplicate key {duplicate_keys[0]!r}",
            )
        result: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise _LoopSourceInputError(
                    "invalid_loop_item",
                    "loop source item object keys must be strings",
                )
            result[key] = _canonical_source_value(
                nested,
                depth=depth + 1,
                max_depth=max_depth,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _canonical_source_value(
                nested,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for nested in value
        ]
    raise _LoopSourceInputError(
        "invalid_loop_item",
        "loop source items must contain only JSON-compatible values",
    )


def _canonical_source_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _LoopSourceInputError(
            "invalid_loop_item",
            "loop source items could not be canonically encoded",
        ) from exc


def _decode_loop_source_json(
    source: bytes,
    *,
    limits: WorkflowEffectiveLimits,
) -> object:
    if len(source) > limits.max_snapshot_bytes:
        raise _LoopSourceInputError(
            "resource_limit_exceeded",
            f"loop source payload exceeds {limits.max_snapshot_bytes} bytes",
        )
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _LoopSourceInputError(
            "invalid_loop_source",
            "loop source payload must be valid UTF-8 JSON",
        ) from exc
    if _scan_json_depth(source) > limits.max_json_depth:
        raise _LoopSourceInputError(
            "resource_limit_exceeded",
            f"loop source nesting exceeds {limits.max_json_depth} levels",
        )
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise _LoopSourceInputError(
            "invalid_loop_source",
            "loop source payload must be valid JSON",
        ) from exc


def _extract_loop_source_items(
    raw: object,
    *,
    limits: WorkflowEffectiveLimits,
) -> Iterable[object]:
    if isinstance(raw, LoopSourceSnapshot):
        return (item.payload for item in raw.items)
    if isinstance(raw, str):
        raw = _decode_loop_source_json(raw.encode("utf-8"), limits=limits)
    elif isinstance(raw, (bytes, bytearray)):
        raw = _decode_loop_source_json(bytes(raw), limits=limits)

    if isinstance(raw, Mapping):
        if getattr(raw, "duplicate_keys", ()):
            duplicate = raw.duplicate_keys[0]
            raise _LoopSourceInputError(
                "duplicate_key",
                f"loop source payload contains duplicate key {duplicate!r}",
            )
        if "items" in raw:
            raw = raw["items"]
        elif "tasks" in raw:
            # ``work_items.json`` is the first built-in source contract.  The
            # provider still owns this shape; accepting it here only keeps
            # the generic snapshot boundary useful for the standard source.
            raw = raw["tasks"]
        elif "id" in raw:
            raw = (raw,)
        else:
            raise _LoopSourceInputError(
                "invalid_loop_source",
                "loop source must contain an items or tasks collection",
            )
    if isinstance(raw, (set, frozenset)):
        raise _LoopSourceInputError(
            "invalid_loop_source",
            "loop source must be an ordered finite collection",
        )
    if isinstance(raw, (str, bytes, bytearray)) or isinstance(raw, Mapping):
        raise _LoopSourceInputError(
            "invalid_loop_source",
            "loop source must be an ordered finite collection",
        )
    try:
        return iter(raw)  # type: ignore[arg-type]
    except TypeError as exc:
        raise _LoopSourceInputError(
            "invalid_loop_source",
            "loop source must be an ordered finite collection",
        ) from exc


def _invoke_loop_source_provider(
    provider: object,
    binding: LoopSourceBinding,
    limits: WorkflowEffectiveLimits,
) -> object:
    """Invoke one trusted provider exactly once.

    The public protocol uses ``snapshot(binding, limits)``.  ``read`` and
    ``load`` are accepted as small adapters for existing source providers,
    while signature inspection avoids retrying a provider after an internal
    ``TypeError`` (which could read a mutable source twice).
    """

    operation: Callable[..., object] | None = None
    for method_name in ("snapshot", "read", "load"):
        candidate = getattr(provider, method_name, None)
        if callable(candidate):
            operation = candidate
            break
    if operation is None and callable(provider):
        operation = provider  # type: ignore[assignment]
    if operation is None:
        return provider

    try:
        signature = inspect.signature(operation)
    except (TypeError, ValueError):
        return operation(binding)

    parameters = tuple(signature.parameters.values())
    positional = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    if any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return operation(binding, limits)
    limits_parameter = signature.parameters.get("limits")
    if limits_parameter is not None and limits_parameter.kind == inspect.Parameter.KEYWORD_ONLY:
        return operation(binding, limits=limits)
    if len(positional) >= 2:
        return operation(binding, limits)
    if len(positional) == 1:
        return operation(binding)
    return operation()


def _snapshot_loop_source_payload(
    loop: LoopPlan,
    raw: object,
    *,
    limits: WorkflowEffectiveLimits,
    issues: _IssueCollector,
    config_path: str,
) -> LoopSourceSnapshot | None:
    try:
        source_items = _extract_loop_source_items(raw, limits=limits)
        iterator = iter(source_items)
    except _LoopSourceInputError as exc:
        issues.add(exc.code, f"{config_path}/source/from", exc.message)
        return None
    except Exception:
        issues.add(
            "invalid_loop_source",
            f"{config_path}/source/from",
            "loop source could not be iterated",
        )
        return None

    max_items = min(loop.max_items, limits.max_loop_items)
    items: list[LoopItem] = []
    seen_ids: set[str] = set()
    item_bytes_total = 0
    index = 0
    while True:
        try:
            raw_item = next(iterator)
        except StopIteration:
            break
        except Exception:
            issues.add(
                "loop_source_read_error",
                f"{config_path}/source/from",
                "loop source failed while being read",
            )
            return None

        if index >= max_items:
            issues.add(
                "resource_limit_exceeded",
                f"{config_path}/max_items",
                f"loop source contains more than the allowed {max_items} items",
            )
            break
        item_path = f"{config_path}/source/items/{index}"
        try:
            normalized = _canonical_source_value(
                raw_item,
                depth=1,
                max_depth=limits.max_json_depth,
            )
        except _LoopSourceInputError as exc:
            issues.add(exc.code, item_path, exc.message)
            index += 1
            continue
        if not isinstance(normalized, dict):
            issues.add(
                "invalid_loop_item",
                item_path,
                "each loop source item must be an object",
            )
            index += 1
            continue

        item_id = normalized.get("id", _MISSING)
        if item_id is _MISSING:
            issues.add(
                "invalid_loop_item",
                f"{item_path}/id",
                "each loop source item must have an id",
            )
            index += 1
            continue
        if not isinstance(item_id, str) or SAFE_IDENTIFIER_PATTERN.fullmatch(item_id) is None:
            issues.add(
                "unsafe_item_id",
                f"{item_path}/id",
                "loop item id must be a safe stable identifier",
            )
            index += 1
            continue
        if item_id in seen_ids:
            issues.add(
                "duplicate_item_id",
                f"{item_path}/id",
                f"duplicate loop item id {item_id!r}",
            )
        seen_ids.add(item_id)

        encoded_item = _canonical_source_bytes(normalized)
        item_size = len(encoded_item)
        if item_size > limits.max_item_bytes:
            issues.add(
                "resource_limit_exceeded",
                f"{item_path}",
                f"loop item exceeds {limits.max_item_bytes} bytes",
            )
        item_bytes_total += item_size
        items.append(
            LoopItem(
                item_id=item_id,
                position=index,
                payload=normalized,
                size_bytes=item_size,
            )
        )
        if item_bytes_total > limits.max_snapshot_bytes:
            issues.add(
                "resource_limit_exceeded",
                f"{config_path}/source/from",
                f"loop source items exceed {limits.max_snapshot_bytes} bytes",
            )
            break
        index += 1

    snapshot_payload = {
        "loop_id": loop.local_id,
        "provider": loop.source.provider,
        "source": loop.source.source,
        "items": [
            {
                "id": item.item_id,
                "position": item.position,
                "payload": _thaw_metadata_value(item.payload),
            }
            for item in items
        ],
    }
    encoded_snapshot = _canonical_source_bytes(snapshot_payload)
    if len(encoded_snapshot) > limits.max_snapshot_bytes:
        issues.add(
            "resource_limit_exceeded",
            f"{config_path}/source/from",
            f"loop source snapshot exceeds {limits.max_snapshot_bytes} bytes",
        )
    return LoopSourceSnapshot(
        loop_id=loop.local_id,
        provider=loop.source.provider,
        source=loop.source.source,
        items=tuple(items),
        digest=hashlib.sha256(encoded_snapshot).hexdigest(),
        size_bytes=len(encoded_snapshot),
        input_bytes=item_bytes_total,
    )


def _runtime_loop_source_provider(providers: object, provider_id: str) -> object:
    if providers is None:
        return _MISSING
    if isinstance(providers, Mapping):
        provider = providers.get(provider_id, _MISSING)
        return _MISSING if provider is None else provider
    provider_map = getattr(providers, "providers", _MISSING)
    if isinstance(provider_map, Mapping):
        provider = provider_map.get(provider_id, _MISSING)
        return _MISSING if provider is None else provider
    getter = getattr(providers, "get", None)
    if callable(getter):
        try:
            provider = getter(provider_id, _MISSING)
            return _MISSING if provider is None else provider
        except TypeError:
            # A one-argument ``get`` is still safe to call once.  A provider
            # with an internal TypeError is not retried by this branch.
            try:
                provider = getter(provider_id)
                return _MISSING if provider is None else provider
            except KeyError:
                return _MISSING
    if any(callable(getattr(providers, name, None)) for name in ("snapshot", "read", "load")):
        return providers
    if callable(providers):
        return providers
    return _MISSING


def _capability_snapshot_for_bounds(
    registry: CapabilityRegistry | CapabilityRegistrySnapshot | None,
    *,
    profile: str,
) -> CapabilityRegistrySnapshot | None:
    if registry is None:
        return None
    if isinstance(registry, CapabilityRegistry):
        return registry.snapshot(profile)
    if not isinstance(registry, CapabilityRegistrySnapshot):
        raise TypeError("registry must be a CapabilityRegistry or CapabilityRegistrySnapshot")
    if registry.profile != profile:
        raise ValueError("capability registry snapshot profile does not match workflow profile")
    return registry


def _add_bound_issue(
    issues: _IssueCollector,
    *,
    actual: int,
    limit: int,
    path: str,
    label: str,
) -> None:
    if actual > limit:
        issues.add(
            "resource_limit_exceeded",
            path,
            f"{label} {actual} exceeds the allowed {limit}",
        )


def snapshot_loop_source(
    loop: LoopPlan,
    provider: object,
    *,
    limits: WorkflowEffectiveLimits | None = None,
    hard_limits: WorkflowHardLimits | None = None,
) -> LoopSourceSnapshot:
    """Read and freeze one loop source exactly once."""

    if not isinstance(loop, LoopPlan):
        raise TypeError("loop must be a LoopPlan")
    if limits is None:
        limits = (hard_limits or DEFAULT_WORKFLOW_HARD_LIMITS).effective(WorkflowLimits())
    if not isinstance(limits, WorkflowEffectiveLimits):
        raise TypeError("limits must be a WorkflowEffectiveLimits")
    issues = _IssueCollector()
    try:
        raw = _invoke_loop_source_provider(provider, loop.source, limits)
    except Exception:
        issues.add(
            "loop_source_read_error",
            "/source/from",
            "loop source provider failed to read source",
        )
        issues.raise_if_any()
        raise AssertionError("unreachable")
    snapshot = _snapshot_loop_source_payload(
        loop,
        raw,
        limits=limits,
        issues=issues,
        config_path="",
    )
    issues.raise_if_any()
    if snapshot is None:
        raise AssertionError("loop source snapshot was not produced")
    return snapshot


def preflight_workflow_bounds(
    plan: WorkflowPlan | WorkflowConfig,
    providers: object = None,
    *,
    provider_registry: object = None,
    registry: CapabilityRegistry | CapabilityRegistrySnapshot | None = None,
    hard_limits: WorkflowHardLimits | None = None,
) -> WorkflowBoundsResult:
    """Snapshot all loop sources and enforce the bounded execution contract.

    The normalized plan is structurally validated before providers are
    invoked.  Each registered runtime provider is then called once, and its
    ordered items are frozen into a digest-bearing snapshot.  No runner,
    artifact, lock, or workflow state is created by this function.
    """

    if isinstance(plan, WorkflowConfig):
        plan = normalize_workflow_config(plan)
    if not isinstance(plan, WorkflowPlan):
        raise TypeError("plan must be a WorkflowPlan or WorkflowConfig")
    if providers is not None and provider_registry is not None:
        raise ValueError("provide only one of providers and provider_registry")
    if provider_registry is not None:
        providers = provider_registry
    if hard_limits is None:
        hard_limits = DEFAULT_WORKFLOW_HARD_LIMITS
    if not isinstance(hard_limits, WorkflowHardLimits):
        raise TypeError("hard_limits must be a WorkflowHardLimits")
    effective_limits = hard_limits.effective(plan.limits)
    capability_snapshot = _capability_snapshot_for_bounds(registry, profile=plan.profile)
    issues = _IssueCollector()

    loop_entries: list[tuple[int, LoopPlan, str]] = []
    node_count = 0
    loop_count = 0
    body_step_count = 0
    top_level_step_count = 0
    for node_index, node in enumerate(plan.nodes):
        node_path = f"/nodes/{node_index}"
        node_count += 1
        if isinstance(node, StepPlan):
            top_level_step_count += 1
            continue
        if not isinstance(node, LoopPlan):
            issues.add("wrong_type", node_path, "workflow plan contains an unsupported node")
            continue
        loop_entries.append((node_index, node, node_path))
        loop_count += 1
        body_step_count += len(node.body)
        node_count += len(node.body)
        if not isinstance(node.max_items, int) or isinstance(node.max_items, bool) or node.max_items <= 0:
            issues.add("invalid_value", f"{node_path}/max_items", "max_items must be greater than zero")
            continue
        for body_index, body_step in enumerate(node.body):
            if isinstance(body_step, LoopPlan):
                issues.add(
                    "nested_loop",
                    f"{node_path}/body/{body_index}",
                    "nested loops are not supported by workflow schema v1",
                )
            elif not isinstance(body_step, StepPlan):
                issues.add(
                    "wrong_type",
                    f"{node_path}/body/{body_index}",
                    "loop body node must be a step",
                )

    _add_bound_issue(
        issues,
        actual=node_count,
        limit=effective_limits.max_nodes,
        path="/limits/max_nodes",
        label="workflow node count",
    )
    _add_bound_issue(
        issues,
        actual=loop_count,
        limit=effective_limits.max_loops,
        path="/limits/max_loops",
        label="workflow loop count",
    )
    _add_bound_issue(
        issues,
        actual=body_step_count,
        limit=effective_limits.max_body_steps,
        path="/limits/max_body_steps",
        label="workflow loop body step count",
    )
    # Validate all capability/provider registrations before invoking any
    # provider.  This preserves the fail-closed preflight boundary when a
    # later loop references an unknown source.
    runtime_providers: dict[str, object] = {}
    for _node_index, loop, node_path in loop_entries:
        if capability_snapshot is not None:
            capability = capability_snapshot.lookup("loop_source", loop.source.provider)
            if capability is None:
                issues.add(
                    "unknown_capability",
                    f"{node_path}/source/provider",
                    f"unknown loop_source capability {loop.source.provider!r}",
                )
            elif not capability.permits_profile(capability_snapshot.profile):
                issues.add(
                    "unauthorized_capability",
                    f"{node_path}/source/provider",
                    "loop source capability is not authorized for this profile",
                )
        provider = _runtime_loop_source_provider(providers, loop.source.provider)
        if provider is _MISSING:
            issues.add(
                "loop_source_provider_unavailable",
                f"{node_path}/source/provider",
                f"no runtime provider is registered for {loop.source.provider!r}",
            )
        else:
            runtime_providers[loop.local_id] = provider

    # A structural violation must not trigger source reads.  This also keeps
    # source providers out of the side-effect-free validation path when the
    # plan itself is already over budget.
    issues.raise_if_any()

    snapshots: dict[str, LoopSourceSnapshot] = {}
    for _node_index, loop, node_path in loop_entries:
        provider = runtime_providers[loop.local_id]
        try:
            raw = _invoke_loop_source_provider(provider, loop.source, effective_limits)
        except Exception:
            issues.add(
                "loop_source_read_error",
                f"{node_path}/source/from",
                "loop source provider failed to read source",
            )
            continue
        snapshot = _snapshot_loop_source_payload(
            loop,
            raw,
            limits=effective_limits,
            issues=issues,
            config_path=node_path,
        )
        if snapshot is not None:
            snapshots[loop.local_id] = snapshot

    total_items = sum(snapshot.item_count for snapshot in snapshots.values())
    total_snapshot_bytes = sum(snapshot.size_bytes for snapshot in snapshots.values())
    total_input_bytes = sum(snapshot.input_bytes for snapshot in snapshots.values())
    for _node_index, loop, node_path in loop_entries:
        snapshot = snapshots.get(loop.local_id)
        if snapshot is None:
            continue
        _add_bound_issue(
            issues,
            actual=snapshot.item_count,
            limit=min(loop.max_items, effective_limits.max_loop_items),
            path=f"{node_path}/max_items",
            label=f"loop {loop.local_id!r} item count",
        )
    _add_bound_issue(
        issues,
        actual=total_snapshot_bytes,
        limit=effective_limits.max_snapshot_bytes,
        path="/limits/max_snapshot_bytes",
        label="total loop source snapshot bytes",
    )
    _add_bound_issue(
        issues,
        actual=total_input_bytes,
        limit=effective_limits.max_prompt_input_bytes,
        path="/limits/max_prompt_input_bytes",
        label="total loop source input bytes",
    )
    potential_step_executions = top_level_step_count + sum(
        snapshot.item_count * len(loop.body)
        for _node_index, loop, _node_path in loop_entries
        if (snapshot := snapshots.get(loop.local_id)) is not None
    )
    _add_bound_issue(
        issues,
        actual=potential_step_executions,
        limit=effective_limits.max_total_steps,
        path="/limits/max_total_steps",
        label="potential step executions",
    )
    issues.raise_if_any()

    digest_payload = {
        "schema_version": plan.schema_version,
        "workflow_id": plan.workflow_id,
        "sources": [
            {
                "loop_id": loop.local_id,
                "position": node_index,
                "digest": snapshots[loop.local_id].digest,
            }
            for node_index, loop, _node_path in loop_entries
        ],
    }
    snapshot_digest = hashlib.sha256(_canonical_source_bytes(digest_payload)).hexdigest()
    return WorkflowBoundsResult(
        plan=plan,
        snapshots=snapshots,
        hard_limits=hard_limits,
        effective_limits=effective_limits,
        node_count=node_count,
        loop_count=loop_count,
        body_step_count=body_step_count,
        loop_item_count=total_items,
        snapshot_bytes=total_snapshot_bytes,
        input_bytes=total_input_bytes,
        potential_step_executions=potential_step_executions,
        snapshot_digest=snapshot_digest,
    )


snapshot_loop_sources = preflight_workflow_bounds
validate_workflow_bounds = preflight_workflow_bounds
validate_loop_source_bounds = preflight_workflow_bounds
preflight_loop_sources = preflight_workflow_bounds


RUN_IDENTITY_SCHEMA_VERSION = "1.0"
RUN_STATE_SCHEMA_VERSION = "1.0"
RUN_STATE_FILENAME = "workflow-state.json"
MAX_RUN_STATE_BYTES = 8 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_FIELD_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "token",
    }
)


class WorkflowRunIdentityError(ValueError):
    """Base error for invalid or unusable configured-workflow identity data."""


class ResumeStateError(WorkflowRunIdentityError):
    """Base error for unreadable or invalid workflow resume state."""


class StaleResumeIdentityError(ResumeStateError):
    """Raised when persisted state belongs to a different validated run."""

    code = "stale_resume_identity"


class ResumeStateNotFoundError(ResumeStateError):
    """Raised when a caller requested resume but no state exists."""

    code = "resume_state_not_found"


class RunStateCorruptionError(ResumeStateError):
    """Raised when durable state cannot be parsed or passes no invariants."""

    code = "run_state_corrupt"


def _identity_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _identity_secret_key(key: str) -> bool:
    parts = set(re.split(r"[^a-z0-9]+", key.casefold()))
    return bool(parts & _SECRET_FIELD_PARTS)


def _identity_serializable(value: object, *, redact_secrets: bool = False) -> object:
    """Convert trusted identity inputs to deterministic JSON-compatible data.

    This helper is intentionally stricter than ``repr``.  Object addresses and
    unordered collection representations must never affect a persisted run
    identity.  Runner configuration is serialized with secret-looking fields
    redacted because the identity payload may be written to state.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("identity values must not contain non-finite numbers")
        return value
    if isinstance(value, bytes):
        return {
            "__bytes_sha256__": hashlib.sha256(value).hexdigest(),
            "size_bytes": len(value),
        }
    if isinstance(value, Path):
        return {"__path__": value.as_posix()}
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError("identity mapping keys must be strings")
            result[key] = (
                "<redacted>"
                if redact_secrets and _identity_secret_key(key)
                else _identity_serializable(nested, redact_secrets=redact_secrets)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _identity_serializable(item, redact_secrets=redact_secrets)
            for item in value
        ]
    if isinstance(value, (set, frozenset)):
        values = [
            _identity_serializable(item, redact_secrets=redact_secrets)
            for item in value
        ]
        return sorted(values, key=lambda item: json.dumps(item, sort_keys=True, default=repr))
    if is_dataclass(value):
        return {
            field_info.name: (
                "<redacted>"
                if redact_secrets and _identity_secret_key(field_info.name)
                else _identity_serializable(
                    getattr(value, field_info.name),
                    redact_secrets=redact_secrets,
                )
            )
            for field_info in fields(value)
        }

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _identity_serializable(to_dict(), redact_secrets=redact_secrets)
        except TypeError:
            # Some external DTOs expose a parameterized ``to_dict``.  They are
            # handled by the public mapping/object fallbacks below when they
            # provide one; a callable object is never represented by repr.
            pass

    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, Mapping):
        return _identity_serializable(attributes, redact_secrets=redact_secrets)
    raise TypeError(f"unsupported identity value type: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """Encode an identity value with stable JSON ordering and no NaN values."""

    normalized = _identity_serializable(value)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("identity value could not be canonically encoded") from exc


def canonical_sha256(value: object) -> str:
    """Return the SHA-256 digest of a deterministic JSON representation."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _plan_artifact_key_payload(key: ArtifactKey) -> dict[str, object]:
    return {
        "producer_node_id": key.producer_node_id,
        "output_id": key.output_id,
        "scope": key.scope,
        "cardinality": key.cardinality,
    }


def _plan_input_payload(binding: InputBindingPlan) -> dict[str, object]:
    result: dict[str, object] = {
        "name": binding.name,
        "source": binding.source,
        "source_kind": binding.source_kind,
    }
    if binding.virtual_input is not None:
        result["virtual_input"] = binding.virtual_input
    if binding.artifact is not None:
        result["artifact"] = {
            "key": _plan_artifact_key_payload(binding.artifact.key),
            "expected_cardinality": binding.artifact.expected_cardinality,
        }
    return result


def _plan_output_payload(output: OutputPlan) -> dict[str, object]:
    return {
        "id": output.id,
        "kind": output.kind,
        "path": output.path,
        "artifact_key": _plan_artifact_key_payload(output.artifact_key),
    }


def _plan_step_payload(step: StepPlan) -> dict[str, object]:
    return {
        "type": "step",
        "canonical_id": step.canonical_id,
        "local_id": step.local_id,
        "lifecycle": step.lifecycle,
        "runner": step.runner,
        "prompt": step.prompt,
        "skill": step.skill,
        "inputs": [_plan_input_payload(item) for item in step.inputs],
        "outputs": [_plan_output_payload(item) for item in step.outputs],
        "dependencies": list(step.dependencies),
        "explicit_dependencies": list(step.explicit_dependencies),
    }


def _plan_loop_source_payload(source: LoopSourceBinding) -> dict[str, object]:
    result: dict[str, object] = {
        "source": source.source,
        "provider": source.provider,
    }
    if source.virtual_input is not None:
        result["virtual_input"] = source.virtual_input
    if source.artifact is not None:
        result["artifact"] = {
            "key": _plan_artifact_key_payload(source.artifact.key),
            "expected_cardinality": source.artifact.expected_cardinality,
        }
    return result


def _plan_node_payload(node: PlanNode) -> dict[str, object]:
    if isinstance(node, StepPlan):
        return _plan_step_payload(node)
    if isinstance(node, LoopPlan):
        return {
            "type": "loop",
            "canonical_id": node.canonical_id,
            "local_id": node.local_id,
            "source": _plan_loop_source_payload(node.source),
            "max_items": node.max_items,
            "controller": node.controller,
            "body": [_plan_step_payload(step) for step in node.body],
            "exports": [
                {
                    "id": export.id,
                    "source": export.source,
                    "cardinality": export.cardinality,
                    "source_artifact": _plan_artifact_key_payload(export.source_artifact),
                    "artifact_key": _plan_artifact_key_payload(export.artifact_key),
                }
                for export in node.exports
            ],
            "dependencies": list(node.dependencies),
        }
    raise TypeError("workflow plan contains an unsupported node")


def workflow_plan_payload(plan: WorkflowPlan | WorkflowConfig) -> dict[str, object]:
    """Return the complete canonical representation used for workflow identity."""

    if isinstance(plan, WorkflowConfig):
        plan = normalize_workflow_config(plan)
    if not isinstance(plan, WorkflowPlan):
        raise TypeError("plan must be a WorkflowPlan or WorkflowConfig")
    return {
        "schema_version": plan.schema_version,
        "workflow_id": plan.workflow_id,
        "profile": plan.profile,
        "limits": _identity_serializable(plan.limits),
        "nodes": [_plan_node_payload(node) for node in plan.nodes],
        "dependency_graph": {
            key: list(plan.dependency_graph[key])
            for key in sorted(plan.dependency_graph)
        },
        "artifact_graph": {
            key: _plan_artifact_key_payload(plan.artifact_graph[key])
            for key in sorted(plan.artifact_graph)
        },
    }


def workflow_plan_digest(plan: WorkflowPlan | WorkflowConfig) -> str:
    """Digest the normalized workflow structure, independent of JSON formatting."""

    return canonical_sha256(workflow_plan_payload(plan))


canonical_workflow_digest = workflow_plan_digest


def _digest_file_snapshot(path: Path | str, *, max_bytes: int = MAX_RUN_STATE_BYTES) -> str:
    """Hash a regular, non-symlink file without following a replacement race."""

    target = Path(path)
    try:
        state = target.lstat()
    except OSError as exc:
        raise WorkflowRunIdentityError(f"cannot inspect input snapshot: {target}") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise WorkflowRunIdentityError(
            f"input snapshot must be a regular non-symlink file: {target}"
        )
    descriptor: int | None = None
    digest = hashlib.sha256()
    size = 0
    try:
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise WorkflowRunIdentityError(
                f"cannot open input snapshot: {target}"
            ) from exc
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != state.st_dev
            or opened.st_ino != state.st_ino
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise WorkflowRunIdentityError(
                f"input snapshot changed before it was read: {target}"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise WorkflowRunIdentityError(
                    f"input snapshot exceeds {max_bytes} bytes: {target}"
                )
            digest.update(chunk)
        after = target.lstat()
        if (
            stat.S_ISLNK(after.st_mode)
            or after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise WorkflowRunIdentityError(
                f"input snapshot changed while it was read: {target}"
            )
        return digest.hexdigest()
    except FileNotFoundError as exc:
        raise WorkflowRunIdentityError(
            f"input snapshot disappeared while it was read: {target}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def digest_input_snapshot(value: object) -> str:
    """Digest one issue/instruction/resource snapshot without persisting it."""

    if isinstance(value, ResourceMetadata):
        return _identity_sha256(value.sha256, field_name="resource digest")
    if isinstance(value, Path):
        return _digest_file_snapshot(value)
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, bytearray):
        return hashlib.sha256(bytes(value)).hexdigest()
    if isinstance(value, str):
        try:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
        except UnicodeEncodeError as exc:
            raise WorkflowRunIdentityError("input snapshot text is not valid UTF-8") from exc
    if isinstance(value, LoopSourceSnapshot):
        return _identity_sha256(value.digest, field_name="loop source digest")
    digest = getattr(value, "sha256", None)
    if isinstance(digest, str) and _SHA256_PATTERN.fullmatch(digest):
        return digest
    digest = getattr(value, "digest", None)
    if isinstance(digest, str) and _SHA256_PATTERN.fullmatch(digest):
        return digest
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowRunIdentityError("input snapshot is not digestible") from exc


def _digest_named_values(values: object, *, field_name: str) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    result: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_name} keys must be non-empty strings")
        if key in result:
            raise ValueError(f"duplicate {field_name} key: {key}")
        result[key] = digest_input_snapshot(value)
    return result


def _normalise_digest_mapping(values: object, *, field_name: str) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    result: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_name} keys must be non-empty strings")
        if isinstance(value, ResourceMetadata):
            value = value.sha256
        result[key] = _identity_sha256(value, field_name=f"{field_name}.{key}")
    return result


def _merge_digest_maps(
    first: Mapping[str, str],
    second: Mapping[str, str],
    *,
    field_name: str,
) -> dict[str, str]:
    result = dict(first)
    for key, value in second.items():
        previous = result.get(key)
        if previous is not None and previous != value:
            raise ValueError(f"conflicting {field_name} digest for {key!r}")
        result[key] = value
    return result


def _source_snapshot_digests(
    source_snapshots: WorkflowBoundsResult | Mapping[str, object] | None,
) -> tuple[str, dict[str, str], str | None]:
    if source_snapshots is None:
        empty = canonical_sha256({"snapshots": []})
        return empty, {}, None
    effective_limits_digest: str | None = None
    if isinstance(source_snapshots, WorkflowBoundsResult):
        digests = {
            loop_id: _identity_sha256(snapshot.digest, field_name="loop source digest")
            for loop_id, snapshot in source_snapshots.snapshots.items()
        }
        effective_limits_digest = canonical_sha256(
            source_snapshots.effective_limits.as_dict()
        )
        return (
            _identity_sha256(source_snapshots.snapshot_digest, field_name="snapshot digest"),
            digests,
            effective_limits_digest,
        )
    if not isinstance(source_snapshots, Mapping):
        raise TypeError("source_snapshots must be a WorkflowBoundsResult or mapping")
    digests: dict[str, str] = {}
    for loop_id, snapshot in source_snapshots.items():
        if not isinstance(loop_id, str) or not loop_id:
            raise ValueError("source snapshot keys must be non-empty strings")
        if isinstance(snapshot, LoopSourceSnapshot):
            digest = snapshot.digest
        elif isinstance(snapshot, str) and _SHA256_PATTERN.fullmatch(snapshot):
            digest = snapshot
        else:
            digest = digest_input_snapshot(snapshot)
        digests[loop_id] = _identity_sha256(
            digest,
            field_name=f"source_snapshots.{loop_id}",
        )
    overall = canonical_sha256(
        {"snapshots": [{"loop_id": key, "digest": digests[key]} for key in sorted(digests)]}
    )
    return overall, digests, None


def _registry_identity(
    registry: object,
    *,
    profile: str,
    capability_authorization: CapabilityAuthorizationResult | None,
) -> tuple[str, Mapping[str, str]]:
    candidate = capability_authorization or registry
    if isinstance(candidate, CapabilityAuthorizationResult):
        snapshot = candidate.snapshot
        return snapshot.registry_digest, snapshot.resource_digests
    if isinstance(candidate, CapabilityRegistrySnapshot):
        return candidate.registry_digest, candidate.resource_digests
    if isinstance(candidate, CapabilityRegistry):
        snapshot = candidate.snapshot(profile)
        return snapshot.registry_digest, snapshot.resource_digests
    if candidate is None:
        snapshot = CapabilityRegistry.default().snapshot(profile)
        return snapshot.registry_digest, snapshot.resource_digests
    registry_digest = getattr(candidate, "registry_digest", None)
    if isinstance(registry_digest, str) and _SHA256_PATTERN.fullmatch(registry_digest):
        resource_digests = getattr(candidate, "resource_digests", {})
        return registry_digest, _normalise_digest_mapping(
            resource_digests,
            field_name="registry resource digests",
        )
    if isinstance(candidate, Mapping):
        explicit = candidate.get("registry_digest", candidate.get("digest"))
        if isinstance(explicit, str) and _SHA256_PATTERN.fullmatch(explicit):
            return explicit, _normalise_digest_mapping(
                candidate.get("resource_digests", {}),
                field_name="registry resource digests",
            )
    try:
        return canonical_sha256(candidate), {}
    except (TypeError, ValueError) as exc:
        raise WorkflowRunIdentityError("registry snapshot is not digestible") from exc


def _namespace_identity_payload(
    namespace_plan: ArtifactNamespacePlan,
) -> dict[str, object]:
    def one(namespace: ArtifactNamespace) -> dict[str, object]:
        return {
            "artifact_key": _plan_artifact_key_payload(namespace.artifact_key),
            "node_instance_id": namespace.node_instance_id,
            "relative_path": namespace.relative_path,
            "scope_relative_path": namespace.scope_relative_path,
            "kind": namespace.kind,
            "item_id": namespace.item_id,
            "position": namespace.position,
            "is_export": namespace.is_export,
        }

    return {
        "entries": [one(namespace) for namespace in namespace_plan.entries],
        "collection_exports": [one(namespace) for namespace in namespace_plan.collection_exports],
    }


@dataclass(frozen=True, slots=True)
class WorkflowRunIdentity:
    """Immutable identity binding every input that affects a workflow run."""

    digest: str
    workflow_id: str
    workflow_digest: str
    registry_digest: str
    resource_digests: Mapping[str, str]
    input_digests: Mapping[str, str]
    source_snapshot_digest: str
    source_snapshot_digests: Mapping[str, str]
    runner_config_digest: str | None = None
    artifact_namespace_digest: str | None = None
    effective_limits_digest: str | None = None
    schema_version: str = RUN_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUN_IDENTITY_SCHEMA_VERSION:
            raise ValueError(f"unsupported run identity schema: {self.schema_version!r}")
        if not isinstance(self.workflow_id, str) or not self.workflow_id:
            raise ValueError("workflow identity requires a workflow id")
        _identity_sha256(self.digest, field_name="run identity")
        _identity_sha256(self.workflow_digest, field_name="workflow digest")
        _identity_sha256(self.registry_digest, field_name="registry digest")
        _identity_sha256(self.source_snapshot_digest, field_name="source snapshot digest")
        for field_name in ("runner_config_digest", "artifact_namespace_digest", "effective_limits_digest"):
            value = getattr(self, field_name)
            if value is not None:
                _identity_sha256(value, field_name=field_name)
        for field_name in ("resource_digests", "input_digests", "source_snapshot_digests"):
            normalized = _normalise_digest_mapping(
                getattr(self, field_name),
                field_name=field_name,
            )
            object.__setattr__(self, field_name, MappingProxyType(normalized))

    @property
    def value(self) -> str:
        return self.digest

    @property
    def run_identity(self) -> str:
        return self.digest

    @property
    def run_id(self) -> str:
        return self.digest

    @property
    def identity(self) -> str:
        return self.digest

    @property
    def sha256(self) -> str:
        return self.digest

    def __str__(self) -> str:
        return self.digest

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
            "workflow_digest": self.workflow_digest,
            "registry_digest": self.registry_digest,
            "resource_digests": dict(sorted(self.resource_digests.items())),
            "input_digests": dict(sorted(self.input_digests.items())),
            "source_snapshot_digest": self.source_snapshot_digest,
            "source_snapshot_digests": dict(sorted(self.source_snapshot_digests.items())),
            "runner_config_digest": self.runner_config_digest,
            "artifact_namespace_digest": self.artifact_namespace_digest,
            "effective_limits_digest": self.effective_limits_digest,
        }

    to_payload = payload

    def verify(self) -> bool:
        return self.digest == canonical_sha256(self.payload())

    def to_dict(self) -> dict[str, object]:
        result = self.payload()
        result["digest"] = self.digest
        return result

    as_dict = to_dict

    @classmethod
    def from_dict(cls, value: object) -> "WorkflowRunIdentity":
        if not isinstance(value, Mapping):
            raise ValueError("run identity must be an object")
        required = {
            "schema_version",
            "digest",
            "workflow_id",
            "workflow_digest",
            "registry_digest",
            "resource_digests",
            "input_digests",
            "source_snapshot_digest",
            "source_snapshot_digests",
            "runner_config_digest",
            "artifact_namespace_digest",
            "effective_limits_digest",
        }
        unknown = set(value) - required
        missing = required - set(value)
        if unknown:
            raise ValueError("run identity has unsupported keys: " + ", ".join(sorted(unknown)))
        if missing:
            raise ValueError("run identity is missing keys: " + ", ".join(sorted(missing)))
        identity = cls(
            schema_version=value["schema_version"],  # type: ignore[arg-type]
            digest=value["digest"],  # type: ignore[arg-type]
            workflow_id=value["workflow_id"],  # type: ignore[arg-type]
            workflow_digest=value["workflow_digest"],  # type: ignore[arg-type]
            registry_digest=value["registry_digest"],  # type: ignore[arg-type]
            resource_digests=value["resource_digests"],  # type: ignore[arg-type]
            input_digests=value["input_digests"],  # type: ignore[arg-type]
            source_snapshot_digest=value["source_snapshot_digest"],  # type: ignore[arg-type]
            source_snapshot_digests=value["source_snapshot_digests"],  # type: ignore[arg-type]
            runner_config_digest=value["runner_config_digest"],  # type: ignore[arg-type]
            artifact_namespace_digest=value["artifact_namespace_digest"],  # type: ignore[arg-type]
            effective_limits_digest=value["effective_limits_digest"],  # type: ignore[arg-type]
        )
        if not identity.verify():
            raise ValueError("run identity digest does not match its components")
        return identity


RunIdentity = WorkflowRunIdentity
WorkflowIdentity = WorkflowRunIdentity


def build_run_identity(
    plan: WorkflowPlan | WorkflowConfig,
    registry: object = None,
    *,
    capability_snapshot: CapabilityRegistrySnapshot | None = None,
    capability_authorization: CapabilityAuthorizationResult | None = None,
    effective_runner_config: object = None,
    runner_config: object = None,
    runner_configs: object = None,
    resource_digests: Mapping[str, object] | None = None,
    resources: Mapping[str, object] | None = None,
    input_snapshots: Mapping[str, object] | None = None,
    issue_snapshot: object = None,
    issue: object = None,
    repo_instructions_snapshot: object = None,
    repo_instructions: object = None,
    source_snapshots: WorkflowBoundsResult | Mapping[str, object] | None = None,
    loop_source_snapshots: WorkflowBoundsResult | Mapping[str, object] | None = None,
    artifact_namespace_plan: ArtifactNamespacePlan | None = None,
    namespace_plan: ArtifactNamespacePlan | None = None,
    effective_limits: WorkflowEffectiveLimits | Mapping[str, object] | None = None,
) -> WorkflowRunIdentity:
    """Build identity only from validated plan and frozen input snapshots."""

    if isinstance(plan, WorkflowConfig):
        plan = normalize_workflow_config(plan)
    if not isinstance(plan, WorkflowPlan):
        raise TypeError("plan must be a WorkflowPlan or WorkflowConfig")
    if capability_snapshot is not None:
        if capability_authorization is not None:
            raise ValueError("provide only one of capability_snapshot and capability_authorization")
        registry = capability_snapshot
    if capability_authorization is not None and isinstance(registry, CapabilityAuthorizationResult):
        if registry is not capability_authorization:
            raise ValueError("conflicting capability authorization results")
    registry_digest, authorized_resources = _registry_identity(
        registry,
        profile=plan.profile,
        capability_authorization=capability_authorization,
    )
    registry_resources = _normalise_digest_mapping(
        authorized_resources,
        field_name="resource_digests",
    )
    registry_resources = _merge_digest_maps(
        registry_resources,
        _normalise_digest_mapping(resource_digests, field_name="resource_digests"),
        field_name="resource",
    )
    registry_resources = _merge_digest_maps(
        registry_resources,
        _digest_named_values(resources, field_name="resources"),
        field_name="resource",
    )

    if effective_runner_config is not None and runner_config is not None:
        raise ValueError("provide only one of effective_runner_config and runner_config")
    runner_value = effective_runner_config if effective_runner_config is not None else runner_config
    if runner_value is not None and runner_configs is not None:
        raise ValueError("provide only one runner configuration value")
    if runner_value is None:
        runner_value = runner_configs
    runner_digest = (
        canonical_sha256(_identity_serializable(runner_value, redact_secrets=True))
        if runner_value is not None
        else None
    )

    if issue_snapshot is not None and issue is not None:
        raise ValueError("provide only one of issue_snapshot and issue")
    issue_value = issue_snapshot if issue_snapshot is not None else issue
    if repo_instructions_snapshot is not None and repo_instructions is not None:
        raise ValueError(
            "provide only one of repo_instructions_snapshot and repo_instructions"
        )
    instructions_value = (
        repo_instructions_snapshot
        if repo_instructions_snapshot is not None
        else repo_instructions
    )
    input_digests = _digest_named_values(input_snapshots, field_name="input_snapshots")
    if issue_value is not None:
        input_digests = _merge_digest_maps(
            input_digests,
            {"issue": digest_input_snapshot(issue_value)},
            field_name="input snapshot",
        )
    if instructions_value is not None:
        input_digests = _merge_digest_maps(
            input_digests,
            {"repo_instructions": digest_input_snapshot(instructions_value)},
            field_name="input snapshot",
        )

    if source_snapshots is not None and loop_source_snapshots is not None:
        raise ValueError("provide only one of source_snapshots and loop_source_snapshots")
    source_value = source_snapshots if source_snapshots is not None else loop_source_snapshots
    source_digest, source_digests, bounds_limits_digest = _source_snapshot_digests(source_value)
    limits_digest = bounds_limits_digest
    if effective_limits is not None:
        limits_digest = canonical_sha256(
            effective_limits.as_dict()
            if isinstance(effective_limits, WorkflowEffectiveLimits)
            else effective_limits
        )

    if artifact_namespace_plan is not None and namespace_plan is not None:
        raise ValueError("provide only one namespace plan")
    namespace_value = (
        artifact_namespace_plan if artifact_namespace_plan is not None else namespace_plan
    )
    namespace_digest = (
        canonical_sha256(_namespace_identity_payload(namespace_value))
        if namespace_value is not None
        else None
    )

    workflow_digest = workflow_plan_digest(plan)
    payload = {
        "schema_version": RUN_IDENTITY_SCHEMA_VERSION,
        "workflow_id": plan.workflow_id,
        "workflow_digest": workflow_digest,
        "registry_digest": _identity_sha256(registry_digest, field_name="registry digest"),
        "resource_digests": dict(sorted(registry_resources.items())),
        "input_digests": dict(sorted(input_digests.items())),
        "source_snapshot_digest": source_digest,
        "source_snapshot_digests": dict(sorted(source_digests.items())),
        "runner_config_digest": runner_digest,
        "artifact_namespace_digest": namespace_digest,
        "effective_limits_digest": limits_digest,
    }
    digest = canonical_sha256(payload)
    return WorkflowRunIdentity(
        digest=digest,
        workflow_id=plan.workflow_id,
        workflow_digest=workflow_digest,
        registry_digest=payload["registry_digest"],  # type: ignore[arg-type]
        resource_digests=registry_resources,
        input_digests=input_digests,
        source_snapshot_digest=source_digest,
        source_snapshot_digests=source_digests,
        runner_config_digest=runner_digest,
        artifact_namespace_digest=namespace_digest,
        effective_limits_digest=limits_digest,
    )


create_run_identity = build_run_identity
compute_run_identity = build_run_identity


def _state_identity_value(identity: WorkflowRunIdentity | str) -> str:
    if isinstance(identity, WorkflowRunIdentity):
        return identity.digest
    return _identity_sha256(identity, field_name="run identity")


def _validate_instance_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ValueError("completed instance id must be a non-empty short string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("completed instance id contains a control character")
    if "\\" in value or ".." in value.split("/"):
        raise ValueError("completed instance id has unsafe path segments")
    return value


def _coerce_manifest(value: object) -> ArtifactManifest | None:
    if value is None:
        return None
    if isinstance(value, ArtifactManifest):
        return value
    if isinstance(value, Mapping):
        return ArtifactManifest.from_dict(value)
    raise TypeError("output manifest must be an ArtifactManifest or mapping")


@dataclass(frozen=True, slots=True)
class RunState:
    """Durable progress state tied to one exact :class:`WorkflowRunIdentity`."""

    run_identity: str
    workflow_id: str
    completed_instances: tuple[str, ...] = ()
    output_manifest: ArtifactManifest | None = None
    status: Literal["running", "paused", "failed", "completed"] = "running"
    pause_reason: str | None = None
    schema_version: str = RUN_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUN_STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported run state schema: {self.schema_version!r}")
        _state_identity_value(self.run_identity)
        if not isinstance(self.workflow_id, str) or not self.workflow_id:
            raise ValueError("run state workflow id must be a non-empty string")
        if not isinstance(self.status, str) or self.status not in {
            "running",
            "paused",
            "failed",
            "completed",
        }:
            raise ValueError(f"unsupported run state status: {self.status!r}")
        if self.pause_reason is not None and not isinstance(self.pause_reason, str):
            raise ValueError("pause reason must be null or a non-empty string")
        if self.pause_reason is not None and not self.pause_reason.strip():
            raise ValueError("pause reason must be null or a non-empty string")
        if self.status == "paused" and self.pause_reason is None:
            raise ValueError("paused run state requires a pause reason")
        instances = tuple(_validate_instance_id(item) for item in self.completed_instances)
        if len(set(instances)) != len(instances):
            raise ValueError("run state contains duplicate completed instances")
        object.__setattr__(self, "completed_instances", instances)
        manifest = _coerce_manifest(self.output_manifest)
        if manifest is not None and manifest.run_identity != self.run_identity:
            raise ValueError("output manifest run identity does not match run state")
        object.__setattr__(self, "output_manifest", manifest)

    @classmethod
    def initial(
        cls,
        identity: WorkflowRunIdentity | str,
        workflow_id: str,
    ) -> "RunState":
        return cls(run_identity=_state_identity_value(identity), workflow_id=workflow_id)

    @property
    def identity(self) -> str:
        return self.run_identity

    @property
    def completed_instance_ids(self) -> tuple[str, ...]:
        return self.completed_instances

    @property
    def output_entries(self) -> tuple[ArtifactManifestEntry, ...]:
        return self.output_manifest.entries if self.output_manifest is not None else ()

    def is_completed(self, node_instance_id: str) -> bool:
        return _validate_instance_id(node_instance_id) in self.completed_instances

    is_instance_completed = is_completed

    def should_execute(self, node_instance_id: str) -> bool:
        return not self.is_completed(node_instance_id)

    def pending_instances(self, node_instance_ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            item
            for item in (_validate_instance_id(value) for value in node_instance_ids)
            if item not in self.completed_instances
        )

    def with_completed_instance(
        self,
        node_instance_id: str,
        output_manifest: ArtifactManifest | ArtifactManifestEntry | Mapping[str, object] | None = None,
    ) -> "RunState":
        instance = _validate_instance_id(node_instance_id)
        manifest = self.output_manifest
        if isinstance(output_manifest, ArtifactManifestEntry):
            existing_entries = manifest.entries if manifest is not None else ()
            existing = next(
                (entry for entry in existing_entries if entry.identity == output_manifest.identity),
                None,
            )
            if existing is not None and existing.to_dict() != output_manifest.to_dict():
                raise ValueError("conflicting output manifest entry for completed instance")
            if existing is None:
                manifest = ArtifactManifest(
                    run_identity=self.run_identity,
                    entries=existing_entries + (output_manifest,),
                )
        elif output_manifest is not None:
            candidate = _coerce_manifest(output_manifest)
            assert candidate is not None
            if candidate.run_identity != self.run_identity:
                raise ValueError("output manifest run identity does not match run state")
            if manifest is None:
                manifest = candidate
            else:
                by_identity = {entry.identity: entry for entry in manifest.entries}
                for entry in candidate.entries:
                    previous = by_identity.get(entry.identity)
                    if previous is not None and previous.to_dict() != entry.to_dict():
                        raise ValueError("conflicting output manifest entry for completed instance")
                    by_identity[entry.identity] = entry
                manifest = ArtifactManifest(
                    run_identity=self.run_identity,
                    entries=tuple(by_identity.values()),
                )
        completed = self.completed_instances
        if instance not in completed:
            completed = completed + (instance,)
        return RunState(
            schema_version=self.schema_version,
            run_identity=self.run_identity,
            workflow_id=self.workflow_id,
            completed_instances=completed,
            output_manifest=manifest,
            status=self.status,
            pause_reason=self.pause_reason,
        )

    mark_completed = with_completed_instance
    record_completion = with_completed_instance

    def with_status(self, status: str, *, pause_reason: str | None = None) -> "RunState":
        return RunState(
            schema_version=self.schema_version,
            run_identity=self.run_identity,
            workflow_id=self.workflow_id,
            completed_instances=self.completed_instances,
            output_manifest=self.output_manifest,
            status=status,  # type: ignore[arg-type]
            pause_reason=pause_reason,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_identity": self.run_identity,
            "workflow_id": self.workflow_id,
            "completed_instances": list(self.completed_instances),
            "output_manifest": (
                self.output_manifest.to_dict() if self.output_manifest is not None else None
            ),
            "status": self.status,
            "pause_reason": self.pause_reason,
        }

    as_dict = to_dict

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        )

    @classmethod
    def from_dict(cls, value: object) -> "RunState":
        if not isinstance(value, Mapping):
            raise ValueError("run state must be an object")
        required = {
            "schema_version",
            "run_identity",
            "workflow_id",
            "completed_instances",
            "output_manifest",
            "status",
            "pause_reason",
        }
        unknown = set(value) - required
        missing = required - set(value)
        if unknown:
            raise ValueError("run state has unsupported keys: " + ", ".join(sorted(unknown)))
        if missing:
            raise ValueError("run state is missing keys: " + ", ".join(sorted(missing)))
        completed = value["completed_instances"]
        if not isinstance(completed, (list, tuple)):
            raise ValueError("run state completed_instances must be an array")
        return cls(
            schema_version=value["schema_version"],  # type: ignore[arg-type]
            run_identity=value["run_identity"],  # type: ignore[arg-type]
            workflow_id=value["workflow_id"],  # type: ignore[arg-type]
            completed_instances=tuple(completed),  # type: ignore[arg-type]
            output_manifest=_coerce_manifest(value["output_manifest"]),
            status=value["status"],  # type: ignore[arg-type]
            pause_reason=value["pause_reason"],  # type: ignore[arg-type]
        )


WorkflowState = RunState
WorkflowRunState = RunState


def validate_resume_state(
    state: RunState | Mapping[str, object],
    identity: WorkflowRunIdentity | str,
    *,
    artifact_root: Path | str | None = None,
    expected_namespaces: ArtifactNamespacePlan | Iterable[ArtifactNamespace] | None = None,
    workflow_id: str | None = None,
) -> RunState:
    """Fail closed unless state identity and any persisted outputs still match."""

    if isinstance(state, Mapping):
        try:
            state = RunState.from_dict(state)
        except (TypeError, ValueError) as exc:
            raise RunStateCorruptionError("workflow resume state is invalid") from exc
    if not isinstance(state, RunState):
        raise TypeError("state must be a RunState or mapping")
    expected_identity = _state_identity_value(identity)
    if state.run_identity != expected_identity:
        raise StaleResumeIdentityError(
            "stale_resume_identity: persisted workflow state belongs to another run"
        )
    if workflow_id is not None and state.workflow_id != workflow_id:
        raise StaleResumeIdentityError(
            "stale_resume_identity: persisted workflow state belongs to another workflow"
        )
    if state.output_manifest is not None and artifact_root is not None:
        try:
            validate_artifact_manifest(
                artifact_root,
                state.output_manifest,
                expected_run_identity=expected_identity,
                expected_namespaces=expected_namespaces,
            )
        except (ArtifactPathSafetyError, ArtifactOutputValidationError, OSError, ValueError) as exc:
            raise RunStateCorruptionError(
                "persisted workflow output manifest is stale or unsafe"
            ) from exc
    return state


validate_resume = validate_resume_state
validate_run_state = validate_resume_state


class RunStateStore:
    """Read and atomically persist resume state below a validated artifact root."""

    def __init__(
        self,
        artifact_root: Path | str,
        *,
        filename: str = RUN_STATE_FILENAME,
        max_bytes: int = MAX_RUN_STATE_BYTES,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        _portable_artifact_path_parts(filename, field_name="run state path")
        self.guard = ArtifactPathGuard(artifact_root)
        self.filename = filename
        self.max_bytes = max_bytes

    @property
    def artifact_root(self) -> Path:
        return self.guard.root

    @property
    def path(self) -> Path:
        return self.guard.validate(self.filename)

    @property
    def state_path(self) -> Path:
        return self.path

    def load(self) -> RunState | None:
        try:
            path = self.path
            state = _lstat_path(path)
        except (OSError, ValueError, TypeError) as exc:
            raise RunStateCorruptionError("workflow resume state path is unsafe") from exc
        if state is None:
            return None
        try:
            raw, _fingerprint = _read_regular_artifact_bytes(
                self.guard,
                path,
                max_bytes=self.max_bytes,
            )
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_manifest_object_pairs,
                parse_constant=_reject_json_constant,
            )
            return RunState.from_dict(payload)
        except (
            AttributeError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            OSError,
        ) as exc:
            if isinstance(exc, RunStateCorruptionError):
                raise
            raise RunStateCorruptionError(
                f"workflow resume state is unreadable: {path}"
            ) from exc

    read = load

    def save(self, state: RunState, *, expected_identity: WorkflowRunIdentity | str | None = None) -> Path:
        if not isinstance(state, RunState):
            raise TypeError("state must be a RunState")
        if expected_identity is not None and state.run_identity != _state_identity_value(expected_identity):
            raise StaleResumeIdentityError(
                "stale_resume_identity: state does not match the expected run"
            )
        payload = state.to_json_bytes()
        if len(payload) > self.max_bytes:
            raise RunStateCorruptionError("workflow resume state exceeds its size limit")
        # ``atomic_write_bytes`` is the first operation that may create the
        # artifact root.  Callers reach this method only after full
        # config/capability/source/path validation has completed.
        return ArtifactManifestStore(self.artifact_root).atomic_write_bytes(self.path, payload)

    write = save

    def initialize(
        self,
        identity: WorkflowRunIdentity | str,
        workflow_id: str,
        *,
        expected_identity: WorkflowRunIdentity | str | None = None,
    ) -> RunState:
        state = RunState.initial(identity, workflow_id)
        self.save(state, expected_identity=expected_identity)
        return state

    def load_for_resume(
        self,
        identity: WorkflowRunIdentity | str,
        *,
        artifact_root: Path | str | None = None,
        expected_namespaces: ArtifactNamespacePlan | Iterable[ArtifactNamespace] | None = None,
        workflow_id: str | None = None,
    ) -> RunState:
        state = self.load()
        if state is None:
            raise ResumeStateNotFoundError(
                f"workflow resume state not found: {self.path}"
            )
        validation_root = self.artifact_root if artifact_root is None else artifact_root
        return validate_resume_state(
            state,
            identity,
            artifact_root=validation_root,
            expected_namespaces=expected_namespaces,
            workflow_id=workflow_id,
        )

    resume = load_for_resume

    def record_completed(
        self,
        state: RunState,
        node_instance_id: str,
        output_manifest: ArtifactManifest | ArtifactManifestEntry | Mapping[str, object] | None = None,
        *,
        expected_identity: WorkflowRunIdentity | str | None = None,
    ) -> RunState:
        if expected_identity is not None:
            validate_resume_state(state, expected_identity)
        updated = state.with_completed_instance(node_instance_id, output_manifest)
        self.save(updated, expected_identity=expected_identity)
        return updated


ResumeStateStore = RunStateStore
WorkflowStateStore = RunStateStore
RunStateRepository = RunStateStore


def default_capability_registry() -> CapabilityRegistry:
    return CapabilityRegistry.default()


__all__ = [
    "ARTIFACT_CARDINALITIES",
    "ARTIFACT_SCOPES",
    "CAPABILITY_ID_PATTERN",
    "CAPABILITY_KINDS",
    "CAPABILITY_REGISTRY_VERSION",
    "DEFAULT_CAPABILITY_PROFILE",
    "AuthorizationResult",
    "Capability",
    "CapabilityAuthorizationError",
    "CapabilityAuthorizationResult",
    "CapabilityRegistry",
    "CapabilityRegistrySnapshot",
    "CapabilityResourceLimits",
    "CapabilitySpec",
    "CapabilityUse",
    "CapabilityValidationError",
    "CapabilityValidationResult",
    "ARTIFACT_MANIFEST_SCHEMA_VERSION",
    "ARTIFACT_SCOPE_LOCK_FILENAME",
    "DEFAULT_WORKFLOW_HARD_LIMITS",
    "DEFAULT_ARTIFACT_MANIFEST_PATH",
    "ArtifactKey",
    "ArtifactRef",
    "ArtifactReference",
    "ArtifactFingerprint",
    "ArtifactManifest",
    "ArtifactManifestEntry",
    "ArtifactManifestStore",
    "ArtifactNamespace",
    "ArtifactNamespaceMap",
    "ArtifactNamespacePlan",
    "ArtifactNamespaceResult",
    "ArtifactOutputExpectation",
    "ArtifactOutputValidationError",
    "ArtifactOutputValidator",
    "ArtifactPathError",
    "ArtifactPathGuard",
    "ArtifactPathSafetyError",
    "ArtifactScopeLock",
    "CollectionExport",
    "CollectionExportDTO",
    "CollectionExportPlan",
    "InputBinding",
    "InputBindingDTO",
    "InputBindingPlan",
    "InputPlan",
    "LoopConfig",
    "LoopDTO",
    "LoopPlan",
    "LoopSource",
    "LoopSourceDTO",
    "LoopSourceBinding",
    "LoopSourcePlan",
    "LoopSourceProvider",
    "LoopSourceSnapshot",
    "LoopSourceSnapshotResult",
    "LoopItem",
    "MAX_CONFIG_BYTES",
    "MAX_CAPABILITY_RESOURCE_BYTES",
    "MAX_JSON_DEPTH",
    "MAX_WORKFLOW_BODY_STEPS",
    "MAX_WORKFLOW_CONFIG_BYTES",
    "MAX_WORKFLOW_ITEM_BYTES",
    "MAX_WORKFLOW_JSON_DEPTH",
    "MAX_WORKFLOW_LOOP_ITEMS",
    "MAX_WORKFLOW_LOOPS",
    "MAX_WORKFLOW_NODES",
    "MAX_WORKFLOW_PROMPT_INPUT_BYTES",
    "MAX_WORKFLOW_SNAPSHOT_BYTES",
    "MAX_WORKFLOW_STRING_BYTES",
    "MAX_WORKFLOW_TOTAL_STEPS",
    "MAX_ARTIFACT_MANIFEST_BYTES",
    "MAX_RUN_STATE_BYTES",
    "RUN_IDENTITY_SCHEMA_VERSION",
    "RUN_STATE_FILENAME",
    "RUN_STATE_SCHEMA_VERSION",
    "OutputDeclaration",
    "OutputDeclarationDTO",
    "OutputDeclarationPlan",
    "OutputPlan",
    "NormalizedWorkflow",
    "NormalizedWorkflowPlan",
    "PlanNode",
    "ResourceMetadata",
    "ResourceLimits",
    "RegistrySnapshot",
    "RunnerCapability",
    "SAFE_IDENTIFIER_PATTERN",
    "SAFE_PATH_SEGMENT_PATTERN",
    "SUPPORTED_WORKFLOW_SCHEMA_VERSIONS",
    "StepConfig",
    "StepDTO",
    "StepPlan",
    "LoopControllerCapability",
    "LifecycleCapability",
    "LoopSourceCapability",
    "VirtualInputCapability",
    "WORKFLOW_SCHEMA_VERSION",
    "WorkflowConfig",
    "WorkflowConfigDTO",
    "WorkflowConfigDiagnostic",
    "WorkflowConfigError",
    "WorkflowConfigLoadError",
    "WorkflowConfigLoader",
    "WorkflowConfigParseError",
    "WorkflowConfigValidationError",
    "WorkflowBoundsResult",
    "WorkflowEffectiveLimits",
    "WorkflowHardLimits",
    "WorkflowIdentity",
    "WorkflowLimits",
    "WorkflowLimitsDTO",
    "WorkflowNode",
    "WorkflowIR",
    "WorkflowNormalizer",
    "WorkflowPlan",
    "WorkflowRunIdentity",
    "WorkflowRunIdentityError",
    "WorkflowRunState",
    "WorkflowState",
    "RunIdentity",
    "RunState",
    "RunStateCorruptionError",
    "RunStateRepository",
    "RunStateStore",
    "ResumeStateError",
    "ResumeStateNotFoundError",
    "ResumeStateStore",
    "StaleResumeIdentityError",
    "authorize_workflow_capabilities",
    "default_capability_registry",
    "load_workflow_config",
    "load_workflow_config_text",
    "build_artifact_namespaces",
    "build_artifact_namespace_plan",
    "build_run_identity",
    "build_workflow_plan",
    "normalize_workflow",
    "normalize_workflow_config",
    "normalise_workflow_config",
    "parse_workflow_config",
    "canonical_json_bytes",
    "canonical_sha256",
    "canonical_workflow_digest",
    "compute_run_identity",
    "create_run_identity",
    "digest_input_snapshot",
    "precompute_artifact_namespaces",
    "preflight_loop_sources",
    "preflight_workflow_bounds",
    "snapshot_loop_source",
    "snapshot_loop_sources",
    "SystemResourceLimits",
    "validate_artifact_manifest",
    "validate_artifact_namespaces",
    "validate_capabilities",
    "validate_loop_source_bounds",
    "validate_workflow_bounds",
    "validate_workflow_capabilities",
    "validate_output_manifest",
    "validate_required_output_manifest",
    "validate_resume",
    "validate_resume_state",
    "validate_run_state",
    "workflow_plan_digest",
    "workflow_plan_payload",
    "WorkflowPreflightResult",
]
