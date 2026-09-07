"""Pure, dependency-injected workflow normalization phases.

The compatibility facade supplies the version-specific DTO, IR, and
diagnostic constructors.  The phases communicate through immutable boundary
objects and never import runner, executor, artifact, or state modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Literal, Mapping

try:
    from scripts.workflow_models import (
        DiagnosticEvent,
        ReferenceResolution,
        RegistrationIndex,
        RegistrationResult,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script-style imports
    from workflow_models import (  # type: ignore
        DiagnosticEvent,
        ReferenceResolution,
        RegistrationIndex,
        RegistrationResult,
    )


SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class NormalizationDependencies:
    """Runtime-owned DTO/IR constructors supplied by the compatibility facade."""

    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _NormalizationNodeInfo:
    local_id: str
    canonical_id: str
    config: Any
    index: int
    parent_loop_id: str | None = None


@dataclass(frozen=True, slots=True)
class _NormalizationContext:
    kind: Literal["top", "body"]
    top_index: int
    loop_id: str | None = None
    body_index: int | None = None


class _RegistrationWorkspace:
    """Mutable scratch space owned exclusively by declaration registration."""

    def __init__(self, config: Any, deps: NormalizationDependencies) -> None:
        values = deps.values
        self.config = config
        self.StepConfig = values["StepConfig"]
        self.LoopConfig = values["LoopConfig"]
        self.OutputDeclaration = values["OutputDeclaration"]
        self.CollectionExport = values["CollectionExport"]
        self.ArtifactKey = values["ArtifactKey"]
        self.IssueCollector = values["IssueCollector"]
        self.issues = self.IssueCollector()
        self.top_infos: dict[str, _NormalizationNodeInfo] = {}
        self.top_order: list[_NormalizationNodeInfo] = []
        self.body_infos: dict[str, dict[str, _NormalizationNodeInfo]] = {}
        self.body_order: dict[str, tuple[_NormalizationNodeInfo, ...]] = {}
        self.body_output_keys: dict[tuple[str, str, str], Any] = {}
        self.top_output_keys: dict[tuple[str, str], Any] = {}
        self.export_keys: dict[tuple[str, str], Any] = {}
        self.artifact_graph: dict[str, Any] = {}
        self.body_output_short: dict[tuple[str, str], list[tuple[str, Any]]] = {}

    def add_duplicate(self, path: str, label: str, value: str) -> None:
        self.issues.add("duplicate_id", path, f"duplicate {label} {value!r}")

    def register_top_nodes(self) -> None:
        for node_index, node in enumerate(self.config.nodes):
            path = f"/nodes/{node_index}"
            if not isinstance(node, (self.StepConfig, self.LoopConfig)):
                self.issues.add("wrong_type", path, "workflow node is not a supported DTO")
                continue
            local_id = node.id
            if not _normalization_identifier(local_id):
                self.issues.add("invalid_identifier", f"{path}/id", "node id is not a safe identifier")
                continue
            if local_id in self.top_infos:
                self.add_duplicate(f"{path}/id", "node id", local_id)
                continue
            info = _NormalizationNodeInfo(
                local_id=local_id,
                canonical_id=f"nodes/{local_id}",
                config=node,
                index=node_index,
            )
            self.top_infos[local_id] = info
            self.top_order.append(info)
            if isinstance(node, self.LoopConfig):
                body_map: dict[str, _NormalizationNodeInfo] = {}
                body_list: list[_NormalizationNodeInfo] = []
                for body_index, step in enumerate(node.body):
                    body_path = f"{path}/body/{body_index}"
                    if not isinstance(step, self.StepConfig):
                        self.issues.add("wrong_type", body_path, "loop body node must be a step")
                        continue
                    body_id = step.id
                    if not _normalization_identifier(body_id):
                        self.issues.add(
                            "invalid_identifier",
                            f"{body_path}/id",
                            "step id is not a safe identifier",
                        )
                        continue
                    if body_id in body_map:
                        self.add_duplicate(f"{body_path}/id", "body step id", body_id)
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
                self.body_infos[local_id] = body_map
                self.body_order[local_id] = tuple(body_list)

    def register_step_outputs(
        self,
        info: _NormalizationNodeInfo,
        *,
        loop_id: str | None,
        config_path: str,
    ) -> None:
        step = info.config
        if not isinstance(step, self.StepConfig):
            return
        seen: set[str] = set()
        for output_index, output in enumerate(step.outputs):
            output_path = f"{config_path}/outputs/{output_index}"
            if not isinstance(output, self.OutputDeclaration):
                self.issues.add("wrong_type", output_path, "output is not an OutputDeclaration")
                continue
            if output.id in seen:
                self.add_duplicate(f"{output_path}/id", "output id", output.id)
                continue
            seen.add(output.id)
            if not _normalization_identifier(output.id):
                self.issues.add(
                    "invalid_identifier",
                    f"{output_path}/id",
                    "output id is not a safe identifier",
                )
                continue
            scope: Literal["workflow", "loop_item"] = "loop_item" if loop_id is not None else "workflow"
            key = self.ArtifactKey(
                producer_node_id=info.canonical_id,
                output_id=output.id,
                scope=scope,
                cardinality="scalar",
            )
            if loop_id is None:
                self.top_output_keys[(info.local_id, output.id)] = key
            else:
                self.body_output_keys[(loop_id, info.local_id, output.id)] = key
                self.body_output_short.setdefault((info.local_id, output.id), []).append((loop_id, key))
            self.artifact_graph[key.reference] = key

    def register_outputs(self) -> None:
        for info in self.top_order:
            path = f"/nodes/{info.index}"
            if isinstance(info.config, self.StepConfig):
                self.register_step_outputs(info, loop_id=None, config_path=path)
                continue
            for body_info in self.body_order.get(info.local_id, ()):
                self.register_step_outputs(
                    body_info,
                    loop_id=info.local_id,
                    config_path=f"{path}/body/{body_info.index}",
                )
            loop = info.config
            seen: set[str] = set()
            for export_index, export in enumerate(loop.exports):
                export_path = f"{path}/exports/{export_index}"
                if not isinstance(export, self.CollectionExport):
                    self.issues.add("wrong_type", export_path, "loop export is not a CollectionExport")
                    continue
                if export.id in seen:
                    self.add_duplicate(f"{export_path}/id", "export id", export.id)
                    continue
                seen.add(export.id)
                if not _normalization_identifier(export.id):
                    self.issues.add(
                        "invalid_identifier",
                        f"{export_path}/id",
                        "export id is not a safe identifier",
                    )
                    continue
                key = self.ArtifactKey(
                    producer_node_id=info.canonical_id,
                    output_id=export.id,
                    scope="workflow",
                    cardinality="collection",
                )
                self.export_keys[(info.local_id, export.id)] = key
                self.artifact_graph[key.reference] = key

    def run(self) -> RegistrationResult:
        self.register_top_nodes()
        self.register_outputs()
        index = RegistrationIndex(
            top_nodes=self.top_infos,
            body_nodes=self.body_infos,
            top_outputs=self.top_output_keys,
            body_outputs=self.body_output_keys,
            body_output_short=self.body_output_short,
            exports=self.export_keys,
            artifact_graph=self.artifact_graph,
            declaration_order=tuple(item.canonical_id for item in self.top_order),
        )
        return RegistrationResult(
            index=index,
            diagnostics=_diagnostic_events(self.issues.items, "registration"),
            owner_token=self.config,
        )


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
    """Parse qualified artifact payloads without treating paths as filesystem paths."""

    if not payload:
        return None
    if payload.startswith("nodes/"):
        parts = payload.split("/")
        if len(parts) == 3 and parts[0] == "nodes":
            return ("top", parts[1], parts[2])
        if len(parts) == 5 and parts[0] == "nodes" and parts[2] == "body":
            return ("body", f"{parts[1]}:{parts[3]}", parts[4])
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
            return ("body", f"{parts[0]}:{parts[2]}", parts[3])
        if len(parts) != 2:
            return None
        left, right = parts
        if not left or not right:
            return None
        return ("top", left, right)
    return ("short", payload, None)


def _diagnostic_events(items: Any, phase: str) -> tuple[DiagnosticEvent, ...]:
    return tuple(
        DiagnosticEvent(
            phase=phase,
            code=str(item.code),
            path=str(item.path),
            message=str(item.message),
            ordinal=index,
        )
        for index, item in enumerate(items)
    )


def register_declarations(config: Any, *, deps: NormalizationDependencies) -> RegistrationResult:
    """Register nodes, body steps, outputs, and exports without resolving references."""

    workflow_config = deps.values["WorkflowConfig"]
    if not isinstance(config, workflow_config):
        raise TypeError("config must be a WorkflowConfig")
    return _RegistrationWorkspace(config, deps).run()


def _validate_registration_boundary(config: Any, registration: RegistrationResult) -> None:
    """Reject a registration result that was produced for another DTO instance."""

    if registration.owner_token is not config:
        raise ValueError("registration result does not belong to config")

    config_nodes: set[int] = set()
    for node in config.nodes:
        config_nodes.add(id(node))
        config_nodes.update(id(body_node) for body_node in getattr(node, "body", ()))
    for info in registration.index.top_nodes.values():
        if id(info.config) not in config_nodes:
            raise ValueError("registration result does not belong to config")
    for body_map in registration.index.body_nodes.values():
        for info in body_map.values():
            if id(info.config) not in config_nodes:
                raise ValueError("registration result does not belong to config")


class _ReferenceResolutionWorkspace:
    """Resolve references using only a registration boundary and the DTO."""

    def __init__(
        self,
        config: Any,
        registration: RegistrationResult,
        deps: NormalizationDependencies,
    ) -> None:
        _validate_registration_boundary(config, registration)
        values = deps.values
        self.config = config
        self.registration = registration
        self.StepConfig = values["StepConfig"]
        self.LoopConfig = values["LoopConfig"]
        self.InputBinding = values["InputBinding"]
        self.ArtifactReference = values["ArtifactReference"]
        self.InputBindingPlan = values["InputBindingPlan"]
        self.LoopSourceBinding = values["LoopSourceBinding"]
        self.CollectionExportPlan = values["CollectionExportPlan"]
        self.IssueCollector = values["IssueCollector"]
        index = registration.index
        self.top_infos = index.top_nodes
        self.top_order = tuple(
            self.top_infos[canonical_id.split("/", 1)[1]]
            for canonical_id in index.declaration_order
        )
        self.body_infos = index.body_nodes
        self.body_order = {
            loop_id: tuple(sorted(body_map.values(), key=lambda item: item.index))
            for loop_id, body_map in index.body_nodes.items()
        }
        self.body_output_keys = index.body_outputs
        self.top_output_keys = index.top_outputs
        self.export_keys = index.exports
        self.artifact_graph = index.artifact_graph
        self.body_output_short = index.body_output_short
        self.issues = self.IssueCollector()
        self.inputs: dict[str, tuple[Any, ...]] = {}
        self.dependencies: dict[str, tuple[str, ...]] = {}
        self.explicit_dependencies: dict[str, tuple[str, ...]] = {}
        self.loop_sources: dict[str, Any] = {}
        self.exports: dict[str, tuple[Any, ...]] = {}

    def add_duplicate(self, path: str, label: str, value: str) -> None:
        self.issues.add("duplicate_id", path, f"duplicate {label} {value!r}")

    def emit_reference_error(self, code: str, path: str, message: str) -> None:
        self.issues.add(code, path, message)

    def validate_cardinality(self, key: Any, expected: str | None, path: str) -> bool:
        if expected is not None and key.cardinality != expected:
            self.emit_reference_error(
                "cardinality_mismatch",
                path,
                f"artifact has cardinality {key.cardinality!r}, expected {expected!r}",
            )
            return False
        return True

    def find_top_output(self, node_id: str, output_id: str) -> Any | None:
        key = self.top_output_keys.get((node_id, output_id))
        if key is not None:
            return key
        return self.export_keys.get((node_id, output_id))

    def find_short_output(self, output_id: str) -> tuple[Any | None, bool]:
        matches = [
            key
            for (_node_id, candidate), key in (
                *self.top_output_keys.items(),
                *self.export_keys.items(),
            )
            if candidate == output_id
        ]
        unique = tuple(dict.fromkeys(matches))
        if len(unique) == 1:
            return unique[0], False
        if len(unique) > 1:
            return None, True
        return None, False

    def find_body_output(self, loop_id: str, body_id: str, output_id: str) -> Any | None:
        return self.body_output_keys.get((loop_id, body_id, output_id))

    def check_reachability(self, key: Any, context: _NormalizationContext, path: str) -> bool:
        producer = key.producer_node_id
        if key.scope == "loop_item":
            if context.kind != "body" or context.loop_id is None:
                self.emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "loop-item artifact cannot be consumed outside its loop item scope",
                )
                return False
            owner = producer.split("/body/", 1)
            if len(owner) != 2 or owner[0] != f"nodes/{context.loop_id}":
                self.emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "artifact belongs to a different loop item scope",
                )
                return False
            producer_info = self.body_infos.get(context.loop_id, {}).get(owner[1])
            if producer_info is None or context.body_index is None:
                self.emit_reference_error(
                    "undefined_reference",
                    path,
                    "artifact producer is not a declared loop step",
                )
                return False
            if producer_info.index >= context.body_index:
                self.emit_reference_error(
                    "unreachable_dependency",
                    path,
                    "artifact producer must be declared before its consumer in the loop body",
                )
                return False
            return True

        producer_info = next(
            (item for item in self.top_order if item.canonical_id == producer),
            None,
        )
        if producer_info is None:
            self.emit_reference_error("undefined_reference", path, "artifact producer is not declared")
            return False
        if producer_info.index >= context.top_index:
            self.emit_reference_error(
                "unreachable_dependency",
                path,
                "artifact producer must be declared before its consumer",
            )
            return False
        return True

    def resolve_artifact_reference(
        self,
        source: str,
        *,
        context: _NormalizationContext,
        path: str,
        allow_item_artifact: bool = True,
    ) -> Any | None:
        payload, expected = _normalization_expected_cardinality(source)
        syntax: Literal["artifact", "item-artifact"] | None = None
        if payload.startswith("artifact:"):
            syntax = "artifact"
            payload = payload[len("artifact:") :]
        elif payload.startswith("item-artifact:"):
            syntax = "item-artifact"
            payload = payload[len("item-artifact:") :]
        else:
            self.emit_reference_error(
                "undefined_reference",
                path,
                "artifact reference must start with 'artifact:' or 'item-artifact:'",
            )
            return None

        if syntax == "item-artifact":
            if not allow_item_artifact or context.kind != "body" or context.loop_id is None:
                self.emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "item-artifact reference is only valid inside its owning loop body",
                )
                return None
            if payload.startswith("nodes/") or ".body." in payload:
                parsed = _normalization_reference_parts(payload)
                if parsed is None or parsed[0] != "body" or ":" not in parsed[1]:
                    self.emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "item-artifact reference must identify a step in the current loop body",
                    )
                    return None
                owner, body_id = parsed[1].split(":", 1)
                if owner != context.loop_id:
                    self.emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "item-artifact reference belongs to a different loop",
                    )
                    return None
                output_id = parsed[2]
                if output_id is None:
                    self.emit_reference_error(
                        "undefined_reference",
                        path,
                        "item-artifact output is not declared",
                    )
                    return None
                key = self.find_body_output(owner, body_id, output_id)
                if key is None:
                    self.emit_reference_error(
                        "undefined_reference",
                        path,
                        "item-artifact producer or output is not declared",
                    )
                    return None
                if not self.validate_cardinality(key, expected, path):
                    return None
                if not self.check_reachability(key, context, path):
                    return None
                return self.ArtifactReference(source=source, key=key, expected_cardinality=expected)
            if "." not in payload:
                self.emit_reference_error(
                    "undefined_reference",
                    path,
                    "item-artifact reference must name step and output",
                )
                return None
            body_id, output_id = payload.split(".", 1)
            if not _normalization_identifier(body_id) or not _normalization_identifier(output_id):
                self.emit_reference_error(
                    "undefined_reference",
                    path,
                    "item-artifact reference is not well formed",
                )
                return None
            key = self.find_body_output(context.loop_id, body_id, output_id)
            if key is None:
                self.emit_reference_error(
                    "undefined_reference",
                    path,
                    "item-artifact producer or output is not declared",
                )
                return None
            if not self.validate_cardinality(key, expected, path):
                return None
            if not self.check_reachability(key, context, path):
                return None
            return self.ArtifactReference(source=source, key=key, expected_cardinality=expected)

        parsed = _normalization_reference_parts(payload)
        if parsed is None:
            self.emit_reference_error("undefined_reference", path, "artifact reference is not well formed")
            return None
        kind, node_or_body, output_id = parsed
        key: Any | None = None
        if kind == "body":
            if ":" not in node_or_body:
                self.emit_reference_error("undefined_reference", path, "body artifact reference is not well formed")
                return None
            loop_id, body_id = node_or_body.split(":", 1)
            key = self.find_body_output(loop_id, body_id, output_id or "")
            if key is not None:
                self.emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "loop-body output must be exported before it can cross the loop boundary",
                )
                self.emit_reference_error(
                    "cardinality_mismatch",
                    path,
                    "loop-body scalar output cannot be used as a workflow-scoped artifact",
                )
                return None
            self.emit_reference_error(
                "undefined_reference",
                path,
                "body artifact producer or output is not declared",
            )
            return None
        if kind == "short":
            key, ambiguous = self.find_short_output(node_or_body)
            if ambiguous:
                self.emit_reference_error(
                    "ambiguous_reference",
                    path,
                    "short artifact reference names multiple outputs",
                )
                return None
            if key is None:
                body_matches = [
                    body_key
                    for values in self.body_output_short.values()
                    for _loop_id, body_key in values
                    if body_key.output_id == node_or_body
                ]
                if body_matches:
                    self.emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "loop-body output must be referenced through an explicit collection export",
                    )
                else:
                    self.emit_reference_error("undefined_reference", path, "artifact output is not declared")
                return None
        else:
            if (
                output_id is None
                or not _normalization_identifier(node_or_body)
                or not _normalization_identifier(output_id)
            ):
                self.emit_reference_error(
                    "undefined_reference",
                    path,
                    "artifact reference is not well formed",
                )
                return None
            key = self.find_top_output(node_or_body, output_id)
            if key is None:
                if any(
                    body_step_id == node_or_body and body_output == output_id
                    for (_loop_id, body_step_id, body_output), _body_key in self.body_output_keys.items()
                ):
                    self.emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "loop-body output must be referenced through an explicit collection export",
                    )
                    return None
                if node_or_body in self.body_infos:
                    self.emit_reference_error(
                        "cross_scope_reference",
                        path,
                        "loop-body output must be referenced through an explicit collection export",
                    )
                else:
                    self.emit_reference_error(
                        "undefined_reference",
                        path,
                        "artifact producer or output is not declared",
                    )
                return None

        assert key is not None
        if not self.validate_cardinality(key, expected, path):
            return None
        if not self.check_reachability(key, context, path):
            return None
        return self.ArtifactReference(source=source, key=key, expected_cardinality=expected)

    def resolve_body_export_source(
        self,
        loop_id: str,
        source: str,
        path: str,
    ) -> Any | None:
        payload, expected = _normalization_expected_cardinality(source)
        key: Any | None = None
        if payload.startswith("item-artifact:"):
            payload = payload[len("item-artifact:") :]
        elif payload.startswith("artifact:"):
            payload = payload[len("artifact:") :]
        if payload.startswith("nodes/"):
            parsed = _normalization_reference_parts(payload)
            if parsed is None or parsed[0] != "body":
                self.emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "collection export source must identify a step in this loop body",
                )
                return None
            owner, body_id = parsed[1].split(":", 1)
            if owner != loop_id:
                self.emit_reference_error(
                    "cross_scope_reference",
                    path,
                    "collection export source belongs to another loop",
                )
                return None
            output_id = parsed[2]
        else:
            dotted_parts = payload.split(".")
            if len(dotted_parts) == 4 and dotted_parts[1] == "body":
                owner, body_id, output_id = dotted_parts[0], dotted_parts[2], dotted_parts[3]
                if owner != loop_id:
                    self.emit_reference_error(
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
                self.emit_reference_error("undefined_reference", path, "collection export source is not well formed")
                return None
            if not _normalization_identifier(output_id):
                self.emit_reference_error("undefined_reference", path, "collection export source is not well formed")
                return None
            if not body_id:
                candidates = [
                    candidate_key
                    for (_candidate_step, candidate_output), values in self.body_output_short.items()
                    if candidate_output == output_id
                    for candidate_loop, candidate_key in values
                    if candidate_loop == loop_id
                ]
                unique = tuple(dict.fromkeys(candidates))
                if len(unique) != 1:
                    code = "ambiguous_reference" if len(unique) > 1 else "undefined_reference"
                    self.emit_reference_error(code, path, "collection export output is not uniquely declared")
                    return None
                key = unique[0]
            else:
                key = self.find_body_output(loop_id, body_id, output_id)
        if key is None:
            key = self.find_body_output(loop_id, body_id, output_id)
        if key is None:
            self.emit_reference_error(
                "undefined_reference",
                path,
                "collection export source is not declared in this loop body",
            )
            return None
        if key.cardinality != "scalar":
            self.emit_reference_error(
                "cardinality_mismatch",
                path,
                "collection export source must be an item scalar",
            )
            return None
        if expected is not None and expected != "scalar":
            self.emit_reference_error(
                "cardinality_mismatch",
                path,
                "collection export source must be scalar",
            )
            return None
        return self.ArtifactReference(source=source, key=key, expected_cardinality="scalar")

    def resolve_explicit_dependencies(
        self,
        raw_dependencies: tuple[str, ...],
        *,
        context: _NormalizationContext,
        path: str,
    ) -> tuple[str, ...]:
        resolved: list[str] = []
        seen: set[str] = set()
        sibling_map = self.top_infos if context.kind == "top" else self.body_infos.get(context.loop_id or "", {})
        for dependency_index, dependency in enumerate(raw_dependencies):
            dependency_path = f"{path}/{dependency_index}"
            if dependency in seen:
                self.add_duplicate(dependency_path, "dependency", dependency)
                continue
            seen.add(dependency)
            target = sibling_map.get(dependency)
            if target is None:
                other_scope = (
                    any(dependency in mapping for mapping in self.body_infos.values())
                    if context.kind == "top"
                    else dependency in self.top_infos
                    or any(
                        dependency in mapping
                        for loop_id, mapping in self.body_infos.items()
                        if loop_id != context.loop_id
                    )
                )
                self.emit_reference_error(
                    "cross_scope_reference" if other_scope else "undefined_reference",
                    dependency_path,
                    "dependency must reference a node in the same container",
                )
                continue
            if context.kind == "top":
                if target.index >= context.top_index:
                    self.emit_reference_error(
                        "unreachable_dependency",
                        dependency_path,
                        "dependency must be declared before its consumer",
                    )
                    resolved.append(target.canonical_id)
                    continue
            elif context.body_index is None or target.index >= context.body_index:
                self.emit_reference_error(
                    "unreachable_dependency",
                    dependency_path,
                    "dependency must be declared before its consumer in the loop body",
                )
                resolved.append(target.canonical_id)
                continue
            resolved.append(target.canonical_id)
        return tuple(resolved)

    def resolve_step(
        self,
        info: _NormalizationNodeInfo,
        *,
        context: _NormalizationContext,
        path: str,
    ) -> None:
        step = info.config
        assert isinstance(step, self.StepConfig)
        explicit = self.resolve_explicit_dependencies(
            step.depends_on,
            context=context,
            path=f"{path}/depends_on",
        )
        inputs: list[Any] = []
        implicit: list[str] = []
        input_names: set[str] = set()
        for input_index, binding in enumerate(step.inputs):
            input_path = f"{path}/inputs/{input_index}"
            if not isinstance(binding, self.InputBinding):
                self.issues.add("wrong_type", input_path, "input is not an InputBinding")
                continue
            if binding.name in input_names:
                self.add_duplicate(f"{input_path}/name", "input name", binding.name)
                continue
            input_names.add(binding.name)
            if binding.source.startswith("$"):
                if binding.source == "$loop_item" and context.kind != "body":
                    self.emit_reference_error(
                        "cross_scope_reference",
                        f"{input_path}/from",
                        "$loop_item is only available inside a loop body",
                    )
                inputs.append(
                    self.InputBindingPlan(
                        name=binding.name,
                        source=binding.source,
                        virtual_input=binding.source,
                    )
                )
                continue
            reference = self.resolve_artifact_reference(
                binding.source,
                context=context,
                path=f"{input_path}/from",
            )
            if reference is None:
                continue
            inputs.append(
                self.InputBindingPlan(
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
        self.inputs[info.canonical_id] = tuple(inputs)
        self.dependencies[info.canonical_id] = tuple(dependencies)
        self.explicit_dependencies[info.canonical_id] = tuple(explicit)

    def run(self) -> ReferenceResolution:
        for info in self.top_order:
            path = f"/nodes/{info.index}"
            if isinstance(info.config, self.StepConfig):
                self.resolve_step(
                    info,
                    context=_NormalizationContext(kind="top", top_index=info.index),
                    path=path,
                )
                continue

            loop = info.config
            source_reference: Any | None = None
            source_virtual: str | None = None
            if loop.source.source.startswith("$"):
                source_virtual = loop.source.source
            else:
                source_reference = self.resolve_artifact_reference(
                    loop.source.source,
                    context=_NormalizationContext(kind="top", top_index=info.index),
                    path=f"{path}/source/from",
                    allow_item_artifact=False,
                )
            source_binding = self.LoopSourceBinding(
                source=loop.source.source,
                provider=loop.source.provider,
                artifact=source_reference,
                virtual_input=source_virtual,
            )
            self.loop_sources[info.canonical_id] = source_binding
            loop_dependencies: list[str] = []
            if source_reference is not None:
                loop_dependencies.append(source_reference.key.producer_node_id)

            for body_info in self.body_order.get(info.local_id, ()):
                self.resolve_step(
                    body_info,
                    context=_NormalizationContext(
                        kind="body",
                        top_index=info.index,
                        loop_id=info.local_id,
                        body_index=body_info.index,
                    ),
                    path=f"{path}/body/{body_info.index}",
                )
                for input_binding in self.inputs.get(body_info.canonical_id, ()):
                    if (
                        input_binding.artifact is not None
                        and input_binding.artifact.key.scope == "workflow"
                        and input_binding.artifact.key.producer_node_id not in loop_dependencies
                    ):
                        loop_dependencies.append(input_binding.artifact.key.producer_node_id)

            export_plans: list[Any] = []
            for export_index, export in enumerate(loop.exports):
                export_key = self.export_keys.get((info.local_id, export.id))
                if export_key is None:
                    continue
                source_reference = self.resolve_body_export_source(
                    info.local_id,
                    export.source,
                    f"{path}/exports/{export_index}/from",
                )
                if source_reference is None:
                    continue
                export_plans.append(
                    self.CollectionExportPlan(
                        id=export.id,
                        source=export.source,
                        cardinality="collection",
                        source_artifact=source_reference.key,
                        artifact_key=export_key,
                    )
                )
            self.dependencies[info.canonical_id] = tuple(loop_dependencies)
            self.explicit_dependencies[info.canonical_id] = ()
            self.exports[info.canonical_id] = tuple(export_plans)

        return ReferenceResolution(
            inputs=self.inputs,
            dependencies=self.dependencies,
            loop_sources=self.loop_sources,
            diagnostics=_diagnostic_events(self.issues.items, "reference_resolution"),
            exports=self.exports,
            explicit_dependencies=self.explicit_dependencies,
            registration_token=self.registration.owner_token,
        )


def resolve_references(
    config: Any,
    registration: RegistrationResult,
    *,
    deps: NormalizationDependencies,
) -> ReferenceResolution:
    """Resolve references from the immutable registration index."""

    workflow_config = deps.values["WorkflowConfig"]
    if not isinstance(config, workflow_config):
        raise TypeError("config must be a WorkflowConfig")
    if not isinstance(registration, RegistrationResult):
        raise TypeError("registration must be a RegistrationResult")
    return _ReferenceResolutionWorkspace(config, registration, deps).run()


def _known_node_ids(registration: RegistrationResult) -> set[str]:
    result = set(registration.index.declaration_order)
    for body_map in registration.index.body_nodes.values():
        result.update(info.canonical_id for info in body_map.values())
    return result


def _validate_resolution_boundary(
    registration: RegistrationResult,
    resolution: ReferenceResolution,
) -> None:
    if (
        resolution.registration_token is not None
        and resolution.registration_token is not registration.owner_token
    ):
        raise ValueError("resolution result does not belong to registration")
    known = _known_node_ids(registration)
    for label, mapping in (
        ("inputs", resolution.inputs),
        ("dependencies", resolution.dependencies),
        ("explicit_dependencies", resolution.explicit_dependencies),
        ("loop_sources", resolution.loop_sources),
        ("exports", resolution.exports),
    ):
        unknown = set(mapping) - known
        if unknown:
            raise ValueError(f"resolution {label} contains unknown node ids: {sorted(unknown)!r}")


def _build_output_plans(
    info: _NormalizationNodeInfo,
    *,
    loop_id: str | None,
    registration: RegistrationResult,
    deps: NormalizationDependencies,
) -> tuple[Any, ...]:
    OutputPlan = deps.values["OutputPlan"]
    StepConfig = deps.values["StepConfig"]
    step = info.config
    if not isinstance(step, StepConfig):
        return ()
    output_keys = (
        registration.index.body_outputs
        if loop_id is not None
        else registration.index.top_outputs
    )
    result: list[Any] = []
    for output in step.outputs:
        key = output_keys.get(
            (loop_id, info.local_id, output.id)
            if loop_id is not None
            else (info.local_id, output.id)
        )
        if key is not None:
            result.append(OutputPlan(id=output.id, kind=output.kind, path=output.path, artifact_key=key))
    return tuple(result)


def build_workflow_plan(
    config: Any,
    registration: RegistrationResult,
    resolution: ReferenceResolution,
    *,
    deps: NormalizationDependencies,
) -> Any:
    """Build the immutable IR exclusively from registration and resolution outputs."""

    values = deps.values
    WorkflowConfig = values["WorkflowConfig"]
    StepConfig = values["StepConfig"]
    LoopConfig = values["LoopConfig"]
    StepPlan = values["StepPlan"]
    LoopPlan = values["LoopPlan"]
    WorkflowPlan = values["WorkflowPlan"]
    if not isinstance(config, WorkflowConfig):
        raise TypeError("config must be a WorkflowConfig")
    if not isinstance(registration, RegistrationResult):
        raise TypeError("registration must be a RegistrationResult")
    if not isinstance(resolution, ReferenceResolution):
        raise TypeError("resolution must be a ReferenceResolution")
    _validate_registration_boundary(config, registration)
    _validate_resolution_boundary(registration, resolution)

    top_infos = registration.index.top_nodes
    body_infos = registration.index.body_nodes
    normalized_nodes: list[Any] = []
    for canonical_id in registration.index.declaration_order:
        local_id = canonical_id.split("/", 1)[1]
        info = top_infos[local_id]
        if isinstance(info.config, StepConfig):
            normalized_nodes.append(
                StepPlan(
                    canonical_id=info.canonical_id,
                    local_id=info.local_id,
                    lifecycle=info.config.lifecycle,
                    runner=info.config.runner,
                    prompt=info.config.prompt,
                    skill=info.config.skill,
                    inputs=tuple(resolution.inputs.get(info.canonical_id, ())),
                    outputs=_build_output_plans(info, loop_id=None, registration=registration, deps=deps),
                    dependencies=tuple(resolution.dependencies.get(info.canonical_id, ())),
                    explicit_dependencies=tuple(
                        resolution.explicit_dependencies.get(info.canonical_id, ())
                    ),
                )
            )
            continue

        loop = info.config
        if not isinstance(loop, LoopConfig):
            continue
        source = resolution.loop_sources.get(info.canonical_id)
        if source is None:
            raise ValueError(f"resolution is missing loop source for {info.canonical_id}")
        body_plans: list[Any] = []
        body_map = body_infos.get(info.local_id, {})
        for body_info in sorted(body_map.values(), key=lambda item: item.index):
            body_plans.append(
                StepPlan(
                    canonical_id=body_info.canonical_id,
                    local_id=body_info.local_id,
                    lifecycle=body_info.config.lifecycle,
                    runner=body_info.config.runner,
                    prompt=body_info.config.prompt,
                    skill=body_info.config.skill,
                    inputs=tuple(resolution.inputs.get(body_info.canonical_id, ())),
                    outputs=_build_output_plans(
                        body_info,
                        loop_id=info.local_id,
                        registration=registration,
                        deps=deps,
                    ),
                    dependencies=tuple(resolution.dependencies.get(body_info.canonical_id, ())),
                    explicit_dependencies=tuple(
                        resolution.explicit_dependencies.get(body_info.canonical_id, ())
                    ),
                )
            )
        normalized_nodes.append(
            LoopPlan(
                canonical_id=info.canonical_id,
                local_id=info.local_id,
                source=source,
                max_items=loop.max_items,
                controller=loop.controller,
                body=tuple(body_plans),
                exports=tuple(resolution.exports.get(info.canonical_id, ())),
                dependencies=tuple(resolution.dependencies.get(info.canonical_id, ())),
            )
        )

    dependency_graph: dict[str, tuple[str, ...]] = {}
    for node in normalized_nodes:
        dependency_graph[node.canonical_id] = tuple(node.dependencies)
        if isinstance(node, LoopPlan):
            for body_step in node.body:
                dependency_graph[body_step.canonical_id] = tuple(body_step.dependencies)
    return WorkflowPlan(
        schema_version=config.schema_version,
        workflow_id=config.id,
        profile=config.profile,
        limits=config.limits,
        nodes=tuple(normalized_nodes),
        dependency_graph=dependency_graph,
        artifact_graph=registration.index.artifact_graph,
    )


def validate_dependency_graph(
    plan: Any,
    *,
    deps: NormalizationDependencies | None = None,
) -> tuple[DiagnosticEvent, ...]:
    """Validate cycles in an already constructed dependency graph."""

    graph = plan.dependency_graph
    visit_state: dict[str, int] = {}
    cycle_signatures: set[tuple[str, ...]] = set()
    diagnostics: list[DiagnosticEvent] = []

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
                diagnostics.append(
                    DiagnosticEvent(
                        phase="graph_validation",
                        code="dependency_cycle",
                        path="/nodes",
                        message="dependency cycle: " + " -> ".join(cycle),
                        ordinal=len(diagnostics),
                    )
                )
            return
        visit_state[node_id] = 1
        stack.append(node_id)
        for dependency in graph.get(node_id, ()):
            if dependency in graph:
                visit(dependency, stack)
        stack.pop()
        visit_state[node_id] = 2

    for node_id in graph:
        visit(node_id, [])
    return tuple(diagnostics)


def _raise_phase_diagnostics(
    events: tuple[DiagnosticEvent, ...],
    *,
    source_path: Path | str | None,
    deps: NormalizationDependencies,
) -> None:
    if not events:
        return
    issues = deps.values["IssueCollector"]()
    for event in events:
        issues.add(event.code, event.path, event.message)
    source_path_value = Path(source_path) if source_path is not None else None
    issues.raise_if_any(source_path=source_path_value)


def normalize_declaration(
    config: Any,
    *,
    source_path: Path | str | None = None,
    deps: NormalizationDependencies,
) -> Any:
    """Run registration, reference resolution, plan construction, and graph validation once."""

    registration = register_declarations(config, deps=deps)
    resolution = resolve_references(config, registration, deps=deps)
    plan = build_workflow_plan(config, registration, resolution, deps=deps)
    graph_diagnostics = validate_dependency_graph(plan, deps=deps)
    diagnostics = registration.diagnostics + resolution.diagnostics + graph_diagnostics
    _raise_phase_diagnostics(diagnostics, source_path=source_path, deps=deps)
    return plan
