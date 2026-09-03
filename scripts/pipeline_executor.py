"""Validated workflow preparation and the generic configured pipeline executor.

The executor in this module deliberately owns only structural execution:
declaration-order traversal, loop-item context, artifact input resolution, and
stop-on-error behavior.  Runner command resolution and lifecycle/policy
meaning remain behind the :class:`StepExecutionPort` supplied by a caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Literal, Mapping, Protocol, Sequence

try:
    from scripts.workflow_config import (
        ArtifactKey,
        ArtifactManifest,
        ArtifactManifestEntry,
        ArtifactManifestStore,
        ArtifactNamespace,
        ArtifactNamespacePlan,
        ArtifactOutputValidationError,
        ArtifactOutputValidator,
        ArtifactPathGuard,
        ArtifactPathSafetyError,
        ArtifactReference,
        CapabilityAuthorizationResult,
        CapabilityRegistry,
        CapabilityRegistrySnapshot,
        InputBindingPlan,
        LoopSourceSnapshot,
        LoopPlan,
        LoopItem,
        RunState,
        RunStateStore,
        RunStateCorruptionError,
        ResumeStateError,
        ResumeStateNotFoundError,
        RunIdentity,
        StaleResumeIdentityError,
        WorkflowBoundsResult,
        WorkflowConfig,
        WorkflowEffectiveLimits,
        WorkflowHardLimits,
        WorkflowPlan,
        WorkflowRunIdentity,
        OutputPlan,
        StepPlan,
        build_artifact_namespace_plan,
        build_run_identity,
        compute_run_identity,
        create_run_identity,
        default_capability_registry,
        normalize_workflow_config,
        preflight_workflow_bounds,
        validate_artifact_manifest,
        validate_resume_state,
        validate_workflow_capabilities,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script-style imports
    from workflow_config import (  # type: ignore
        ArtifactKey,
        ArtifactManifest,
        ArtifactManifestEntry,
        ArtifactManifestStore,
        ArtifactNamespace,
        ArtifactNamespacePlan,
        ArtifactOutputValidationError,
        ArtifactOutputValidator,
        ArtifactPathGuard,
        ArtifactPathSafetyError,
        ArtifactReference,
        CapabilityAuthorizationResult,
        CapabilityRegistry,
        CapabilityRegistrySnapshot,
        InputBindingPlan,
        LoopSourceSnapshot,
        LoopPlan,
        LoopItem,
        RunState,
        RunStateStore,
        RunStateCorruptionError,
        ResumeStateError,
        ResumeStateNotFoundError,
        RunIdentity,
        StaleResumeIdentityError,
        WorkflowBoundsResult,
        WorkflowConfig,
        WorkflowEffectiveLimits,
        WorkflowHardLimits,
        WorkflowPlan,
        WorkflowRunIdentity,
        OutputPlan,
        StepPlan,
        build_artifact_namespace_plan,
        build_run_identity,
        compute_run_identity,
        create_run_identity,
        default_capability_registry,
        normalize_workflow_config,
        preflight_workflow_bounds,
        validate_artifact_manifest,
        validate_resume_state,
        validate_workflow_capabilities,
    )


@dataclass(frozen=True, slots=True)
class ValidatedWorkflowPlan:
    """All immutable inputs needed before a pipeline executor may run."""

    plan: WorkflowPlan
    capability_authorization: CapabilityAuthorizationResult
    bounds: WorkflowBoundsResult
    artifact_namespaces: ArtifactNamespacePlan
    identity: WorkflowRunIdentity
    repo_root: Path
    artifact_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.plan, WorkflowPlan):
            raise TypeError("validated plan requires a WorkflowPlan")
        if not isinstance(self.capability_authorization, CapabilityAuthorizationResult):
            raise TypeError("validated plan requires capability authorization")
        if not isinstance(self.bounds, WorkflowBoundsResult):
            raise TypeError("validated plan requires bounded source snapshots")
        if self.bounds.plan.workflow_digest != self.plan.workflow_digest:
            raise ValueError("bounded source snapshots belong to another workflow plan")
        if not isinstance(self.artifact_namespaces, ArtifactNamespacePlan):
            raise TypeError("validated plan requires an artifact namespace plan")
        if not isinstance(self.identity, WorkflowRunIdentity):
            raise TypeError("validated plan requires a WorkflowRunIdentity")
        if self.identity.workflow_id != self.plan.workflow_id:
            raise ValueError("run identity workflow does not match validated plan")
        object.__setattr__(self, "repo_root", Path(self.repo_root).absolute())
        object.__setattr__(self, "artifact_root", Path(self.artifact_root).absolute())

    @property
    def normalized_plan(self) -> WorkflowPlan:
        return self.plan

    @property
    def run_identity(self) -> WorkflowRunIdentity:
        return self.identity

    @property
    def identity_digest(self) -> str:
        return self.identity.digest

    @property
    def registry_snapshot(self) -> CapabilityRegistrySnapshot:
        return self.capability_authorization.snapshot

    @property
    def source_snapshots(self) -> Mapping[str, LoopSourceSnapshot]:
        return self.bounds.snapshots

    @property
    def namespace_plan(self) -> ArtifactNamespacePlan:
        return self.artifact_namespaces

    @property
    def effective_limits(self) -> WorkflowEffectiveLimits:
        return self.bounds.effective_limits

    def initial_state(self) -> RunState:
        """Construct state without writing a file or creating an artifact root."""

        return RunState.initial(self.identity, self.plan.workflow_id)

    new_state = initial_state

    def state_store(self, *, filename: str | None = None) -> RunStateStore:
        if filename is None:
            return RunStateStore(self.artifact_root)
        return RunStateStore(self.artifact_root, filename=filename)

    def persist_initial_state(self, store: RunStateStore | None = None) -> RunState:
        """Persist initial state only after this validated object exists."""

        target = store or self.state_store()
        state = self.initial_state()
        target.save(state, expected_identity=self.identity)
        return state

    def validate_resume(
        self,
        state: RunState | Mapping[str, object] | RunStateStore,
        *,
        expected_namespaces: ArtifactNamespacePlan | Iterable[ArtifactNamespace] | None = None,
    ) -> RunState:
        if isinstance(state, RunStateStore):
            state = state.load_for_resume(
                self.identity,
                workflow_id=self.plan.workflow_id,
            )
        validated = validate_resume_state(
            state,
            self.identity,
            artifact_root=self.artifact_root,
            expected_namespaces=expected_namespaces,
            workflow_id=self.plan.workflow_id,
        )
        known_instances: set[str] = set()
        for node in self.plan.nodes:
            if isinstance(node, StepPlan):
                known_instances.add(node.canonical_id)
            elif isinstance(node, LoopPlan):
                known_instances.add(node.canonical_id)
                for item in self.bounds.snapshots[node.local_id].items:
                    known_instances.add(f"{node.canonical_id}@{item.item_id}")
                    for body_step in node.body:
                        known_instances.add(f"{body_step.canonical_id}@{item.item_id}")
        unknown_instances = set(validated.completed_instances) - known_instances
        if unknown_instances:
            raise RunStateCorruptionError(
                "persisted workflow state contains unknown completed instances: "
                + ", ".join(sorted(unknown_instances))
            )
        return validated

    resume = validate_resume

    def load_resume_state(
        self,
        store: RunStateStore | None = None,
        *,
        expected_namespaces: ArtifactNamespacePlan | Iterable[ArtifactNamespace] | None = None,
    ) -> RunState:
        target = store or self.state_store()
        state = target.load_for_resume(
            self.identity,
            artifact_root=self.artifact_root,
            workflow_id=self.plan.workflow_id,
        )
        if state.output_manifest is not None and expected_namespaces is not None:
            state = self.validate_resume(state, expected_namespaces=expected_namespaces)
        return state

    def pending_instances(
        self,
        state: RunState,
        node_instance_ids: Iterable[str],
    ) -> tuple[str, ...]:
        self.validate_resume(state)
        return state.pending_instances(node_instance_ids)


ValidatedPlan = ValidatedWorkflowPlan
PreparedWorkflow = ValidatedWorkflowPlan


def _registry_for_validation(
    registry: object,
    authorization: CapabilityAuthorizationResult | None,
) -> object:
    if authorization is not None:
        if registry is None:
            return authorization.snapshot
        if isinstance(registry, CapabilityAuthorizationResult):
            if registry is not authorization:
                raise ValueError("conflicting capability authorization results")
            return authorization.snapshot
        return registry
    if isinstance(registry, CapabilityAuthorizationResult):
        return registry.snapshot
    return registry


def prepare_workflow_run(
    workflow: WorkflowConfig | WorkflowPlan,
    *,
    repo_root: Path | str,
    artifact_root: Path | str | None = None,
    registry: CapabilityRegistry | CapabilityRegistrySnapshot | CapabilityAuthorizationResult | None = None,
    capability_authorization: CapabilityAuthorizationResult | None = None,
    providers: object = None,
    provider_registry: object = None,
    bounds: WorkflowBoundsResult | None = None,
    hard_limits: WorkflowHardLimits | None = None,
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
    item_namespace: str = "default",
) -> ValidatedWorkflowPlan:
    """Run all WB-06 preflight steps without writing state or artifacts.

    The only method in this module that can create the artifact root is
    :meth:`ValidatedWorkflowPlan.persist_initial_state`; preparation itself
    performs read-only validation and digesting.
    """

    if isinstance(workflow, WorkflowConfig):
        config = workflow
        plan = normalize_workflow_config(config)
        validation_registry = _registry_for_validation(registry, capability_authorization)
        # Even when a caller supplies an earlier authorization result, read
        # the current resource files again.  Resume must become stale when a
        # prompt/skill changes between runs; a cached authorization result is
        # only a registry snapshot, not a freshness guarantee.
        authorization = validate_workflow_capabilities(
            config,
            validation_registry,  # type: ignore[arg-type]
            repo_root=repo_root,
        )
    elif isinstance(workflow, WorkflowPlan):
        plan = workflow
        if capability_authorization is None:
            raise ValueError(
                "prepare_workflow_run requires WorkflowConfig or a prior capability authorization"
            )
        authorization = capability_authorization
    else:
        raise TypeError("workflow must be a WorkflowConfig or WorkflowPlan")

    if authorization.snapshot.profile != plan.profile:
        raise ValueError("capability authorization profile does not match workflow plan")

    if provider_registry is not None and providers is not None:
        raise ValueError("provide only one of providers and provider_registry")
    if bounds is None:
        bounds = preflight_workflow_bounds(
            plan,
            providers,
            provider_registry=provider_registry,
            registry=authorization.snapshot,
            hard_limits=hard_limits,
        )
    elif not isinstance(bounds, WorkflowBoundsResult):
        raise TypeError("bounds must be a WorkflowBoundsResult")
    elif bounds.plan.workflow_digest != plan.workflow_digest:
        raise ValueError("bounds belong to another workflow plan")

    resolved_artifact_root = (
        Path(artifact_root)
        if artifact_root is not None
        else Path(repo_root) / ".kelpie" / "artifacts" / plan.workflow_id
    )
    namespaces = build_artifact_namespace_plan(
        plan,
        bounds,
        artifact_root=resolved_artifact_root,
        item_namespace=item_namespace,  # type: ignore[arg-type]
    )
    identity = build_run_identity(
        plan,
        authorization.snapshot,
        capability_authorization=authorization,
        effective_runner_config=effective_runner_config,
        runner_config=runner_config,
        runner_configs=runner_configs,
        resource_digests=resource_digests,
        resources=resources,
        input_snapshots=input_snapshots,
        issue_snapshot=issue_snapshot,
        issue=issue,
        repo_instructions_snapshot=repo_instructions_snapshot,
        repo_instructions=repo_instructions,
        source_snapshots=bounds,
        artifact_namespace_plan=namespaces,
        effective_limits=bounds.effective_limits,
    )
    return ValidatedWorkflowPlan(
        plan=plan,
        capability_authorization=authorization,
        bounds=bounds,
        artifact_namespaces=namespaces,
        identity=identity,
        repo_root=Path(repo_root),
        artifact_root=resolved_artifact_root,
    )


validate_workflow_run = prepare_workflow_run
build_validated_workflow_plan = prepare_workflow_run
prepare_validated_plan = prepare_workflow_run


def _freeze_runtime_value(value: object) -> object:
    """Freeze JSON-like values before putting them on an execution request."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze_runtime_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_runtime_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_runtime_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class LoopItemContext:
    """The immutable context exposed to one loop-body step."""

    loop_id: str
    item_id: str
    position: int
    value: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.loop_id, str) or not self.loop_id:
            raise ValueError("loop context requires a loop id")
        if not isinstance(self.item_id, str) or not self.item_id:
            raise ValueError("loop context requires an item id")
        if isinstance(self.position, bool) or not isinstance(self.position, int) or self.position < 0:
            raise ValueError("loop context position must be a non-negative integer")
        if not isinstance(self.value, Mapping):
            raise TypeError("loop context value must be a mapping")
        frozen = _freeze_runtime_value(dict(self.value))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "value", frozen)

    @classmethod
    def from_item(cls, loop_id: str, item: LoopItem) -> "LoopItemContext":
        if not isinstance(item, LoopItem):
            raise TypeError("loop item context requires a LoopItem")
        return cls(
            loop_id=loop_id,
            item_id=item.item_id,
            position=item.position,
            value=item.payload,
        )

    @property
    def id(self) -> str:
        return self.item_id

    @property
    def index(self) -> int:
        return self.position

    @property
    def payload(self) -> Mapping[str, object]:
        return self.value

    @property
    def data(self) -> Mapping[str, object]:
        return self.value

    @property
    def item(self) -> Mapping[str, object]:
        return self.value

    @property
    def loop_item(self) -> Mapping[str, object]:
        return self.value

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.item_id,
            "position": self.position,
            "payload": _thaw_runtime_value(self.value),
        }


def _thaw_runtime_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_runtime_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_runtime_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_thaw_runtime_value(item) for item in sorted(value, key=repr)]
    return value


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    """One validated physical artifact made available to a consumer step."""

    key: ArtifactKey
    namespace: ArtifactNamespace
    path: Path
    manifest_entry: ArtifactManifestEntry | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, ArtifactKey):
            raise TypeError("resolved artifact requires an ArtifactKey")
        if not isinstance(self.namespace, ArtifactNamespace):
            raise TypeError("resolved artifact requires an ArtifactNamespace")
        if self.namespace.artifact_key != self.key:
            raise ValueError("resolved artifact key does not match its namespace")
        object.__setattr__(self, "path", Path(self.path).absolute())
        if self.manifest_entry is not None and not isinstance(
            self.manifest_entry,
            ArtifactManifestEntry,
        ):
            raise TypeError("resolved artifact manifest entry has an invalid type")

    @property
    def artifact_key(self) -> ArtifactKey:
        return self.key

    @property
    def artifact(self) -> ArtifactKey:
        return self.key

    @property
    def relative_path(self) -> str:
        return self.namespace.relative_path

    @property
    def entry(self) -> ArtifactManifestEntry | None:
        return self.manifest_entry

    @property
    def freshness(self) -> str | None:
        return self.manifest_entry.freshness if self.manifest_entry is not None else None


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    """A named input whose value is either virtual data or checked artifact paths."""

    name: str
    source: str
    value: object
    artifact: ArtifactReference | None = None
    artifacts: tuple[ResolvedArtifact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("resolved input name must be a non-empty string")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("resolved input source must be a non-empty string")
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if any(not isinstance(item, ResolvedArtifact) for item in self.artifacts):
            raise TypeError("resolved input artifacts must be ResolvedArtifact values")
        if self.artifact is not None and not isinstance(self.artifact, ArtifactReference):
            raise TypeError("resolved input artifact reference has an invalid type")

    @property
    def selector(self) -> str:
        return self.source

    @property
    def from_(self) -> str:
        return self.source

    @property
    def from_value(self) -> str:
        return self.source

    @property
    def source_kind(self) -> Literal["virtual", "artifact"]:
        return "artifact" if self.artifact is not None else "virtual"

    @property
    def cardinality(self) -> str | None:
        return self.artifact.cardinality if self.artifact is not None else None

    @property
    def path(self) -> Path | None:
        return self.artifacts[0].path if len(self.artifacts) == 1 else None

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(item.path for item in self.artifacts)

    @property
    def artifact_paths(self) -> tuple[Path, ...]:
        return self.paths


@dataclass(frozen=True, slots=True)
class ExpectedOutput:
    """One declared output passed to a step port."""

    output: OutputPlan
    namespace: ArtifactNamespace
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.output, OutputPlan):
            raise TypeError("expected output requires an OutputPlan")
        if not isinstance(self.namespace, ArtifactNamespace):
            raise TypeError("expected output requires an ArtifactNamespace")
        if self.output.artifact_key != self.namespace.artifact_key:
            raise ValueError("expected output does not match its artifact namespace")
        object.__setattr__(self, "path", Path(self.path).absolute())

    @property
    def id(self) -> str:
        return self.output.id

    @property
    def output_id(self) -> str:
        return self.output.id

    @property
    def kind(self) -> str:
        return self.output.kind

    @property
    def artifact_key(self) -> ArtifactKey:
        return self.output.artifact_key

    @property
    def artifact_namespace(self) -> ArtifactNamespace:
        return self.namespace

    @property
    def relative_path(self) -> str:
        return self.namespace.relative_path


@dataclass(frozen=True, slots=True)
class StepExecutionRequest:
    """The single execution contract used by top-level and loop-body steps."""

    run_identity: str
    node_instance_id: str
    step: StepPlan
    artifact_scope: Path
    loop_context: LoopItemContext | None
    resolved_inputs: tuple[ResolvedInput, ...]
    expected_outputs: tuple[ExpectedOutput, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.run_identity, str) or not self.run_identity:
            raise ValueError("step request requires a run identity")
        if not isinstance(self.node_instance_id, str) or not self.node_instance_id:
            raise ValueError("step request requires a node instance id")
        if not isinstance(self.step, StepPlan):
            raise TypeError("step request requires a StepPlan")
        object.__setattr__(self, "artifact_scope", Path(self.artifact_scope).absolute())
        if self.loop_context is not None and not isinstance(self.loop_context, LoopItemContext):
            raise TypeError("step request loop context has an invalid type")
        object.__setattr__(self, "resolved_inputs", tuple(self.resolved_inputs))
        object.__setattr__(self, "expected_outputs", tuple(self.expected_outputs))
        if any(not isinstance(item, ResolvedInput) for item in self.resolved_inputs):
            raise TypeError("step request inputs must be ResolvedInput values")
        if any(not isinstance(item, ExpectedOutput) for item in self.expected_outputs):
            raise TypeError("step request outputs must be ExpectedOutput values")

    @property
    def inputs(self) -> tuple[ResolvedInput, ...]:
        return self.resolved_inputs

    @property
    def outputs(self) -> tuple[ExpectedOutput, ...]:
        return self.expected_outputs

    @property
    def artifact_namespace(self) -> str:
        """Return the scope-relative namespace for compatibility observers."""

        if self.loop_context is None:
            return ""
        return self.artifact_scope.as_posix()


_FAILED_EVENT_STATUSES = frozenset({"failed", "failure", "error", "paused", "stopped"})


@dataclass(frozen=True, slots=True)
class StepCompletionEvent:
    """Execution outcome returned by a step port.

    ``result`` is opaque to the structural executor.  In particular, values
    such as verdicts or retry hints are never inspected here.
    """

    node_instance_id: str = ""
    success: bool | None = None
    status: str = "completed"
    result: object = None
    error: object = None
    output_manifest: ArtifactManifest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.node_instance_id, str):
            raise TypeError("completion event node instance id must be a string")
        if not isinstance(self.status, str) or not self.status:
            raise ValueError("completion event status must be a non-empty string")
        if self.success is None:
            object.__setattr__(
                self,
                "success",
                self.status.casefold() not in _FAILED_EVENT_STATUSES,
            )
        elif not isinstance(self.success, bool):
            raise TypeError("completion event success must be boolean or None")
        if self.success and self.status.casefold() in _FAILED_EVENT_STATUSES:
            raise ValueError(
                "completion event cannot report success with a failure status"
            )
        if self.output_manifest is not None and not isinstance(
            self.output_manifest,
            ArtifactManifest,
        ):
            raise TypeError("completion event output manifest has an invalid type")

    @property
    def succeeded(self) -> bool:
        return bool(self.success)

    @property
    def completed(self) -> bool:
        return self.succeeded

    @property
    def failed(self) -> bool:
        return not self.succeeded

    @property
    def error_message(self) -> str | None:
        return None if self.error is None else str(self.error)


StepExecutionEvent = StepCompletionEvent


class StepExecutionPort(Protocol):
    """Port implemented by a runner/lifecycle adapter."""

    def execute(self, request: StepExecutionRequest) -> object:
        """Execute one request and return an opaque completion value."""


class LoopController(Protocol):
    """Trusted controller selecting declared body step IDs for one item."""

    def initial_steps(
        self,
        loop: LoopPlan,
        item: LoopItemContext,
    ) -> Sequence[str]:
        """Return local IDs from ``loop.body`` in the desired order."""

    def next_steps(
        self,
        loop: LoopPlan,
        item: LoopItemContext,
        completed_step: StepPlan,
        event: StepCompletionEvent,
    ) -> Sequence[str]:
        """Optionally return the next declared body step IDs.

        A controller that implements this method is a trusted, stateful
        compatibility adapter.  The structural executor only transports the
        completion event to it and validates the returned IDs; it does not
        inspect verdicts or choose a route itself.
        """


@dataclass(frozen=True, slots=True)
class FixedSequenceController:
    """The v1 controller: execute every body step in declaration order."""

    def initial_steps(self, loop: LoopPlan, item: LoopItemContext) -> tuple[str, ...]:
        if not isinstance(loop, LoopPlan):
            raise TypeError("fixed sequence controller requires a LoopPlan")
        if not isinstance(item, LoopItemContext):
            raise TypeError("fixed sequence controller requires a loop item context")
        return tuple(step.local_id for step in loop.body)


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    """Bounded result of one structural pipeline traversal."""

    status: Literal["completed", "failed", "paused"]
    events: tuple[StepCompletionEvent, ...]
    requests: tuple[StepExecutionRequest, ...]
    state: RunState
    error: BaseException | None = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed", "paused"}:
            raise ValueError(f"unsupported pipeline result status: {self.status!r}")
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "requests", tuple(self.requests))
        if any(not isinstance(item, StepCompletionEvent) for item in self.events):
            raise TypeError("pipeline result events must be StepCompletionEvent values")
        if any(not isinstance(item, StepExecutionRequest) for item in self.requests):
            raise TypeError("pipeline result requests must be StepExecutionRequest values")
        if not isinstance(self.state, RunState):
            raise TypeError("pipeline result requires a RunState")

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"

    @property
    def success(self) -> bool:
        return self.succeeded

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def paused(self) -> bool:
        return self.status == "paused"

    @property
    def completed_instances(self) -> tuple[str, ...]:
        return self.state.completed_instances

    @property
    def output_manifest(self) -> ArtifactManifest | None:
        return self.state.output_manifest

    @property
    def execution_events(self) -> tuple[StepCompletionEvent, ...]:
        return self.events

    @property
    def executed_requests(self) -> tuple[StepExecutionRequest, ...]:
        return self.requests


RunResult = PipelineRunResult
PipelineResult = PipelineRunResult


class PipelineExecutionError(RuntimeError):
    """Raised only when a caller explicitly requests exception-style failure."""

    def __init__(
        self,
        message: str,
        *,
        result: PipelineRunResult | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.result = result
        self.cause = cause
        super().__init__(message)


ExecutionError = PipelineExecutionError


class MissingVirtualInputError(PipelineExecutionError):
    """A required virtual input was not supplied to the executor."""


class MissingArtifactInputError(PipelineExecutionError):
    """A required producer output is not available or is stale."""


class UnsupportedLoopControllerError(PipelineExecutionError):
    """A loop controller has no trusted runtime implementation."""


@dataclass(frozen=True, slots=True)
class _ProducedArtifact:
    namespace: ArtifactNamespace
    entry: ArtifactManifestEntry

    @property
    def key(self) -> ArtifactKey:
        return self.namespace.artifact_key


class _ExecutionContext:
    """Mutable, execution-local indexes kept out of immutable public plans."""

    def __init__(
        self,
        plan: ValidatedWorkflowPlan,
        state: RunState,
        virtual_inputs: Mapping[str, object],
        *,
        validate_outputs: bool,
    ) -> None:
        self.plan = plan
        self.state = state
        self.virtual_inputs = MappingProxyType(dict(virtual_inputs))
        self.validate_outputs = validate_outputs
        self.scalar: dict[tuple[ArtifactKey, str | None], _ProducedArtifact] = {}
        self.collections: dict[ArtifactKey, tuple[_ProducedArtifact, ...]] = {}
        self._namespaces_by_identity = {
            (item.node_instance_id, item.output_id, item.item_id): item
            for item in plan.namespace_plan.entries
        }
        self._hydrate_state()

    def _hydrate_state(self) -> None:
        for entry in self.state.output_entries:
            namespace = self._namespaces_by_identity.get(entry.identity)
            if namespace is None:
                raise RunStateCorruptionError(
                    "persisted workflow state contains an undeclared output"
                )
            if namespace.is_export:
                raise RunStateCorruptionError(
                    "persisted collection export cannot be used as a step output"
                )
            if entry.artifact_key != namespace.artifact_key:
                raise RunStateCorruptionError(
                    "persisted workflow state contains an output with the wrong logical identity"
                )
            if (
                entry.relative_path != namespace.relative_path
                or entry.item_position != namespace.position
                or entry.kind != namespace.kind
            ):
                raise RunStateCorruptionError(
                    "persisted workflow state output does not match its declared namespace"
                )
            if not self.state.is_completed(entry.node_instance_id):
                raise RunStateCorruptionError(
                    "persisted workflow state contains output for an incomplete instance"
                )
            produced = _ProducedArtifact(namespace=namespace, entry=entry)
            if namespace.artifact_key.cardinality == "collection":
                self.collections[namespace.artifact_key] = (produced,)
            else:
                self.scalar[(namespace.artifact_key, namespace.item_id)] = produced

    def register_manifest(self, manifest: ArtifactManifest) -> None:
        for entry in manifest.entries:
            namespace = self._namespaces_by_identity.get(entry.identity)
            if namespace is None:
                raise ArtifactOutputValidationError(
                    "step produced an undeclared artifact output"
                )
            if entry.artifact_key != namespace.artifact_key:
                raise ArtifactOutputValidationError(
                    "step produced an artifact with the wrong logical identity"
                )
            produced = _ProducedArtifact(namespace=namespace, entry=entry)
            if namespace.artifact_key.cardinality == "collection":
                self.collections[namespace.artifact_key] = (produced,)
            else:
                self.scalar[(namespace.artifact_key, namespace.item_id)] = produced

    def verify_produced(self, produced: _ProducedArtifact) -> None:
        """Recheck content and producer identity immediately before consuming it."""

        if not self.validate_outputs:
            # Dry-run requests carry planned output paths but do not create
            # files.  Keep those planned outputs usable by later inputs while
            # retaining the real manifest/freshness checks for execution.
            return

        validate_artifact_manifest(
            self.plan.artifact_root,
            ArtifactManifest(
                run_identity=self.plan.identity.digest,
                entries=(produced.entry,),
            ),
            expected_run_identity=self.plan.identity.digest,
            expected_namespaces=(produced.namespace,),
        )

    def record_completion(self, instance_id: str, manifest: ArtifactManifest) -> None:
        self.register_manifest(manifest)
        self.state = self.state.with_completed_instance(instance_id, manifest)

    def complete_container(self, instance_id: str) -> None:
        self.state = self.state.with_completed_instance(instance_id)


class PipelineExecutor:
    """Execute a validated workflow through one common step port.

    The class is intentionally structural.  It does not know how a runner is
    invoked, what a lifecycle result means, or how a verdict should route the
    workflow.  Those concerns are supplied by ``port`` and (for future
    controller capabilities) trusted controller adapters.
    """

    def __init__(
        self,
        port: StepExecutionPort | Callable[[StepExecutionRequest], object] | None = None,
        *,
        step_port: StepExecutionPort | Callable[[StepExecutionRequest], object] | None = None,
        execution_port: StepExecutionPort | Callable[[StepExecutionRequest], object] | None = None,
        virtual_inputs: Mapping[str, object] | None = None,
        input_context: Mapping[str, object] | None = None,
        issue: object = None,
        repo_instructions: object = None,
        controllers: Mapping[str, LoopController] | None = None,
        loop_controllers: Mapping[str, LoopController] | None = None,
        validate_outputs: bool = True,
        state_store: RunStateStore | None = None,
        persist_state: bool | None = None,
        raise_on_error: bool = False,
    ) -> None:
        provided_ports = [candidate for candidate in (port, step_port, execution_port) if candidate is not None]
        if len(provided_ports) > 1:
            raise ValueError("provide only one of port, step_port, and execution_port")
        self.port = provided_ports[0] if provided_ports else None
        if self.port is None:
            raise TypeError("PipelineExecutor requires a step execution port")
        if virtual_inputs is not None and input_context is not None:
            raise ValueError("provide only one of virtual_inputs and input_context")
        configured_inputs = dict(
            virtual_inputs if virtual_inputs is not None else (input_context or {})
        )
        for key in configured_inputs:
            if not isinstance(key, str) or not key.startswith("$"):
                raise ValueError("virtual input keys must be capability tokens")
        if issue is not None:
            configured_inputs["$issue"] = issue
        if repo_instructions is not None:
            configured_inputs["$repo_instructions"] = repo_instructions
        self.virtual_inputs = MappingProxyType(configured_inputs)
        if controllers is not None and loop_controllers is not None:
            raise ValueError("provide only one of controllers and loop_controllers")
        configured_controllers = dict(
            controllers if controllers is not None else (loop_controllers or {})
        )
        if any(not isinstance(key, str) or not key for key in configured_controllers):
            raise ValueError("loop controller keys must be non-empty strings")
        self.controllers = MappingProxyType(configured_controllers)
        if not isinstance(validate_outputs, bool):
            raise TypeError("validate_outputs must be a boolean")
        self.validate_outputs = validate_outputs
        if state_store is not None and not isinstance(state_store, RunStateStore):
            raise TypeError("state_store must be a RunStateStore")
        self.state_store = state_store
        self.persist_state = state_store is not None if persist_state is None else persist_state
        if not isinstance(self.persist_state, bool):
            raise TypeError("persist_state must be a boolean")
        if self.persist_state and self.state_store is None:
            # The concrete store is selected from the validated plan at
            # execute time, because a constructor cannot know its artifact root.
            self.state_store = None
        if not isinstance(raise_on_error, bool):
            raise TypeError("raise_on_error must be a boolean")
        self.raise_on_error = raise_on_error

    def execute(
        self,
        plan: ValidatedWorkflowPlan,
        state: RunState | Mapping[str, object] | None = None,
        *,
        virtual_inputs: Mapping[str, object] | None = None,
        input_context: Mapping[str, object] | None = None,
        state_store: RunStateStore | None = None,
        persist_state: bool | None = None,
        raise_on_error: bool | None = None,
    ) -> PipelineRunResult:
        """Traverse ``plan`` in declaration order and stop at the first failure."""

        if not isinstance(plan, ValidatedWorkflowPlan):
            raise TypeError("PipelineExecutor requires a ValidatedWorkflowPlan")
        if virtual_inputs is not None and input_context is not None:
            raise ValueError("provide only one of virtual_inputs and input_context")
        supplied_inputs = virtual_inputs if virtual_inputs is not None else input_context
        runtime_inputs = dict(self.virtual_inputs)
        if supplied_inputs is not None:
            for key, value in supplied_inputs.items():
                if not isinstance(key, str) or not key.startswith("$"):
                    raise ValueError("virtual input keys must be capability tokens")
                runtime_inputs[key] = value

        # This is the final read-only namespace check before a scope lock can
        # create anything.  ``prepare_workflow_run`` has already performed the
        # full config/capability/source preflight.
        plan.namespace_plan.validate_runtime_paths(plan.artifact_root)
        current_state = (
            plan.initial_state()
            if state is None
            else plan.validate_resume(state)
        )
        selected_store = state_store if state_store is not None else self.state_store
        if selected_store is not None and not isinstance(selected_store, RunStateStore):
            raise TypeError("state_store must be a RunStateStore")
        should_persist = self.persist_state if persist_state is None else persist_state
        if not isinstance(should_persist, bool):
            raise TypeError("persist_state must be a boolean")
        if selected_store is not None:
            should_persist = True
            if selected_store.artifact_root != plan.artifact_root:
                raise ValueError("state store artifact root does not match validated plan")
        if should_persist and selected_store is None:
            selected_store = plan.state_store()

        context = _ExecutionContext(
            plan,
            current_state,
            runtime_inputs,
            validate_outputs=self.validate_outputs,
        )
        events: list[StepCompletionEvent] = []
        requests: list[StepExecutionRequest] = []
        use_raise = self.raise_on_error if raise_on_error is None else raise_on_error
        if not isinstance(use_raise, bool):
            raise TypeError("raise_on_error must be a boolean")

        try:
            if should_persist and selected_store is not None and state is None:
                selected_store.save(context.state, expected_identity=plan.identity)

            for node in plan.plan.nodes:
                if isinstance(node, StepPlan):
                    failure = self._execute_step(
                        context,
                        node,
                        loop_context=None,
                        events=events,
                        requests=requests,
                        state_store=selected_store if should_persist else None,
                    )
                elif isinstance(node, LoopPlan):
                    failure = self._execute_loop(
                        context,
                        node,
                        events=events,
                        requests=requests,
                        state_store=selected_store if should_persist else None,
                    )
                else:  # pragma: no cover - validated plans cannot contain this
                    failure = ("failed", ValueError("unsupported workflow plan node"), None)
                if failure is not None:
                    status, error, event = failure
                    if event is not None:
                        events.append(event)
                    return self._finish(
                        context,
                        events,
                        requests,
                        status=status,
                        error=error,
                        state_store=selected_store if should_persist else None,
                        raise_on_error=use_raise,
                    )

            context.state = context.state.with_status("completed")
            if should_persist and selected_store is not None:
                selected_store.save(context.state, expected_identity=plan.identity)
            return PipelineRunResult(
                status="completed",
                events=tuple(events),
                requests=tuple(requests),
                state=context.state,
            )
        except PipelineExecutionError:
            raise
        except Exception as exc:
            return self._finish(
                context,
                events,
                requests,
                status="failed",
                error=exc,
                state_store=selected_store if should_persist else None,
                raise_on_error=use_raise,
            )

    run = execute

    def _execute_loop(
        self,
        context: _ExecutionContext,
        loop: LoopPlan,
        *,
        events: list[StepCompletionEvent],
        requests: list[StepExecutionRequest],
        state_store: RunStateStore | None,
    ) -> tuple[str, BaseException, StepCompletionEvent | None] | None:
        if context.state.is_completed(loop.canonical_id):
            try:
                self._resolve_loop_exports(context, loop)
                return None
            except Exception as exc:
                return self._failure(loop.canonical_id, exc)
        dependency_error = self._check_dependencies(context, loop.dependencies, None)
        if dependency_error is not None:
            return self._failure(loop.canonical_id, dependency_error)
        snapshot = context.plan.source_snapshots.get(loop.local_id)
        if snapshot is None:
            return self._failure(
                loop.canonical_id,
                MissingArtifactInputError(
                    f"loop source snapshot is unavailable: {loop.local_id}"
                ),
            )
        try:
            controller = self._controller_for(loop)
            for source_item in snapshot.items:
                item_context = LoopItemContext.from_item(loop.local_id, source_item)
                selected_ids = tuple(controller.initial_steps(loop, item_context))
                next_steps = getattr(controller, "next_steps", None)
                if not callable(next_steps):
                    selected_steps = self._selected_body_steps(loop, selected_ids)
                    for step in selected_steps:
                        instance_id = f"{step.canonical_id}@{source_item.item_id}"
                        if context.state.is_completed(instance_id):
                            continue
                        failure = self._execute_step(
                            context,
                            step,
                            loop_context=item_context,
                            events=events,
                            requests=requests,
                            state_store=state_store,
                        )
                        if failure is not None:
                            return failure
                    continue

                # Dynamic compatibility controllers own the transition
                # decision.  Keep a structural guard here so a controller
                # cannot select a body step twice or escape its declared
                # body, even though the controller is registry-authorized.
                scheduled_ids: set[str] = set()
                pending_ids = list(selected_ids)
                while pending_ids:
                    pending_id = pending_ids.pop(0)
                    selected_steps = self._selected_body_steps(loop, (pending_id,))
                    step = selected_steps[0]
                    if step.local_id in scheduled_ids:
                        raise UnsupportedLoopControllerError(
                            "loop controller selected a body step more than once: "
                            f"{step.local_id!r}"
                        )
                    scheduled_ids.add(step.local_id)
                    instance_id = f"{step.canonical_id}@{source_item.item_id}"
                    if context.state.is_completed(instance_id):
                        raise UnsupportedLoopControllerError(
                            "dynamic loop controller cannot resume a completed step "
                            f"without its completion result: {instance_id}"
                        )
                    event_count = len(events)
                    failure = self._execute_step(
                        context,
                        step,
                        loop_context=item_context,
                        events=events,
                        requests=requests,
                        state_store=state_store,
                    )
                    if failure is not None:
                        return failure
                    if len(events) == event_count:
                        raise UnsupportedLoopControllerError(
                            "loop controller step completed without an execution event: "
                            f"{instance_id}"
                        )
                    returned_ids = tuple(next_steps(loop, item_context, step, events[-1]))
                    # Validate all IDs before adding any of them to the
                    # pending queue.  This keeps a malformed controller
                    # response from partially changing execution order.
                    returned_steps = self._selected_body_steps(loop, returned_ids)
                    returned_local_ids = [candidate.local_id for candidate in returned_steps]
                    if len(returned_local_ids) != len(set(returned_local_ids)):
                        raise UnsupportedLoopControllerError(
                            "loop controller selected a body step more than once"
                        )
                    if any(candidate in scheduled_ids for candidate in returned_local_ids):
                        repeated = next(
                            candidate
                            for candidate in returned_local_ids
                            if candidate in scheduled_ids
                        )
                        raise UnsupportedLoopControllerError(
                            "loop controller selected a body step more than once: "
                            f"{repeated!r}"
                        )
                    pending_ids.extend(returned_local_ids)
            self._resolve_loop_exports(context, loop)
            context.complete_container(loop.canonical_id)
            if state_store is not None:
                context.state = state_store.record_completed(
                    context.state,
                    loop.canonical_id,
                    expected_identity=context.plan.identity,
                )
            return None
        except Exception as exc:
            return self._failure(loop.canonical_id, exc)

    def _execute_step(
        self,
        context: _ExecutionContext,
        step: StepPlan,
        *,
        loop_context: LoopItemContext | None,
        events: list[StepCompletionEvent],
        requests: list[StepExecutionRequest],
        state_store: RunStateStore | None,
    ) -> tuple[str, BaseException, StepCompletionEvent | None] | None:
        instance_id = step.canonical_id
        if loop_context is not None:
            instance_id = f"{instance_id}@{loop_context.item_id}"
        if context.state.is_completed(instance_id):
            return None
        dependency_error = self._check_dependencies(
            context,
            step.dependencies,
            loop_context,
        )
        if dependency_error is not None:
            return self._failure(instance_id, dependency_error)

        try:
            namespaces = context.plan.namespace_plan.for_instance(instance_id)
            expected_outputs = self._expected_outputs(step, namespaces, context.plan.artifact_root)
            scope_relative = self._scope_relative_path(
                context.plan,
                step,
                loop_context,
                namespaces,
            )
            artifact_store = ArtifactManifestStore(context.plan.artifact_root)
            # Passing an empty string deliberately selects the artifact root;
            # all non-empty paths are validated by ArtifactScopeLock immediately
            # before it creates the scope and lock file.
            lock_scope: str = scope_relative or ""
            with artifact_store.locked_scope(lock_scope, owner=f"{context.plan.identity.digest}:{instance_id}"):
                output_validator = (
                    ArtifactOutputValidator(
                        context.plan.artifact_root,
                        namespaces,
                        run_identity=context.plan.identity.digest,
                    )
                    if self.validate_outputs and namespaces
                    else None
                )
                resolved_inputs = self._resolve_inputs(context, step, loop_context)
                request = StepExecutionRequest(
                    run_identity=context.plan.identity.digest,
                    node_instance_id=instance_id,
                    step=step,
                    artifact_scope=ArtifactPathGuard(context.plan.artifact_root).validate(
                        scope_relative or context.plan.artifact_root
                    ),
                    loop_context=loop_context,
                    resolved_inputs=resolved_inputs,
                    expected_outputs=expected_outputs,
                )
                requests.append(request)
                raw_event = self._invoke_port(request)
                event = self._coerce_event(raw_event, instance_id)
                if not event.succeeded:
                    return self._failure(instance_id, self._event_error(event), event)
                if output_validator is None and not self.validate_outputs:
                    manifest = self._planned_manifest(
                        context.plan.identity.digest,
                        expected_outputs,
                    )
                elif output_validator is None:
                    manifest = ArtifactManifest(
                        run_identity=context.plan.identity.digest,
                        entries=(),
                    )
                else:
                    manifest = output_validator.validate_all()
                context.record_completion(instance_id, manifest)
                if state_store is not None:
                    context.state = state_store.record_completed(
                        context.state,
                        instance_id,
                        manifest,
                        expected_identity=context.plan.identity,
                    )
                events.append(event)
                return None
        except Exception as exc:
            return self._failure(instance_id, exc)

    @staticmethod
    def _planned_manifest(
        run_identity: str,
        expected_outputs: Sequence[ExpectedOutput],
    ) -> ArtifactManifest:
        """Describe dry-run outputs without pretending that files exist."""

        entries = tuple(
            ArtifactManifestEntry(
                run_identity=run_identity,
                node_instance_id=expected.namespace.node_instance_id,
                producer_node_id=expected.namespace.producer_node_id,
                output_id=expected.namespace.output_id,
                scope=expected.namespace.scope,
                cardinality=expected.namespace.cardinality,
                kind=expected.namespace.kind,
                relative_path=expected.namespace.relative_path,
                item_id=expected.namespace.item_id,
                item_position=expected.namespace.position,
                size_bytes=0,
                sha256="0" * 64,
                device=0,
                inode=0,
                mtime_ns=0,
                freshness="0" * 64,
            )
            for expected in expected_outputs
        )
        return ArtifactManifest(run_identity=run_identity, entries=entries)

    def _controller_for(self, loop: LoopPlan) -> LoopController:
        if loop.controller in {"fixed_sequence.v1", "fixed_sequence"}:
            return FixedSequenceController()
        controller = self.controllers.get(loop.controller)
        if controller is None:
            raise UnsupportedLoopControllerError(
                f"no trusted loop controller is registered: {loop.controller}"
            )
        if not callable(getattr(controller, "initial_steps", None)):
            raise UnsupportedLoopControllerError(
                f"loop controller does not implement initial_steps: {loop.controller}"
            )
        return controller

    @staticmethod
    def _selected_body_steps(loop: LoopPlan, selected_ids: Sequence[str]) -> tuple[StepPlan, ...]:
        if isinstance(selected_ids, (str, bytes, bytearray)):
            raise UnsupportedLoopControllerError("loop controller returned a string, not step IDs")
        body_by_id = {step.local_id: step for step in loop.body}
        selected: list[StepPlan] = []
        seen: set[str] = set()
        for step_id in selected_ids:
            if not isinstance(step_id, str) or step_id not in body_by_id:
                raise UnsupportedLoopControllerError(
                    f"loop controller selected an undeclared body step: {step_id!r}"
                )
            if step_id in seen:
                raise UnsupportedLoopControllerError(
                    f"loop controller selected a body step more than once: {step_id!r}"
                )
            seen.add(step_id)
            selected.append(body_by_id[step_id])
        return tuple(selected)

    @staticmethod
    def _check_dependencies(
        context: _ExecutionContext,
        dependencies: Iterable[str],
        loop_context: LoopItemContext | None,
    ) -> BaseException | None:
        for dependency in dependencies:
            dependency_instance = dependency
            if loop_context is not None and dependency.startswith(
                f"nodes/{loop_context.loop_id}/body/"
            ):
                dependency_instance = f"{dependency}@{loop_context.item_id}"
            if not context.state.is_completed(dependency_instance):
                return MissingArtifactInputError(
                    f"required dependency has not completed: {dependency_instance}"
                )
        return None

    @staticmethod
    def _expected_outputs(
        step: StepPlan,
        namespaces: Iterable[ArtifactNamespace],
        artifact_root: Path,
    ) -> tuple[ExpectedOutput, ...]:
        namespace_by_output = {namespace.output_id: namespace for namespace in namespaces}
        expected: list[ExpectedOutput] = []
        for output in step.outputs:
            namespace = namespace_by_output.get(output.id)
            if namespace is None:
                raise ArtifactOutputValidationError(
                    f"no physical namespace is declared for output {output.id!r}"
                )
            expected.append(
                ExpectedOutput(
                    output=output,
                    namespace=namespace,
                    path=namespace.absolute_path(artifact_root),
                )
            )
        return tuple(expected)

    @staticmethod
    def _scope_relative_path(
        plan: ValidatedWorkflowPlan,
        step: StepPlan,
        loop_context: LoopItemContext | None,
        namespaces: tuple[ArtifactNamespace, ...],
    ) -> str:
        if loop_context is None:
            return ""
        if namespaces:
            scope_paths = {namespace.scope_relative_path for namespace in namespaces}
            if len(scope_paths) != 1:
                raise ArtifactPathSafetyError(
                    f"step outputs do not share one artifact scope: {step.canonical_id}"
                )
            return next(iter(scope_paths))
        # A body step is allowed to have no outputs.  Infer the selected
        # namespace convention from a sibling output when one exists.  The
        # fallback is the default v1 namespace and remains lexical/symlink
        # checked by ArtifactScopeLock.
        body_prefix = f"{loop_context.loop_id}/body/"
        for candidate in plan.namespace_plan.entries:
            if candidate.item_id != loop_context.item_id:
                continue
            if not candidate.producer_node_id.startswith(f"nodes/{body_prefix}"):
                continue
            scope_root = candidate.scope_relative_path.rsplit("/steps/", 1)[0]
            return f"{scope_root}/steps/{step.local_id}"
        return (
            f"loops/{loop_context.loop_id}/items/{loop_context.item_id}"
            f"/steps/{step.local_id}"
        )

    def _resolve_inputs(
        self,
        context: _ExecutionContext,
        step: StepPlan,
        loop_context: LoopItemContext | None,
    ) -> tuple[ResolvedInput, ...]:
        resolved: list[ResolvedInput] = []
        for binding in step.inputs:
            if binding.virtual_input is not None:
                value = self._resolve_virtual_input(
                    context,
                    binding.virtual_input,
                    loop_context,
                )
                resolved.append(
                    ResolvedInput(
                        name=binding.name,
                        source=binding.source,
                        value=_freeze_runtime_value(value),
                    )
                )
                continue
            if binding.artifact is None:
                raise MissingArtifactInputError(
                    f"input has no resolved source: {binding.name}"
                )
            records = self._resolve_artifact_records(
                context,
                binding,
                loop_context,
            )
            value: object = (
                records[0].namespace.absolute_path(context.plan.artifact_root)
                if binding.artifact.cardinality == "scalar"
                else tuple(
                    record.namespace.absolute_path(context.plan.artifact_root)
                    for record in records
                )
            )
            resolved.append(
                ResolvedInput(
                    name=binding.name,
                    source=binding.source,
                    value=value,
                    artifact=binding.artifact,
                    artifacts=tuple(
                        ResolvedArtifact(
                            key=record.key,
                            namespace=record.namespace,
                            path=record.namespace.absolute_path(context.plan.artifact_root),
                            manifest_entry=record.entry,
                        )
                        for record in records
                    ),
                )
            )
        return tuple(resolved)

    @staticmethod
    def _resolve_virtual_input(
        context: _ExecutionContext,
        token: str,
        loop_context: LoopItemContext | None,
    ) -> object:
        if token == "$loop_item":
            if loop_context is None:
                raise MissingVirtualInputError(
                    "$loop_item is unavailable outside a loop body"
                )
            return loop_context.value
        if token not in context.virtual_inputs:
            raise MissingVirtualInputError(f"required virtual input is missing: {token}")
        return context.virtual_inputs[token]

    @staticmethod
    def _resolve_artifact_records(
        context: _ExecutionContext,
        binding: InputBindingPlan,
        loop_context: LoopItemContext | None,
    ) -> tuple[_ProducedArtifact, ...]:
        assert binding.artifact is not None
        key = binding.artifact.key
        if key.cardinality == "collection":
            records = context.collections.get(key)
            if records is None:
                raise MissingArtifactInputError(
                    f"required collection output is unavailable: {key.reference}"
                )
        else:
            item_id = (
                loop_context.item_id
                if key.scope == "loop_item" and loop_context is not None
                else None
            )
            if key.scope == "loop_item" and item_id is None:
                raise MissingArtifactInputError(
                    f"item-scoped artifact is unavailable outside its loop: {key.reference}"
                )
            record = context.scalar.get((key, item_id))
            if record is None:
                raise MissingArtifactInputError(
                    f"required artifact output is unavailable: {key.reference}"
                )
            records = (record,)
        for record in records:
            context.verify_produced(record)
        return records

    @staticmethod
    def _resolve_loop_exports(context: _ExecutionContext, loop: LoopPlan) -> None:
        snapshot = context.plan.source_snapshots.get(loop.local_id)
        if snapshot is None:
            raise MissingArtifactInputError(
                f"loop source snapshot is unavailable: {loop.local_id}"
            )
        for export in loop.exports:
            records: list[_ProducedArtifact] = []
            for item in snapshot.items:
                record = context.scalar.get((export.source_artifact, item.item_id))
                if record is None:
                    raise MissingArtifactInputError(
                        f"collection export source is unavailable: {export.source_artifact.reference}"
                    )
                context.verify_produced(record)
                records.append(record)
            context.collections[export.artifact_key] = tuple(records)

    def _invoke_port(self, request: StepExecutionRequest) -> object:  # type: ignore[override]
        method = getattr(self.port, "execute", None)
        if callable(method):
            return method(request)
        if callable(self.port):
            return self.port(request)  # type: ignore[misc]
        raise TypeError("step execution port must be callable or expose execute")

    @staticmethod
    def _coerce_event(raw: object, instance_id: str) -> StepCompletionEvent:
        if isinstance(raw, StepCompletionEvent):
            event = raw
        elif raw is None:
            event = StepCompletionEvent(node_instance_id=instance_id)
        elif isinstance(raw, bool):
            event = StepCompletionEvent(
                node_instance_id=instance_id,
                success=raw,
                status="completed" if raw else "failed",
            )
        elif isinstance(raw, Mapping):
            status_value = raw.get("status", "completed")
            if not isinstance(status_value, str):
                raise TypeError("step port completion status must be a string")
            status = status_value
            success_value = raw.get("success", None)
            if success_value is not None and not isinstance(success_value, bool):
                raise TypeError("step port completion success must be boolean or null")
            success = success_value
            instance_value = raw.get("node_instance_id", instance_id)
            if not isinstance(instance_value, str):
                raise TypeError("step port completion node instance id must be a string")
            manifest_value = raw.get("output_manifest")
            if manifest_value is not None and not isinstance(
                manifest_value,
                ArtifactManifest,
            ):
                raise TypeError("step port completion output manifest has an invalid type")
            event = StepCompletionEvent(
                node_instance_id=instance_value,
                success=success,
                status=status,
                result=raw.get("result"),
                error=raw.get("error"),
                output_manifest=manifest_value,
            )
        else:
            status_value = getattr(raw, "status", "completed")
            if not isinstance(status_value, str):
                raise TypeError("step port completion status must be a string")
            status = status_value
            success_value = getattr(raw, "success", None)
            if success_value is not None and not isinstance(success_value, bool):
                raise TypeError("step port completion success must be boolean or null")
            instance_value = getattr(raw, "node_instance_id", instance_id)
            if not isinstance(instance_value, str):
                raise TypeError("step port completion node instance id must be a string")
            manifest_value = getattr(raw, "output_manifest", None)
            if manifest_value is not None and not isinstance(
                manifest_value,
                ArtifactManifest,
            ):
                raise TypeError("step port completion output manifest has an invalid type")
            event = StepCompletionEvent(
                node_instance_id=instance_value,
                success=success_value,
                status=status,
                result=raw,
                error=getattr(raw, "error", None),
                output_manifest=manifest_value,
            )
        if event.node_instance_id not in {"", instance_id}:
            raise ValueError(
                f"step port returned completion for another instance: {event.node_instance_id}"
            )
        if event.node_instance_id == "":
            event = replace(event, node_instance_id=instance_id)
        return event

    @staticmethod
    def _event_error(event: StepCompletionEvent) -> BaseException:
        if isinstance(event.error, BaseException):
            return event.error
        if event.error is not None:
            return PipelineExecutionError(str(event.error))
        return PipelineExecutionError(
            f"step execution failed: {event.node_instance_id}"
        )

    @staticmethod
    def _failure(
        instance_id: str,
        error: BaseException,
        event: StepCompletionEvent | None = None,
    ) -> tuple[str, BaseException, StepCompletionEvent]:
        if event is None:
            event = StepCompletionEvent(
                node_instance_id=instance_id,
                success=False,
                status="failed",
                error=error,
            )
        status: Literal["failed", "paused"] = (
            "paused" if event.status.casefold() == "paused" else "failed"
        )
        return status, error, event

    @staticmethod
    def _finish(
        context: _ExecutionContext,
        events: Sequence[StepCompletionEvent],
        requests: Sequence[StepExecutionRequest],
        *,
        status: Literal["failed", "paused"],
        error: BaseException,
        state_store: RunStateStore | None,
        raise_on_error: bool,
    ) -> PipelineRunResult:
        context.state = context.state.with_status(
            status,
            pause_reason=str(error) if status == "paused" else None,
        )
        if state_store is not None:
            state_store.save(context.state, expected_identity=context.plan.identity)
        result = PipelineRunResult(
            status=status,
            events=tuple(events),
            requests=tuple(requests),
            state=context.state,
            error=error,
        )
        if raise_on_error:
            raise PipelineExecutionError(
                str(error),
                result=result,
                cause=error,
            ) from error
        return result


# The explicit aliases make the concrete implementation discoverable without
# removing the shorter name used by the solution-design contract.
ConfiguredPipelineExecutor = PipelineExecutor
PipelineExecutorImpl = PipelineExecutor
DefaultPipelineExecutor = PipelineExecutor
PipelineExecutionRequest = StepExecutionRequest
ExecutionRequest = StepExecutionRequest
ExpectedArtifact = ExpectedOutput
LoopContext = LoopItemContext


__all__ = [
    "ConfiguredPipelineExecutor",
    "DefaultPipelineExecutor",
    "ExecutionError",
    "ExecutionRequest",
    "ExpectedArtifact",
    "ExpectedOutput",
    "FixedSequenceController",
    "LoopContext",
    "LoopController",
    "LoopItemContext",
    "MissingArtifactInputError",
    "MissingVirtualInputError",
    "PipelineExecutionError",
    "PipelineExecutionRequest",
    "PipelineExecutor",
    "PipelineExecutorImpl",
    "PipelineResult",
    "PipelineRunResult",
    "PreparedWorkflow",
    "ResolvedArtifact",
    "ResolvedInput",
    "RunIdentity",
    "RunResult",
    "RunState",
    "RunStateCorruptionError",
    "RunStateStore",
    "ResumeStateError",
    "ResumeStateNotFoundError",
    "StaleResumeIdentityError",
    "StepCompletionEvent",
    "StepExecutionEvent",
    "StepExecutionPort",
    "StepExecutionRequest",
    "UnsupportedLoopControllerError",
    "ValidatedPlan",
    "ValidatedWorkflowPlan",
    "build_validated_workflow_plan",
    "build_run_identity",
    "compute_run_identity",
    "create_run_identity",
    "prepare_validated_plan",
    "prepare_workflow_run",
    "validate_resume_state",
    "validate_workflow_run",
    "WorkflowRunIdentity",
]
