"""Immutable contracts shared by workflow normalization phases."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _readonly_mapping(value: Mapping[object, object] | None) -> Mapping[object, object]:
    frozen = _freeze(value or {})
    assert isinstance(frozen, MappingProxyType)
    return frozen


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    """One ordered diagnostic emitted by a normalization phase."""

    phase: str
    code: str
    path: str
    message: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class RegistrationIndex:
    """Read-only indexes produced by declaration registration."""

    top_nodes: Mapping[object, object]
    body_nodes: Mapping[object, object]
    top_outputs: Mapping[object, object]
    body_outputs: Mapping[object, object]
    body_output_short: Mapping[object, object]
    exports: Mapping[object, object]
    artifact_graph: Mapping[object, object]
    declaration_order: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "top_nodes", _readonly_mapping(self.top_nodes))
        object.__setattr__(self, "body_nodes", _readonly_mapping(self.body_nodes))
        object.__setattr__(self, "top_outputs", _readonly_mapping(self.top_outputs))
        object.__setattr__(self, "body_outputs", _readonly_mapping(self.body_outputs))
        object.__setattr__(self, "body_output_short", _readonly_mapping(self.body_output_short))
        object.__setattr__(self, "exports", _readonly_mapping(self.exports))
        object.__setattr__(self, "artifact_graph", _readonly_mapping(self.artifact_graph))
        object.__setattr__(self, "declaration_order", tuple(self.declaration_order))


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """Registration output and diagnostics in declaration order.

    ``owner_token`` is the opaque identity token for the source DTO (assigned
    by the registration phase).  It is intentionally not part of value
    equality and prevents a result from being combined with another config.
    """

    index: RegistrationIndex
    diagnostics: tuple[DiagnosticEvent, ...] = ()
    owner_token: object = field(default_factory=object, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True, slots=True)
class ReferenceResolution:
    """Read-only resolved references and dependency values.

    ``registration_token`` prevents a plan builder from combining a
    resolution produced for one registration index with another index.
    """

    inputs: Mapping[object, object]
    dependencies: Mapping[object, object]
    loop_sources: Mapping[object, object]
    diagnostics: tuple[DiagnosticEvent, ...] = ()
    exports: Mapping[object, object] = field(default_factory=dict)
    explicit_dependencies: Mapping[object, object] = field(default_factory=dict)
    registration_token: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", _readonly_mapping(self.inputs))
        object.__setattr__(self, "dependencies", _readonly_mapping(self.dependencies))
        object.__setattr__(self, "loop_sources", _readonly_mapping(self.loop_sources))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "exports", _readonly_mapping(self.exports))
        object.__setattr__(self, "explicit_dependencies", _readonly_mapping(self.explicit_dependencies))
