#!/usr/bin/env python3
"""Bounded convergence policy and durable attempt orchestration.

The fixed evaluation loop in :mod:`scripts.evaluation_loop` deliberately
remains a one-shot operation.  This module owns the state required to decide
whether another one-shot operation may be started.  Policy reduction is kept
free of filesystem, clock, and executor side effects; the store and
orchestrator provide the durable boundary around it.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

try:
    from scripts.single_change import ActiveTarget
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from single_change import ActiveTarget  # type: ignore


SCHEMA_VERSION = "1.0"
POLICY_VERSION = "1.0"
PROGRESS_RULE_VERSION = "1.0"
FINDING_IDENTITY_VERSION = "1.0"

VERDICTS = frozenset(
    {
        "satisfied",
        "changes_requested",
        "execution_failed",
        "invalid_output",
        "plan_defect",
    }
)
RETRY_VERDICTS = frozenset({"changes_requested", "execution_failed", "invalid_output"})
TERMINAL_STATES = frozenset({"satisfied", "blocked", "waiting_for_human"})
ACTIONS = frozenset({"retry", "finish", "stop", "handoff"})
METRICS = frozenset({"wall_seconds", "tokens", "cost"})
AVAILABILITY = frozenset({"available", "partial", "unavailable", "invalid"})
BUDGET_POLICIES = frozenset({"continue", "stop", "handoff"})
STRATEGY_RELATIONS = frozenset({"new", "transient_retry"})
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
TARGET_KINDS = frozenset({"work_item", "acceptance_criterion", "review_finding"})
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REASON_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
MAX_CHANGE_INTENT_LENGTH = 4096
MAX_JSON_DEPTH = 32


class ConvergenceError(ValueError):
    """Base error for invalid or unsafe convergence state."""


class HistoryCorruptionError(ConvergenceError):
    """Raised when durable convergence history cannot be trusted."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_depth(value: object, depth: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        return depth
    if isinstance(value, Mapping):
        return max([depth, *(_json_depth(item, depth + 1) for item in value.values())])
    if isinstance(value, list):
        return max([depth, *(_json_depth(item, depth + 1) for item in value)])
    return depth


def _as_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConvergenceError(f"{field_name} must be an object")
    return value


def _as_non_empty_string(value: object, field_name: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConvergenceError(f"{field_name} must be a non-empty string")
    if len(value) > max_length:
        raise ConvergenceError(f"{field_name} is too long")
    if any(ord(character) < 0x20 and character not in "\t\n" for character in value):
        raise ConvergenceError(f"{field_name} contains control characters")
    return value


def _validate_id(value: object, field_name: str, *, pattern: re.Pattern[str] = ID_PATTERN) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ConvergenceError(f"{field_name} has an unsafe namespace")
    return value


def _validate_sha(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ConvergenceError(f"{field_name} must be a lowercase sha256 digest")
    return value


def _finite_non_negative(value: object, field_name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConvergenceError(f"{field_name} must be a finite non-negative number")
    if not math.isfinite(float(value)) or value < 0:
        raise ConvergenceError(f"{field_name} must be a finite non-negative number")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConvergenceError(f"{field_name} must be an integer >= 1")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConvergenceError(f"{field_name} must be an integer >= 0")
    return value


def _target_payload(target: object) -> dict[str, str]:
    if isinstance(target, ActiveTarget):
        payload = target.to_payload()
    elif isinstance(target, Mapping):
        allowed = {"kind", "id", "source_ref"}
        unknown = set(target) - allowed
        if unknown:
            raise ConvergenceError(f"target has unsupported fields: {sorted(unknown)}")
        payload = {
            "kind": target.get("kind"),
            "id": target.get("id"),
            "source_ref": target.get("source_ref"),
        }
    else:
        raise ConvergenceError("target must be an ActiveTarget or object")
    if payload["kind"] not in TARGET_KINDS:
        raise ConvergenceError("target.kind is unsupported")
    _validate_id(payload["id"], "target.id")
    _as_non_empty_string(payload["source_ref"], "target.source_ref")
    return {
        "kind": str(payload["kind"]),
        "id": str(payload["id"]),
        "source_ref": str(payload["source_ref"]),
    }


def target_sha256(target: object) -> str:
    return canonical_sha256(_target_payload(target))


def digest_intent(value: str) -> str:
    """Return the stable digest used by legacy proposal callers."""

    normalized = " ".join(value.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _metric_default_unit(metric: str) -> str:
    return {"wall_seconds": "seconds", "tokens": "tokens", "cost": "currency"}[metric]


@dataclass(frozen=True)
class BudgetLimit:
    metric: str
    limit: int | float
    unit: str | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.metric not in METRICS:
            raise ConvergenceError(f"unsupported budget metric: {self.metric}")
        _finite_non_negative(self.limit, f"budgets.{self.metric}.limit")
        unit = self.unit or _metric_default_unit(self.metric)
        if not isinstance(unit, str) or not unit.strip():
            raise ConvergenceError(f"budgets.{self.metric}.unit must be non-empty")
        if self.metric == "cost" and self.currency is not None:
            _as_non_empty_string(self.currency, f"budgets.{self.metric}.currency", max_length=16)
        object.__setattr__(self, "unit", unit)

    @classmethod
    def from_value(cls, metric: str, value: object) -> "BudgetLimit":
        if isinstance(value, BudgetLimit):
            if value.metric != metric:
                raise ConvergenceError("budget metric does not match mapping key")
            return value
        if isinstance(value, Mapping):
            allowed = {"limit", "value", "unit", "currency"}
            unknown = set(value) - allowed
            if unknown:
                raise ConvergenceError(f"budget has unsupported fields: {sorted(unknown)}")
            limit = value.get("limit", value.get("value"))
            return cls(metric, limit, value.get("unit"), value.get("currency"))
        return cls(metric, value)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "metric": self.metric,
            "limit": self.limit,
            "unit": self.unit,
        }
        if self.currency is not None:
            payload["currency"] = self.currency
        return payload


@dataclass(frozen=True)
class UsageSample:
    metric: str
    availability: str = "available"
    value: int | float | None = None
    unit: str | None = None
    source: str = "unknown"
    scope: str = "attempt"
    observed_at: str | None = None
    reason_code: str | None = None
    sample_id: str | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.metric not in METRICS:
            raise ConvergenceError(f"unsupported usage metric: {self.metric}")
        if self.availability not in AVAILABILITY:
            raise ConvergenceError(f"unsupported usage availability: {self.availability}")
        if self.scope not in {"attempt", "cumulative"}:
            raise ConvergenceError("usage scope must be attempt or cumulative")
        _as_non_empty_string(self.source, "usage source", max_length=256)
        if self.value is not None:
            _finite_non_negative(self.value, "usage value")
        if self.availability == "available" and self.value is None:
            raise ConvergenceError("available usage must contain a value")
        if self.availability in {"unavailable", "invalid"} and self.value is not None:
            raise ConvergenceError(f"{self.availability} usage must not contain a value")
        expected = _metric_default_unit(self.metric)
        unit = self.unit or expected
        if self.metric != "cost" and unit != expected:
            raise ConvergenceError(f"usage unit does not match {self.metric}: {unit}")
        if self.metric == "cost" and not unit:
            raise ConvergenceError("cost usage requires a currency unit")
        object.__setattr__(self, "unit", unit)
        if self.sample_id is not None:
            _as_non_empty_string(self.sample_id, "usage sample_id", max_length=256)

    @classmethod
    def from_value(cls, value: object, *, metric: str | None = None) -> "UsageSample":
        if isinstance(value, UsageSample):
            return value
        raw = _as_mapping(value, "usage sample")
        allowed = {
            "metric", "availability", "value", "unit", "source", "scope",
            "observed_at", "reason_code", "sample_id", "currency",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ConvergenceError(f"usage sample has unsupported fields: {sorted(unknown)}")
        return cls(
            metric=raw.get("metric", metric),
            availability=raw.get("availability", "available"),
            value=raw.get("value"),
            unit=raw.get("unit"),
            source=raw.get("source", "unknown"),
            scope=raw.get("scope", "attempt"),
            observed_at=raw.get("observed_at"),
            reason_code=raw.get("reason_code"),
            sample_id=raw.get("sample_id"),
            currency=raw.get("currency"),
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "metric": self.metric,
            "availability": self.availability,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "scope": self.scope,
            "observed_at": self.observed_at,
            "reason_code": self.reason_code,
        }
        if self.sample_id is not None:
            payload["sample_id"] = self.sample_id
        if self.currency is not None:
            payload["currency"] = self.currency
        return payload


def _normalize_mapping_of_ints(value: object, field_name: str) -> dict[str, int]:
    if isinstance(value, (tuple, list)):
        try:
            raw = dict(value)
        except (TypeError, ValueError) as exc:
            raise ConvergenceError(f"{field_name} must be a mapping") from exc
    else:
        raw = _as_mapping(value, field_name)
    result: dict[str, int] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or key not in VERDICTS:
            raise ConvergenceError(f"{field_name} contains unsupported verdict: {key}")
        result[key] = _non_negative_int(item, f"{field_name}.{key}")
    return result


@dataclass(frozen=True)
class ConvergencePolicy:
    """Versioned finite policy configuration.

    ``max_iterations`` intentionally has no repository-wide default.  A
    caller must choose it for every run so a loop cannot become unbounded by
    omission.
    """

    max_iterations: int | None = None
    max_finding_occurrences: int = 3
    max_consecutive_no_progress: int = 2
    retry_limits: Mapping[str, int] = field(
        default_factory=lambda: {
            "changes_requested": 1,
            "execution_failed": 1,
            "invalid_output": 1,
        }
    )
    transient_strategy_reuse_limit: int = 0
    retryable_reasons: Mapping[str, Sequence[str]] = field(
        default_factory=lambda: {
            "execution_failed": ("*",),
            "invalid_output": ("*",),
        }
    )
    transient_reuse_reasons: Sequence[str] = (
        "transient",
        "timeout",
        "runner_unavailable",
    )
    budgets: Mapping[str, BudgetLimit | Mapping[str, object] | int | float] = field(default_factory=dict)
    unknown_budget_policy: str = "handoff"
    partial_budget_policy: str = "handoff"
    progress_rule_version: str = PROGRESS_RULE_VERSION
    finding_identity_version: str = FINDING_IDENTITY_VERSION
    policy_id: str = "convergence-default"
    # Compatibility aliases used by the disposable prototype and early
    # callers.  They normalize into the canonical fields above.
    max_same_finding_occurrences: int | None = None
    max_no_progress: int | None = None
    unavailable_budget_policy: str | None = None
    wall_seconds_limit: int | float | None = None
    token_limit: int | float | None = None
    cost_limit: int | float | None = None

    def __post_init__(self) -> None:
        if self.max_same_finding_occurrences is not None:
            object.__setattr__(self, "max_finding_occurrences", self.max_same_finding_occurrences)
        if self.max_no_progress is not None:
            object.__setattr__(self, "max_consecutive_no_progress", self.max_no_progress)
        if self.unavailable_budget_policy is not None:
            object.__setattr__(self, "unknown_budget_policy", self.unavailable_budget_policy)
        object.__setattr__(self, "max_same_finding_occurrences", self.max_finding_occurrences)
        object.__setattr__(self, "max_no_progress", self.max_consecutive_no_progress)
        object.__setattr__(self, "unavailable_budget_policy", self.unknown_budget_policy)
        aliases = {
            "wall_seconds": self.wall_seconds_limit,
            "tokens": self.token_limit,
            "cost": self.cost_limit,
        }
        if any(value is not None for value in aliases.values()):
            budgets = dict(self.budgets)
            for metric, value in aliases.items():
                if value is not None:
                    budgets.setdefault(metric, BudgetLimit(metric, value))
            object.__setattr__(self, "budgets", budgets)
        if not isinstance(self.retry_limits, Mapping):
            object.__setattr__(self, "retry_limits", dict(self.retry_limits))
        if not isinstance(self.retryable_reasons, Mapping):
            object.__setattr__(self, "retryable_reasons", dict(self.retryable_reasons))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ConvergencePolicy":
        allowed = {
            "schema_version", "policy_version", "policy_id", "max_iterations", "max_finding_occurrences",
            "max_same_finding_occurrences", "max_consecutive_no_progress", "max_no_progress",
            "retry_limits", "transient_strategy_reuse_limit", "retryable_reasons",
            "transient_reuse_reasons", "budgets", "unknown_budget_policy",
            "partial_budget_policy", "unavailable_budget_policy", "progress_rule_version",
            "finding_identity_version",
            "wall_seconds_limit", "token_limit", "cost_limit",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ConvergenceError(f"policy has unsupported fields: {sorted(unknown)}")
        if raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise ConvergenceError("unsupported convergence policy schema version")
        if raw.get("policy_version", POLICY_VERSION) != POLICY_VERSION:
            raise ConvergenceError("unsupported convergence policy version")
        retryable = raw.get("retryable_reasons", {
            "execution_failed": ("*",),
            "invalid_output": ("*",),
        })
        budgets = raw.get("budgets", {})
        return cls(
            max_iterations=raw.get("max_iterations"),
            max_finding_occurrences=raw.get(
                "max_finding_occurrences", raw.get("max_same_finding_occurrences", 3)
            ),
            max_consecutive_no_progress=raw.get(
                "max_consecutive_no_progress", raw.get("max_no_progress", 2)
            ),
            retry_limits=raw.get("retry_limits", {
                "changes_requested": 1,
                "execution_failed": 1,
                "invalid_output": 1,
            }),
            transient_strategy_reuse_limit=raw.get("transient_strategy_reuse_limit", 0),
            retryable_reasons=retryable,
            transient_reuse_reasons=raw.get("transient_reuse_reasons", (
                "transient", "timeout", "runner_unavailable"
            )),
            budgets=budgets,
            unknown_budget_policy=raw.get(
                "unknown_budget_policy", raw.get("unavailable_budget_policy", "handoff")
            ),
            partial_budget_policy=raw.get("partial_budget_policy", "handoff"),
            progress_rule_version=raw.get("progress_rule_version", PROGRESS_RULE_VERSION),
            finding_identity_version=raw.get("finding_identity_version", FINDING_IDENTITY_VERSION),
            policy_id=raw.get("policy_id", "convergence-default"),
            wall_seconds_limit=raw.get("wall_seconds_limit"),
            token_limit=raw.get("token_limit"),
            cost_limit=raw.get("cost_limit"),
        )

    def validate(self) -> "ConvergencePolicy":
        _positive_int(self.max_iterations, "max_iterations")
        _positive_int(self.max_finding_occurrences, "max_finding_occurrences")
        _positive_int(self.max_consecutive_no_progress, "max_consecutive_no_progress")
        _non_negative_int(self.transient_strategy_reuse_limit, "transient_strategy_reuse_limit")
        _as_non_empty_string(self.policy_id, "policy_id", max_length=128)
        _as_non_empty_string(self.progress_rule_version, "progress_rule_version", max_length=32)
        _as_non_empty_string(self.finding_identity_version, "finding_identity_version", max_length=32)
        if self.unknown_budget_policy not in BUDGET_POLICIES:
            raise ConvergenceError("unknown_budget_policy must be continue, stop, or handoff")
        if self.partial_budget_policy not in BUDGET_POLICIES:
            raise ConvergenceError("partial_budget_policy must be continue, stop, or handoff")
        retry_limits = _normalize_mapping_of_ints(self.retry_limits, "retry_limits")
        for verdict in RETRY_VERDICTS:
            if verdict not in retry_limits:
                raise ConvergenceError(f"retry_limits must configure {verdict}")
        if not isinstance(self.transient_reuse_reasons, (tuple, list, set, frozenset)):
            raise ConvergenceError("transient_reuse_reasons must be a sequence of reason codes")
        for reason in self.transient_reuse_reasons:
            _as_non_empty_string(reason, "transient_reuse_reasons entry", max_length=256)
        raw_reasons = _as_mapping(self.retryable_reasons, "retryable_reasons")
        for verdict in ("execution_failed", "invalid_output"):
            values = raw_reasons.get(verdict, ())
            if not isinstance(values, (tuple, list, set, frozenset)):
                raise ConvergenceError(f"retryable_reasons.{verdict} must be a sequence")
            for reason in values:
                _as_non_empty_string(reason, f"retryable_reasons.{verdict} entry", max_length=256)
        for metric, raw_limit in _as_mapping(self.budgets, "budgets").items():
            if metric not in METRICS:
                raise ConvergenceError(f"unsupported budget metric: {metric}")
            BudgetLimit.from_value(metric, raw_limit)
        return self

    def budget_limits(self) -> dict[str, BudgetLimit]:
        self.validate()
        return {metric: BudgetLimit.from_value(metric, value) for metric, value in self.budgets.items()}

    def to_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "policy_id": self.policy_id,
            "max_iterations": self.max_iterations,
            "max_finding_occurrences": self.max_finding_occurrences,
            "max_consecutive_no_progress": self.max_consecutive_no_progress,
            "retry_limits": dict(sorted(self.retry_limits.items())),
            "transient_strategy_reuse_limit": self.transient_strategy_reuse_limit,
            "retryable_reasons": {
                key: sorted(str(item) for item in values)
                for key, values in sorted(self.retryable_reasons.items())
            },
            "transient_reuse_reasons": sorted(str(item) for item in self.transient_reuse_reasons),
            "budgets": {
                metric: self.budget_limits()[metric].to_payload()
                for metric in sorted(self.budget_limits())
            },
            "unknown_budget_policy": self.unknown_budget_policy,
            "partial_budget_policy": self.partial_budget_policy,
            "progress_rule_version": self.progress_rule_version,
            "finding_identity_version": self.finding_identity_version,
        }

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self.to_payload())


Policy = ConvergencePolicy


@dataclass(frozen=True)
class RetryInstruction:
    verdict: str
    retry_reason: str
    strategy: Mapping[str, object] | str = field(default_factory=dict)
    expected_evidence: Sequence[object] = field(default_factory=tuple)
    selected_finding_id: str | None = None
    change_intent: str = ""
    prior_strategy_relation: str = "new"
    attempt_key: str | None = None
    strategy_key: str | None = None
    # Prototype/API compatibility alias.
    finding_id: str | None = None

    def __post_init__(self) -> None:
        if self.selected_finding_id is None and self.finding_id is not None:
            object.__setattr__(self, "selected_finding_id", self.finding_id)
        if isinstance(self.strategy, str):
            object.__setattr__(self, "strategy", {"operation": self.strategy})
        if isinstance(self.expected_evidence, list):
            object.__setattr__(self, "expected_evidence", tuple(self.expected_evidence))

    @classmethod
    def from_value(cls, value: object, *, verdict: str | None = None) -> "RetryInstruction":
        if isinstance(value, RetryInstruction):
            return value
        raw = _as_mapping(value, "retry instruction")
        allowed = {
            "schema_version", "verdict", "retry_reason", "reason", "strategy", "expected_evidence",
            "selected_finding_id", "finding_id", "change_intent", "prior_strategy_relation",
            "attempt_key", "strategy_key",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ConvergenceError(f"retry instruction has unsupported fields: {sorted(unknown)}")
        return cls(
            verdict=raw.get("verdict", verdict),
            retry_reason=raw.get("retry_reason", raw.get("reason", "")),
            strategy=raw.get("strategy", {}),
            expected_evidence=raw.get("expected_evidence", ()),
            selected_finding_id=raw.get("selected_finding_id", raw.get("finding_id")),
            change_intent=raw.get("change_intent", ""),
            prior_strategy_relation=raw.get("prior_strategy_relation", "new"),
            attempt_key=raw.get("attempt_key"),
            strategy_key=raw.get("strategy_key"),
        )

    def canonical_strategy_key(self) -> str:
        strategy = self.strategy
        if isinstance(strategy, str):
            strategy = {"operation": strategy}
        if not isinstance(strategy, Mapping):
            return canonical_sha256({"strategy": strategy})
        normalized = {
            str(key): value
            for key, value in strategy.items()
            if key not in {"prior_strategy_relation", "change_intent"}
        }
        return canonical_sha256(normalized)

    def normalized(self, *, attempt_key: str | None = None) -> "RetryInstruction":
        return RetryInstruction(
            verdict=self.verdict,
            retry_reason=self.retry_reason,
            strategy=dict(self.strategy) if isinstance(self.strategy, Mapping) else self.strategy,
            expected_evidence=tuple(self.expected_evidence),
            selected_finding_id=self.selected_finding_id,
            change_intent=self.change_intent,
            prior_strategy_relation=self.prior_strategy_relation,
            attempt_key=attempt_key if attempt_key is not None else self.attempt_key,
            strategy_key=self.canonical_strategy_key(),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": self.verdict,
            "retry_reason": self.retry_reason,
            "selected_finding_id": self.selected_finding_id,
            "strategy": dict(self.strategy) if isinstance(self.strategy, Mapping) else self.strategy,
            "strategy_key": self.strategy_key or self.canonical_strategy_key(),
            "prior_strategy_relation": self.prior_strategy_relation,
            "change_intent": self.change_intent,
            "expected_evidence": [
                dict(item) if isinstance(item, Mapping) else item
                for item in self.expected_evidence
            ],
            "attempt_key": self.attempt_key,
        }


RetryProposal = RetryInstruction


@dataclass(frozen=True)
class AuthoritySignal:
    run_id: str
    target_sha256: str
    issuer: str
    authority_scope: str
    action: str = "approve"
    issued_at: str | None = None
    expires_at: str | None = None
    attempt_number: int | None = None
    approved: bool = True

    @classmethod
    def from_value(cls, value: object) -> "AuthoritySignal":
        if isinstance(value, AuthoritySignal):
            return value
        raw = _as_mapping(value, "authority signal")
        return cls(
            run_id=raw.get("run_id"),
            target_sha256=raw.get("target_sha256"),
            issuer=raw.get("issuer"),
            authority_scope=raw.get("authority_scope", ""),
            action=raw.get("action", "approve"),
            issued_at=raw.get("issued_at"),
            expires_at=raw.get("expires_at"),
            attempt_number=raw.get("attempt_number"),
            approved=raw.get("approved", True),
        )

    def validate_for(self, *, run_id: str, target_digest: str) -> bool:
        try:
            _validate_id(self.run_id, "authority signal run_id", pattern=RUN_ID_PATTERN)
            _validate_sha(self.target_sha256, "authority signal target_sha256")
            _as_non_empty_string(self.issuer, "authority signal issuer", max_length=256)
            _as_non_empty_string(self.authority_scope, "authority signal authority_scope", max_length=256)
        except ConvergenceError:
            return False
        now = datetime.now(timezone.utc)
        for raw_timestamp, field_name in ((self.issued_at, "issued_at"), (self.expires_at, "expires_at")):
            if raw_timestamp is None:
                continue
            if not isinstance(raw_timestamp, str):
                return False
            try:
                timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            except ValueError:
                return False
            if timestamp.tzinfo is None:
                return False
            if field_name == "expires_at" and timestamp <= now:
                return False
            if field_name == "issued_at" and timestamp > now:
                return False
        return (
            self.approved
            and self.run_id == run_id
            and self.target_sha256 == target_digest
            and self.action == "approve"
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "target_sha256": self.target_sha256,
            "issuer": self.issuer,
            "authority_scope": self.authority_scope,
            "action": self.action,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "attempt_number": self.attempt_number,
            "approved": self.approved,
        }


@dataclass(frozen=True)
class ProgressEvidence:
    progress: bool
    improvement: tuple[str, ...] = ()
    veto: tuple[str, ...] = ()
    added_findings: tuple[str, ...] = ()
    resolved_findings: tuple[str, ...] = ()
    changed_findings: tuple[str, ...] = ()
    before_digest: str | None = None
    after_digest: str | None = None
    rule_version: str = PROGRESS_RULE_VERSION
    reason_code: str = "baseline"

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "progress": self.progress,
            "improvement": list(self.improvement),
            "veto": list(self.veto),
            "added_findings": list(self.added_findings),
            "resolved_findings": list(self.resolved_findings),
            "changed_findings": list(self.changed_findings),
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "rule_version": self.rule_version,
            "reason_code": self.reason_code,
        }


def _result_payload(attempt: Mapping[str, object]) -> Mapping[str, object]:
    result = attempt.get("result")
    if isinstance(result, Mapping):
        return result
    return attempt


def _finding_id(finding: Mapping[str, object]) -> str:
    value = finding.get("id", finding.get("finding_id", finding.get("key")))
    return str(value) if value is not None else ""


def _open_findings(value: Mapping[str, object]) -> list[dict[str, object]]:
    result = _result_payload(value)
    raw = result.get("findings", ())
    if not isinstance(raw, (tuple, list)):
        return []
    findings: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status", item.get("lifecycle", "open"))
        if status == "open" and _finding_id(item):
            findings.append(dict(item))
    return findings


def open_findings(value: Mapping[str, object]) -> list[dict[str, object]]:
    return _open_findings(value)


def _verdict(value: Mapping[str, object]) -> str | None:
    result = _result_payload(value)
    raw = result.get("verdict")
    return raw if isinstance(raw, str) else None


def _reason(value: Mapping[str, object]) -> str:
    result = _result_payload(value)
    raw = result.get("decision_reason", result.get("reason_code", "unknown"))
    return str(raw)


def _verify_state(value: Mapping[str, object]) -> str:
    result = _result_payload(value)
    observations = result.get("observations", {})
    if isinstance(observations, Mapping):
        verify = observations.get("verify", {})
        if isinstance(verify, Mapping) and isinstance(verify.get("state"), str):
            return str(verify["state"])
    verify = result.get("verify", {})
    if isinstance(verify, Mapping) and isinstance(verify.get("state"), str):
        return str(verify["state"])
    return "unknown"


def verification_state(value: Mapping[str, object]) -> str:
    return _verify_state(value)


def _canonical_finding_id(finding: Mapping[str, object]) -> str:
    identity = finding.get("identity", finding.get("identity_key"))
    if isinstance(identity, str) and identity:
        return identity
    return _finding_id(finding)


def _severity(finding: Mapping[str, object]) -> int:
    return SEVERITY_RANK.get(str(finding.get("severity", "low")), 0)


def _finding_map(value: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {_canonical_finding_id(item): item for item in _open_findings(value)}


def _check_failures(value: Mapping[str, object]) -> tuple[set[str], set[str]]:
    result = _result_payload(value)
    observations = result.get("observations", {})
    checks = observations.get("checks") if isinstance(observations, Mapping) else None
    if not isinstance(checks, Mapping):
        checks = result.get("checks")
    if not isinstance(checks, Mapping):
        return set(), set()
    failures: set[str] = set()
    names: set[str] = set()
    for name, state in checks.items():
        names.add(str(name))
        if isinstance(state, Mapping):
            state = state.get("state", state.get("status"))
        if state not in {"succeeded", "success", "passed", True}:
            failures.add(str(name))
    return failures, names


def evaluate_progress(
    previous: Mapping[str, object] | None,
    current: Mapping[str, object],
    *,
    rule_version: str = PROGRESS_RULE_VERSION,
) -> ProgressEvidence:
    """Compare two finalized results using the conservative v1 rule."""

    current_result = _result_payload(current)
    after_digest = current_result.get("result_sha256", current.get("result_sha256"))
    if previous is None:
        return ProgressEvidence(
            progress=False,
            before_digest=None,
            after_digest=after_digest if isinstance(after_digest, str) else None,
            rule_version=rule_version,
            reason_code="baseline",
        )

    before_map = _finding_map(previous)
    after_map = _finding_map(current)
    before_ids = set(before_map)
    after_ids = set(after_map)
    added = tuple(sorted(after_ids - before_ids))
    resolved = tuple(sorted(before_ids - after_ids))
    changed = tuple(sorted(
        item_id
        for item_id in before_ids & after_ids
        if before_map[item_id].get("severity") != after_map[item_id].get("severity")
    ))
    improvement: list[str] = []
    veto: list[str] = []
    if resolved and not added:
        improvement.append("open_finding_set_reduced")
    before_failures, before_checks = _check_failures(previous)
    after_failures, after_checks = _check_failures(current)
    if before_checks and after_checks:
        if before_checks - after_checks:
            veto.append("check_set_missing")
        elif after_failures < before_failures:
            improvement.append("check_failures_reduced")
    elif _verify_state(previous) != "succeeded" and _verify_state(current) == "succeeded":
        improvement.append("verify_succeeded")
    for item_id in added:
        if _severity(after_map[item_id]) >= SEVERITY_RANK["high"]:
            veto.append("new_high_or_critical_finding")
    for item_id in changed:
        if _severity(after_map[item_id]) > _severity(before_map[item_id]):
            veto.append("finding_severity_worsened")
    identity_ambiguous = bool(
        current_result.get("finding_identity_ambiguous")
        or current.get("finding_identity_ambiguous")
    )
    if identity_ambiguous:
        veto.append("identity_ambiguity")
    progress = bool(improvement) and not veto
    reason = "progress_observed" if progress else (
        "progress_vetoed" if veto else "no_progress_observed"
    )
    before_digest = previous.get("result_sha256")
    if not isinstance(before_digest, str):
        before_digest = _result_payload(previous).get("result_sha256")
    return ProgressEvidence(
        progress=progress,
        improvement=tuple(improvement),
        veto=tuple(veto),
        added_findings=added,
        resolved_findings=resolved,
        changed_findings=changed,
        before_digest=before_digest if isinstance(before_digest, str) else None,
        after_digest=after_digest if isinstance(after_digest, str) else None,
        rule_version=rule_version,
        reason_code=reason,
    )


def made_progress(previous: Mapping[str, object], current: Mapping[str, object]) -> bool:
    return evaluate_progress(previous, current).progress


def _sample_values(value: object, *, metric: str | None = None) -> list[UsageSample]:
    if value is None:
        return []
    if isinstance(value, UsageSample):
        return [value]
    if isinstance(value, Mapping):
        # A metric map is accepted in addition to the canonical sample shape.
        if "metric" in value or "availability" in value:
            return [UsageSample.from_value(value, metric=metric)]
        samples: list[UsageSample] = []
        for key, item in value.items():
            if key not in METRICS:
                continue
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                samples.append(UsageSample(key, value=item))
            elif item is not None:
                samples.extend(_sample_values(item, metric=key))
        return samples
    if isinstance(value, (tuple, list)):
        return [UsageSample.from_value(item, metric=metric) for item in value]
    if metric is not None:
        return [UsageSample(metric, value=value)]
    raise ConvergenceError("usage must contain samples or a metric mapping")


def aggregate_usage(attempts: Sequence[Mapping[str, object]]) -> dict[str, dict[str, object]]:
    """Aggregate usage without treating unavailable values as zero."""

    samples: list[UsageSample] = []
    for attempt in attempts:
        raw_usage = attempt.get("usage")
        if raw_usage is None:
            result = _result_payload(attempt)
            raw_usage = result.get("usage")
        samples.extend(_sample_values(raw_usage))

    seen: set[str] = set()
    unique: list[UsageSample] = []
    for sample in samples:
        identity = sample.sample_id or canonical_sha256(sample.to_payload())
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(sample)

    result: dict[str, dict[str, object]] = {}
    for metric in sorted(METRICS):
        metric_samples = [item for item in unique if item.metric == metric]
        if not metric_samples:
            result[metric] = {
                "value": None,
                "availability": "unavailable",
                "samples": [],
                "reason_code": "no_sample",
            }
            continue
        if any(item.availability == "invalid" for item in metric_samples):
            availability = "invalid"
            value = None
            reason = "invalid_sample"
        else:
            available = [item for item in metric_samples if item.availability == "available"]
            partial = [item for item in metric_samples if item.availability == "partial"]
            unavailable = [item for item in metric_samples if item.availability == "unavailable"]
            units = {item.unit for item in [*available, *partial] if item.unit is not None}
            if len(units) > 1:
                availability = "invalid"
                value = None
                reason = "unit_mismatch"
            elif partial:
                availability = "partial"
                values = [float(item.value) for item in [*available, *partial] if item.value is not None]
                value = sum(values) if values else None
                reason = "partial_sample"
            elif available:
                # Cumulative samples from one source represent a snapshot; use
                # the greatest value. Attempt samples add independently.
                by_source: dict[tuple[str, str], list[UsageSample]] = {}
                for item in available:
                    by_source.setdefault((item.source, item.unit or ""), []).append(item)
                total = 0.0
                for group in by_source.values():
                    cumulative = [item for item in group if item.scope == "cumulative"]
                    attempt_values = [float(item.value) for item in group if item.scope == "attempt" and item.value is not None]
                    if cumulative:
                        total += max(float(item.value) for item in cumulative if item.value is not None)
                        total += sum(attempt_values)
                    else:
                        total += sum(attempt_values)
                availability = "available"
                value = int(total) if total.is_integer() else total
                reason = None
            else:
                availability = "unavailable"
                value = None
                reason = "unavailable_sample"
        result[metric] = {
            "value": value,
            "availability": availability,
            "samples": [item.to_payload() for item in metric_samples],
            "reason_code": reason,
        }
    return result


def derive_history(attempts: Sequence[Mapping[str, object]], policy: ConvergencePolicy) -> dict[str, object]:
    """Derive counters and explainable evidence from finalized attempts."""

    policy.validate()
    finding_occurrences: Counter[str] = Counter()
    strategy_occurrences: Counter[str] = Counter()
    failure_counters: Counter[str] = Counter()
    progress_records: list[dict[str, object]] = []
    no_progress = 0
    previous: Mapping[str, object] | None = None
    previous_failure_key: str | None = None
    finalized: list[Mapping[str, object]] = []
    for attempt in attempts:
        lifecycle = attempt.get("lifecycle_state", attempt.get("state", "result_bound"))
        result = _result_payload(attempt)
        if lifecycle in {"reserved", "started", "terminal_unknown"} and "verdict" not in result:
            continue
        finalized.append(attempt)
        for finding in _open_findings(attempt):
            finding_occurrences[_canonical_finding_id(finding)] += 1
        instruction = attempt.get("retry_instruction")
        if not isinstance(instruction, Mapping):
            reservation = attempt.get("reservation")
            if isinstance(reservation, Mapping):
                instruction = reservation.get("retry_instruction")
        if isinstance(instruction, Mapping):
            strategy_key = instruction.get("strategy_key")
            if not isinstance(strategy_key, str):
                try:
                    strategy_key = RetryInstruction.from_value(instruction).canonical_strategy_key()
                except ConvergenceError:
                    strategy_key = None
            if strategy_key:
                strategy_occurrences[strategy_key] += 1
        else:
            legacy_strategy = result.get("strategy_key", result.get("change_intent_digest"))
            if isinstance(legacy_strategy, str) and legacy_strategy:
                strategy_occurrences[legacy_strategy] += 1
        verdict = _verdict(attempt)
        reason = _reason(attempt)
        failure_key = f"{verdict}:{reason}"
        if verdict in RETRY_VERDICTS:
            if failure_key == previous_failure_key:
                failure_counters[failure_key] += 1
            else:
                failure_counters[failure_key] = 1
            previous_failure_key = failure_key
        else:
            previous_failure_key = None
        progress = evaluate_progress(previous, attempt, rule_version=policy.progress_rule_version)
        if previous is not None:
            if progress.progress:
                no_progress = 0
            else:
                no_progress += 1
        progress_records.append(progress.to_payload())
        previous = attempt
    usage = aggregate_usage(finalized)
    return {
        "finding_occurrences": dict(sorted(finding_occurrences.items())),
        "strategy_occurrences": dict(sorted(strategy_occurrences.items())),
        "failure_counters": dict(sorted(failure_counters.items())),
        "consecutive_no_progress": no_progress,
        "progress_records": progress_records,
        "usage": usage,
        "finalized_attempt_count": len(finalized),
    }


@dataclass(frozen=True)
class ConvergenceRequest:
    work_item_id: str
    target: object
    policy: ConvergencePolicy | Mapping[str, object]
    run_id: str = "run-1"
    initial_instruction: RetryInstruction | Mapping[str, object] | None = None
    evaluation_request: object | None = None
    authority_signals: Sequence[AuthoritySignal | Mapping[str, object]] = ()
    human_approval_required: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)
    active_target: object | None = None

    def __post_init__(self) -> None:
        if self.active_target is not None:
            object.__setattr__(self, "target", self.active_target)
        policy = self.policy if isinstance(self.policy, ConvergencePolicy) else ConvergencePolicy.from_mapping(self.policy)
        policy.validate()
        object.__setattr__(self, "policy", policy)
        _validate_id(self.work_item_id, "work_item_id")
        _validate_id(self.run_id, "run_id", pattern=RUN_ID_PATTERN)
        _target_payload(self.target)
        if self.initial_instruction is not None:
            object.__setattr__(
                self,
                "initial_instruction",
                RetryInstruction.from_value(self.initial_instruction, verdict="changes_requested"),
            )
        object.__setattr__(
            self,
            "authority_signals",
            tuple(AuthoritySignal.from_value(item) for item in self.authority_signals),
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ConvergenceRequest":
        allowed = {
            "schema_version", "work_item_id", "target", "active_target", "policy", "run_id",
            "initial_instruction", "retry_instruction", "evaluation_request", "authority_signals",
            "human_approval_required", "metadata",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ConvergenceError(f"convergence request has unsupported fields: {sorted(unknown)}")
        target = raw.get("target", raw.get("active_target"))
        policy = raw.get("policy")
        if not isinstance(policy, (ConvergencePolicy, Mapping)):
            raise ConvergenceError("convergence request policy is required")
        return cls(
            work_item_id=raw.get("work_item_id"),
            target=target,
            policy=policy,
            run_id=raw.get("run_id", "run-1"),
            initial_instruction=raw.get("initial_instruction", raw.get("retry_instruction")),
            evaluation_request=raw.get("evaluation_request"),
            authority_signals=raw.get("authority_signals", ()),
            human_approval_required=raw.get("human_approval_required", False),
            metadata=raw.get("metadata", {}),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "work_item_id": self.work_item_id,
            "target": _target_payload(self.target),
            "target_sha256": target_sha256(self.target),
            "policy_sha256": self.policy.policy_sha256,
            "policy": self.policy.to_payload(),
            "authority_signals": [item.to_payload() for item in self.authority_signals],
            "human_approval_required": self.human_approval_required,
            "metadata": dict(self.metadata),
        }


class ConvergenceRequestValidator:
    def validate(self, request: ConvergenceRequest | Mapping[str, object]) -> ConvergenceRequest:
        if isinstance(request, Mapping):
            request = ConvergenceRequest.from_mapping(request)
        if not isinstance(request, ConvergenceRequest):
            raise TypeError("request must be a ConvergenceRequest or mapping")
        request.policy.validate()
        _validate_id(request.work_item_id, "work_item_id")
        _validate_id(request.run_id, "run_id", pattern=RUN_ID_PATTERN)
        _target_payload(request.target)
        return request


@dataclass(frozen=True)
class ValidatedHistorySnapshot:
    run_id: str
    work_item_id: str
    target: Mapping[str, str]
    policy: ConvergencePolicy
    attempts: tuple[Mapping[str, object], ...] = ()
    reserved_attempt_count: int = 0
    derived: Mapping[str, object] = field(default_factory=dict)
    validation_errors: tuple[str, ...] = ()
    unresolved_started: bool = False
    human_approval_required: bool = False
    target_sha256: str | None = None
    policy_sha256: str | None = None
    snapshot_sha256: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.run_id, "snapshot.run_id", pattern=RUN_ID_PATTERN)
        _validate_id(self.work_item_id, "snapshot.work_item_id")
        target = _target_payload(self.target)
        object.__setattr__(self, "target", target)
        self.policy.validate()
        attempts = tuple(dict(item) for item in self.attempts)
        object.__setattr__(self, "attempts", attempts)
        if self.reserved_attempt_count < 0:
            raise ConvergenceError("reserved_attempt_count cannot be negative")
        if self.target_sha256 is None:
            object.__setattr__(self, "target_sha256", canonical_sha256(target))
        if self.policy_sha256 is None:
            object.__setattr__(self, "policy_sha256", self.policy.policy_sha256)
        if not self.derived:
            object.__setattr__(self, "derived", derive_history(attempts, self.policy))
        if self.snapshot_sha256 is None:
            object.__setattr__(self, "snapshot_sha256", canonical_sha256(self._payload_without_digest()))

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "work_item_id": self.work_item_id,
            "target": dict(self.target),
            "target_sha256": self.target_sha256,
            "policy_sha256": self.policy_sha256,
            "reserved_attempt_count": self.reserved_attempt_count,
            "attempts": [dict(item) for item in self.attempts],
            "derived": dict(self.derived),
            "validation_errors": list(self.validation_errors),
            "unresolved_started": self.unresolved_started,
            "human_approval_required": self.human_approval_required,
        }

    @property
    def current(self) -> Mapping[str, object] | None:
        for attempt in reversed(self.attempts):
            result = _result_payload(attempt)
            if _verdict(attempt) is not None:
                return attempt
        return self.attempts[-1] if self.attempts else None

    @property
    def current_verdict(self) -> str | None:
        return _verdict(self.current) if self.current else None

    @property
    def current_open_findings(self) -> list[dict[str, object]]:
        return _open_findings(self.current) if self.current else []

    def to_payload(self) -> dict[str, object]:
        payload = self._payload_without_digest()
        payload["snapshot_sha256"] = self.snapshot_sha256
        return payload


def build_snapshot(
    history: Sequence[Mapping[str, object]],
    policy: ConvergencePolicy | Mapping[str, object],
    *,
    run_id: str = "run-1",
    work_item_id: str = "work-item",
    target: object | None = None,
    reserved_attempt_count: int | None = None,
    validation_errors: Sequence[str] = (),
    unresolved_started: bool = False,
    human_approval_required: bool = False,
) -> ValidatedHistorySnapshot:
    normalized_policy = policy if isinstance(policy, ConvergencePolicy) else ConvergencePolicy.from_mapping(policy)
    normalized_policy.validate()
    if target is None:
        target = {"kind": "work_item", "id": work_item_id, "source_ref": "manual"}
    attempts = tuple(dict(item) for item in history)
    reserved = len(attempts) if reserved_attempt_count is None else reserved_attempt_count
    return ValidatedHistorySnapshot(
        run_id=run_id,
        work_item_id=work_item_id,
        target=_target_payload(target),
        policy=normalized_policy,
        attempts=attempts,
        reserved_attempt_count=reserved,
        validation_errors=tuple(validation_errors),
        unresolved_started=unresolved_started,
        human_approval_required=human_approval_required,
    )


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    terminal_state: str | None
    reason_code: str
    failure_class: str = "none"
    acceptance_outcome: str = "unknown"
    compliance_status: str = "unknown"
    selected_finding_id: str | None = None
    retry_instruction: RetryInstruction | None = None
    evidence_refs: tuple[str, ...] = ()
    snapshot_sha256: str | None = None
    policy_sha256: str | None = None
    remaining_findings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ConvergenceError(f"unsupported policy action: {self.action}")
        if self.terminal_state is not None and self.terminal_state not in TERMINAL_STATES:
            raise ConvergenceError(f"unsupported terminal state: {self.terminal_state}")
        if self.action == "retry" and self.terminal_state is not None:
            raise ConvergenceError("retry decision cannot have a terminal state")
        if self.action != "retry" and self.retry_instruction is not None:
            raise ConvergenceError("terminal decision cannot contain a retry instruction")
        _as_non_empty_string(self.reason_code, "decision reason_code", max_length=256)

    @property
    def decision_sha256(self) -> str:
        return canonical_sha256(self.to_payload(include_digest=False))

    @property
    def reason(self) -> str:
        return self.reason_code

    @property
    def retry_reason(self) -> str | None:
        return self.retry_instruction.retry_reason if self.retry_instruction is not None else None

    @property
    def change_intent_digest(self) -> str | None:
        if self.retry_instruction is None:
            return None
        return digest_intent(self.retry_instruction.change_intent)

    def __getitem__(self, key: str) -> object:
        if key == "reason":
            key = "reason_code"
        return self.to_payload()[key]

    def get(self, key: str, default: object = None) -> object:
        try:
            return self[key]
        except KeyError:
            return default

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "action": self.action,
            "terminal_state": self.terminal_state,
            "reason_code": self.reason_code,
            "failure_class": self.failure_class,
            "acceptance_outcome": self.acceptance_outcome,
            "compliance_status": self.compliance_status,
            "selected_finding_id": self.selected_finding_id,
            "retry_instruction": self.retry_instruction.to_payload()
            if self.retry_instruction is not None else None,
            "evidence_refs": list(self.evidence_refs),
            "snapshot_sha256": self.snapshot_sha256,
            "policy_sha256": self.policy_sha256,
            "remaining_findings": list(self.remaining_findings),
        }
        if include_digest:
            payload["decision_sha256"] = self.decision_sha256
        return payload

    @classmethod
    def from_value(cls, value: object) -> "PolicyDecision":
        if isinstance(value, PolicyDecision):
            return value
        raw = _as_mapping(value, "policy decision")
        instruction = raw.get("retry_instruction")
        return cls(
            action=raw.get("action"),
            terminal_state=raw.get("terminal_state"),
            reason_code=raw.get("reason_code", raw.get("reason", "")),
            failure_class=raw.get("failure_class", "none"),
            acceptance_outcome=raw.get("acceptance_outcome", "unknown"),
            compliance_status=raw.get("compliance_status", "unknown"),
            selected_finding_id=raw.get("selected_finding_id"),
            retry_instruction=RetryInstruction.from_value(instruction)
            if instruction is not None else None,
            evidence_refs=tuple(raw.get("evidence_refs", ())),
            snapshot_sha256=raw.get("snapshot_sha256"),
            policy_sha256=raw.get("policy_sha256"),
            remaining_findings=tuple(raw.get("remaining_findings", ())),
        )


def _decision(
    *,
    action: str,
    terminal_state: str | None,
    reason_code: str,
    snapshot: ValidatedHistorySnapshot,
    failure_class: str = "none",
    acceptance_outcome: str = "unknown",
    compliance_status: str = "unknown",
    selected_finding_id: str | None = None,
    retry_instruction: RetryInstruction | None = None,
    evidence: Sequence[str] = (),
) -> PolicyDecision:
    return PolicyDecision(
        action=action,
        terminal_state=terminal_state,
        reason_code=reason_code,
        failure_class=failure_class,
        acceptance_outcome=acceptance_outcome,
        compliance_status=compliance_status,
        selected_finding_id=selected_finding_id,
        retry_instruction=retry_instruction,
        evidence_refs=tuple(evidence) or (
            f"snapshot:{snapshot.snapshot_sha256}",
        ),
        snapshot_sha256=snapshot.snapshot_sha256,
        policy_sha256=snapshot.policy_sha256,
        remaining_findings=tuple(_finding_id(item) for item in snapshot.current_open_findings),
    )


def _proposal_from_current(current: Mapping[str, object], verdict: str) -> RetryInstruction | None:
    for key in ("retry_instruction", "next_retry", "proposal"):
        value = current.get(key)
        if isinstance(value, (Mapping, RetryInstruction)):
            try:
                return RetryInstruction.from_value(value, verdict=verdict)
            except ConvergenceError:
                return None
    reservation = current.get("reservation")
    if isinstance(reservation, Mapping):
        value = reservation.get("retry_instruction")
        if isinstance(value, (Mapping, RetryInstruction)):
            try:
                return RetryInstruction.from_value(value, verdict=verdict)
            except ConvergenceError:
                return None
    return None


def _reason_allowed(policy: ConvergencePolicy, verdict: str, reason: str) -> bool:
    values = policy.retryable_reasons.get(verdict, ())
    return "*" in values or reason in values


def _instruction_error(
    instruction: RetryInstruction | Mapping[str, object] | None,
    *,
    verdict: str,
    selected_finding_id: str | None,
) -> tuple[RetryInstruction | None, str | None]:
    if instruction is None:
        return None, "missing_retry_instruction"
    try:
        parsed = RetryInstruction.from_value(instruction, verdict=verdict)
    except (ConvergenceError, TypeError) as exc:
        return None, str(exc)
    if parsed.verdict != verdict:
        return None, "retry instruction verdict does not match current verdict"
    if not isinstance(parsed.retry_reason, str) or not REASON_PATTERN.fullmatch(parsed.retry_reason):
        return None, "retry_reason must be a stable reason code"
    if parsed.prior_strategy_relation not in STRATEGY_RELATIONS:
        return None, "unsupported prior_strategy_relation"
    if not isinstance(parsed.strategy, Mapping) or not parsed.strategy:
        return None, "strategy must be a non-empty structured object"
    if not parsed.expected_evidence or not isinstance(parsed.expected_evidence, (tuple, list)):
        return None, "expected_evidence must be non-empty"
    for evidence in parsed.expected_evidence:
        if not isinstance(evidence, Mapping) and (
            not isinstance(evidence, str) or not evidence.strip()
        ):
            return None, "expected_evidence contains an invalid item"
    if not isinstance(parsed.change_intent, str) or not parsed.change_intent.strip():
        return None, "change_intent must be non-empty"
    if len(parsed.change_intent) > MAX_CHANGE_INTENT_LENGTH:
        return None, "change_intent is too long"
    if selected_finding_id is not None and parsed.selected_finding_id != selected_finding_id:
        return None, "retry instruction selected finding does not match policy selection"
    if parsed.attempt_key is not None and (
        not isinstance(parsed.attempt_key, str) or not SHA256_PATTERN.fullmatch(parsed.attempt_key)
    ):
        return None, "attempt_key must be a sha256 digest"
    return parsed.normalized(), None


def _same_strategy_keys(snapshot: ValidatedHistorySnapshot) -> Counter[str]:
    raw = snapshot.derived.get("strategy_occurrences", {})
    if isinstance(raw, Mapping):
        return Counter({str(key): int(value) for key, value in raw.items()})
    return Counter()


class PolicyReducer:
    """Pure, deterministic convergence policy reducer."""

    def decide(
        self,
        snapshot: ValidatedHistorySnapshot | Mapping[str, object],
        proposal: RetryInstruction | Mapping[str, object] | None = None,
        signals: Mapping[str, object] | None = None,
        *,
        human_approval_required: bool = False,
    ) -> PolicyDecision:
        if isinstance(snapshot, Mapping):
            snapshot = self._snapshot_from_mapping(snapshot)
        if not isinstance(snapshot, ValidatedHistorySnapshot):
            raise TypeError("snapshot must be a ValidatedHistorySnapshot or object")
        policy = snapshot.policy
        policy.validate()
        signals = signals or {}
        current = snapshot.current

        # Corruption and unknown side effects are always evaluated before any
        # business verdict so an invalid history can never produce retry.
        if snapshot.validation_errors:
            reason = snapshot.validation_errors[0]
            return _decision(
                action="stop", terminal_state="blocked",
                reason_code="history_corrupt",
                failure_class="history", acceptance_outcome="unknown",
                compliance_status="unknown", snapshot=snapshot,
                evidence=(f"validation:{reason}",),
            )
        if snapshot.unresolved_started:
            return _decision(
                action="handoff", terminal_state="waiting_for_human",
                reason_code="attempt_outcome_unknown", failure_class="operator",
                snapshot=snapshot,
            )
        if self._has_valid_authority_signal(snapshot, signals) or human_approval_required or snapshot.human_approval_required:
            return _decision(
                action="handoff", terminal_state="waiting_for_human",
                reason_code="human_approval_required", failure_class="authority",
                snapshot=snapshot,
            )
        if current is None:
            return _decision(
                action="stop", terminal_state="blocked",
                reason_code="empty_history", failure_class="history", snapshot=snapshot,
            )
        verdict = _verdict(current)
        findings = snapshot.current_open_findings
        if verdict not in VERDICTS:
            return _decision(
                action="stop", terminal_state="blocked",
                reason_code="unsupported_verdict", failure_class="operator",
                snapshot=snapshot,
            )
        if verdict == "plan_defect":
            return _decision(
                action="handoff", terminal_state="waiting_for_human",
                reason_code="plan_defect", failure_class="plan",
                acceptance_outcome="unsatisfied", snapshot=snapshot,
            )

        if verdict == "satisfied":
            if findings:
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code="satisfied_with_open_findings", failure_class="history",
                    acceptance_outcome="unsatisfied", snapshot=snapshot,
                )
            compliance = self._post_run_compliance(snapshot)
            return _decision(
                action="finish", terminal_state="satisfied",
                reason_code="acceptance_satisfied", acceptance_outcome="satisfied",
                compliance_status=compliance, snapshot=snapshot,
            )

        if not findings and verdict == "changes_requested":
            return _decision(
                action="stop", terminal_state="blocked",
                reason_code="changes_requested_without_open_finding", failure_class="history",
                acceptance_outcome="unsatisfied", snapshot=snapshot,
            )
        if snapshot.reserved_attempt_count >= int(policy.max_iterations):
            return _decision(
                action="stop", terminal_state="blocked",
                reason_code="max_iterations_reached", failure_class="work_item",
                acceptance_outcome="unsatisfied", compliance_status="limit_reached",
                snapshot=snapshot,
            )

        budget_decision = self._budget_guard(snapshot)
        if budget_decision is not None:
            return budget_decision

        occurrences = snapshot.derived.get("finding_occurrences", {})
        if isinstance(occurrences, Mapping) and findings:
            if any(int(occurrences.get(_canonical_finding_id(item), 0)) >= policy.max_finding_occurrences for item in findings):
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code="finding_recurrence_limit", failure_class="work_item",
                    acceptance_outcome="unsatisfied", snapshot=snapshot,
                )
        no_progress = int(snapshot.derived.get("consecutive_no_progress", 0))
        if no_progress >= policy.max_consecutive_no_progress:
            return _decision(
                action="stop", terminal_state="blocked",
                reason_code="no_progress_limit", failure_class="work_item",
                acceptance_outcome="unsatisfied", snapshot=snapshot,
            )

        reason = _reason(current)
        if verdict in RETRY_VERDICTS:
            failure_key = f"{verdict}:{reason}"
            counter = int(snapshot.derived.get("failure_counters", {}).get(failure_key, 0))
            if counter > int(policy.retry_limits.get(verdict, 0)):
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code=f"{verdict}_retry_limit", failure_class="work_item",
                    acceptance_outcome="unsatisfied", snapshot=snapshot,
                )
            if verdict in {"execution_failed", "invalid_output"} and not _reason_allowed(policy, verdict, reason):
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code=f"{verdict}_not_retryable", failure_class="work_item",
                    acceptance_outcome="unsatisfied", snapshot=snapshot,
                )

        selected: str | None = None
        if verdict == "changes_requested":
            candidate = self._select_finding(findings)
            if candidate is None:
                return _decision(
                    action="handoff", terminal_state="waiting_for_human",
                    reason_code="finding_selection_ambiguous", failure_class="authority",
                    acceptance_outcome="unsatisfied", snapshot=snapshot,
                )
            selected = _finding_id(candidate)

        instruction = proposal
        if instruction is None:
            instruction = _proposal_from_current(current, verdict)
        parsed, error = _instruction_error(instruction, verdict=verdict, selected_finding_id=selected)
        if error is not None or parsed is None:
            return _decision(
                action="stop", terminal_state="blocked",
                reason_code="retry_instruction_invalid", failure_class="operator",
                acceptance_outcome="unsatisfied", selected_finding_id=selected,
                snapshot=snapshot, evidence=(f"retry_instruction:{error or 'invalid'}",),
            )

        if verdict in {"execution_failed", "invalid_output"} and parsed.retry_reason != reason:
            # A change of reason class must be represented as a new strategy;
            # silently carrying the prior failure instruction is unsafe.
            if parsed.prior_strategy_relation == "transient_retry" and parsed.retry_reason not in policy.transient_reuse_reasons:
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code="retry_reason_mismatch", failure_class="operator",
                    snapshot=snapshot,
                )

        strategy_key = parsed.strategy_key or parsed.canonical_strategy_key()
        strategy_counts = _same_strategy_keys(snapshot)
        prior_count = strategy_counts.get(strategy_key, 0)
        prior_intent_digests = {
            str(_result_payload(attempt).get("change_intent_digest"))
            for attempt in snapshot.attempts
            if _result_payload(attempt).get("change_intent_digest")
        }
        legacy_same_intent = digest_intent(parsed.change_intent) in prior_intent_digests
        if prior_count or legacy_same_intent:
            transient_allowed = (
                parsed.prior_strategy_relation == "transient_retry"
                and parsed.retry_reason in policy.transient_reuse_reasons
                and prior_count <= policy.transient_strategy_reuse_limit
            )
            if not transient_allowed:
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code="same_strategy_reused", failure_class="work_item",
                    acceptance_outcome="unsatisfied", selected_finding_id=selected,
                    snapshot=snapshot,
                )

        normalized = parsed.normalized()
        return _decision(
            action="retry", terminal_state=None,
            reason_code=("open_finding_selected" if verdict == "changes_requested" else f"retry_{verdict}"),
            acceptance_outcome="unsatisfied", compliance_status="within_policy",
            selected_finding_id=selected, retry_instruction=normalized, snapshot=snapshot,
        )

    @staticmethod
    def _snapshot_from_mapping(raw: Mapping[str, object]) -> ValidatedHistorySnapshot:
        policy_raw = raw.get("policy", {})
        policy = policy_raw if isinstance(policy_raw, ConvergencePolicy) else ConvergencePolicy.from_mapping(policy_raw)
        target = raw.get("target", {
            "kind": "work_item",
            "id": raw.get("work_item_id", "work-item"),
            "source_ref": "manual",
        })
        return ValidatedHistorySnapshot(
            run_id=raw.get("run_id", "run-1"),
            work_item_id=raw.get("work_item_id", "work-item"),
            target=target,
            policy=policy,
            attempts=tuple(raw.get("attempts", raw.get("history", ()))),
            reserved_attempt_count=raw.get("reserved_attempt_count", len(raw.get("attempts", raw.get("history", ())))),
            derived=raw.get("derived", {}),
            validation_errors=tuple(raw.get("validation_errors", ())),
            unresolved_started=raw.get("unresolved_started", False),
            human_approval_required=raw.get("human_approval_required", False),
            target_sha256=raw.get("target_sha256"),
            policy_sha256=raw.get("policy_sha256"),
            snapshot_sha256=raw.get("snapshot_sha256"),
        )

    @staticmethod
    def _select_finding(findings: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
        eligible: list[Mapping[str, object]] = []
        for finding in findings:
            if finding.get("auto_fixable") is False:
                continue
            if finding.get("in_scope") is False:
                continue
            if finding.get("dependency_ready") is False:
                continue
            if finding.get("identity_ambiguous") is True:
                continue
            if not _finding_id(finding):
                continue
            eligible.append(finding)
        if not eligible:
            return None
        return sorted(
            eligible,
            key=lambda item: (
                -_severity(item),
                int(item.get("first_seen_attempt", 10**9))
                if isinstance(item.get("first_seen_attempt"), int) else 10**9,
                _finding_id(item),
            ),
        )[0]

    def _has_valid_authority_signal(
        self,
        snapshot: ValidatedHistorySnapshot,
        signals: Mapping[str, object],
    ) -> bool:
        raw = signals.get("authority_signals", signals.get("approval_signals", ()))
        if isinstance(raw, AuthoritySignal):
            raw = (raw,)
        if not isinstance(raw, (tuple, list)):
            return False
        for item in raw:
            try:
                signal = AuthoritySignal.from_value(item)
            except ConvergenceError:
                continue
            if signal.validate_for(run_id=snapshot.run_id, target_digest=snapshot.target_sha256 or ""):
                return True
        return False

    def _budget_guard(self, snapshot: ValidatedHistorySnapshot) -> PolicyDecision | None:
        usage = snapshot.derived.get("usage", {})
        if not isinstance(usage, Mapping):
            return _decision(
                action="stop", terminal_state="blocked",
                reason_code="usage_invalid", failure_class="operator", snapshot=snapshot,
            )
        for metric, limit in snapshot.policy.budget_limits().items():
            state = usage.get(metric, {})
            if not isinstance(state, Mapping):
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code="usage_invalid", failure_class="operator", snapshot=snapshot,
                )
            availability = state.get("availability", "unavailable")
            samples = state.get("samples", ())
            if limit.metric == "cost" and isinstance(samples, (tuple, list)) and limit.unit != "currency":
                sample_units = {
                    item.get("unit")
                    for item in samples
                    if isinstance(item, Mapping) and item.get("unit") is not None
                }
                if sample_units and sample_units != {limit.unit}:
                    return _decision(
                        action="stop", terminal_state="blocked",
                        reason_code=f"{metric}_unit_mismatch", failure_class="operator", snapshot=snapshot,
                    )
            if availability == "invalid":
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code=f"{metric}_invalid", failure_class="operator", snapshot=snapshot,
                )
            if availability in {"unavailable", "partial"}:
                configured = (
                    snapshot.policy.partial_budget_policy
                    if availability == "partial" else snapshot.policy.unknown_budget_policy
                )
                if configured == "stop":
                    return _decision(
                        action="stop", terminal_state="blocked",
                        reason_code=f"{metric}_{availability}", failure_class="work_item",
                        compliance_status="unknown", snapshot=snapshot,
                    )
                if configured == "handoff":
                    return _decision(
                        action="handoff", terminal_state="waiting_for_human",
                        reason_code=f"{metric}_{availability}", failure_class="authority",
                        compliance_status="unknown", snapshot=snapshot,
                    )
                continue
            value = state.get("value")
            if value is None or not isinstance(value, (int, float)) or isinstance(value, bool):
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code=f"{metric}_invalid", failure_class="operator", snapshot=snapshot,
                )
            if float(value) >= float(limit.limit):
                return _decision(
                    action="stop", terminal_state="blocked",
                    reason_code=f"{metric}_budget_reached", failure_class="work_item",
                    compliance_status="limit_reached", snapshot=snapshot,
                )
        return None

    @staticmethod
    def _post_run_compliance(snapshot: ValidatedHistorySnapshot) -> str:
        usage = snapshot.derived.get("usage", {})
        if not isinstance(usage, Mapping):
            return "unknown"
        for metric, limit in snapshot.policy.budget_limits().items():
            state = usage.get(metric, {})
            if isinstance(state, Mapping) and state.get("availability") == "invalid":
                return "unknown"
            if isinstance(state, Mapping) and state.get("availability") in {"unavailable", "partial"}:
                return "unknown"
            value = state.get("value") if isinstance(state, Mapping) else None
            if isinstance(value, (int, float)) and float(value) >= float(limit.limit):
                return "overrun_observed"
        return "within_policy"


def decide(
    snapshot_or_history: ValidatedHistorySnapshot | Mapping[str, object] | Sequence[Mapping[str, object]],
    policy: ConvergencePolicy | Mapping[str, object] | None = None,
    usage: Mapping[str, object] | None = None,
    proposal: RetryInstruction | Mapping[str, object] | None = None,
    *,
    human_approval_required: bool = False,
    signals: Mapping[str, object] | None = None,
) -> PolicyDecision:
    """Convenience reducer API compatible with the prototype call shape."""

    if isinstance(snapshot_or_history, (tuple, list)):
        if policy is None:
            raise ConvergenceError("policy is required when deciding from history")
        snapshot = build_snapshot(snapshot_or_history, policy)
        if usage is not None:
            derived = dict(snapshot.derived)
            normalized_usage: dict[str, object] = {}
            for metric in METRICS:
                raw_value = usage.get(metric)
                if isinstance(raw_value, Mapping):
                    normalized_usage[metric] = dict(raw_value)
                elif raw_value is None:
                    normalized_usage[metric] = {
                        "value": None,
                        "availability": "unavailable",
                        "reason_code": "not_reported",
                    }
                elif isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool) and math.isfinite(float(raw_value)) and raw_value >= 0:
                    normalized_usage[metric] = {
                        "value": raw_value,
                        "availability": "available",
                    }
                else:
                    normalized_usage[metric] = {
                        "value": None,
                        "availability": "invalid",
                        "reason_code": "invalid_legacy_usage",
                    }
            derived["usage"] = normalized_usage
            snapshot = ValidatedHistorySnapshot(
                run_id=snapshot.run_id,
                work_item_id=snapshot.work_item_id,
                target=snapshot.target,
                policy=snapshot.policy,
                attempts=snapshot.attempts,
                reserved_attempt_count=snapshot.reserved_attempt_count,
                derived=derived,
                validation_errors=snapshot.validation_errors,
                unresolved_started=snapshot.unresolved_started,
                human_approval_required=snapshot.human_approval_required,
            )
    else:
        snapshot = snapshot_or_history
    return PolicyReducer().decide(
        snapshot,
        proposal,
        signals,
        human_approval_required=human_approval_required,
    )


def _safe_relative_path(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConvergenceError(f"{field_name} must be a non-empty relative path")
    if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
        raise ConvergenceError(f"invalid {field_name}: {value}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ConvergenceError(f"invalid {field_name}: {value}")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ConvergenceError(f"invalid {field_name}: {value}")
    return "/".join(parts)


def _assert_safe_path(root: Path, path: Path) -> None:
    if root.is_symlink():
        raise ConvergenceError(f"artifact root must not be a symlink: {root}")
    root_canonical = root.resolve(strict=False)
    try:
        path.resolve(strict=False).relative_to(root_canonical)
    except ValueError as exc:
        raise ConvergenceError(f"artifact path escapes root: {path}") from exc
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ConvergenceError(f"artifact path is not below root: {path}") from exc
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ConvergenceError(f"artifact path contains a symlink: {path}")


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    if path.is_symlink():
        raise ConvergenceError(f"artifact destination must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            try:
                descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _exclusive_write_bytes(path: Path, value: bytes) -> None:
    if path.is_symlink():
        raise ConvergenceError(f"artifact destination must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


def _read_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise HistoryCorruptionError(f"required JSON artifact is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryCorruptionError(f"invalid JSON artifact: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HistoryCorruptionError(f"JSON artifact must contain an object: {path}")
    if _json_depth(value) > MAX_JSON_DEPTH:
        raise HistoryCorruptionError(f"JSON artifact is too deeply nested: {path}")
    return value


@dataclass(frozen=True)
class AttemptReservation:
    run_id: str
    attempt_number: int
    attempt_key: str
    attempt_dir: Path
    decision: PolicyDecision
    parent_attempt_digest: str | None
    reservation_sha256: str

    @property
    def path(self) -> Path:
        return self.attempt_dir


class ConvergenceStore:
    """Symlink-safe durable store for one convergence run namespace."""

    def __init__(self, artifact_root: Path, work_item_id: str, run_id: str | None = None) -> None:
        self.artifact_root = Path(artifact_root)
        if self.artifact_root.is_symlink():
            raise ConvergenceError("artifact root must not be a symlink")
        self.artifact_root = self.artifact_root.resolve(strict=False)
        self.work_item_id = _validate_id(work_item_id, "work_item_id")
        if run_id is not None:
            self.run_id = _validate_id(run_id, "run_id", pattern=RUN_ID_PATTERN)
        else:
            self.run_id = None
        self.work_item_root = self.artifact_root / "work-items" / self.work_item_id
        self.runs_root = self.work_item_root / "convergence-runs"

    def run_path(self, run_id: str | None = None) -> Path:
        actual = run_id or self.run_id
        if actual is None:
            raise ConvergenceError("run_id is required")
        _validate_id(actual, "run_id", pattern=RUN_ID_PATTERN)
        path = self.runs_root / actual
        _assert_safe_path(self.artifact_root, path)
        return path

    @contextmanager
    def _lock(self) -> Iterable[None]:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        if self.work_item_root.is_symlink() or self.runs_root.is_symlink():
            raise ConvergenceError("convergence namespace must not contain symlinks")
        self.work_item_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(self.artifact_root, self.runs_root)
        lock_path = self.work_item_root / ".convergence-lock"
        if lock_path.is_symlink():
            raise ConvergenceError("convergence lock must not be a symlink")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError as exc:
            # Stale locks are intentionally not removed automatically.  A
            # caller must resolve them with operator provenance.
            raise RuntimeError(f"convergence work item is already locked: {self.work_item_id}") from exc
        try:
            os.write(descriptor, f"work_item={self.work_item_id}\npid={os.getpid()}\n".encode())
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if lock_path.is_file() and not lock_path.is_symlink():
                lock_path.unlink()

    def _path(self, run_id: str, relative_path: str) -> Path:
        relative = _safe_relative_path(relative_path, "convergence artifact path")
        path = self.run_path(run_id).joinpath(*relative.split("/"))
        _assert_safe_path(self.artifact_root, path)
        return path

    def _write_json(
        self,
        run_id: str,
        relative_path: str,
        payload: Mapping[str, object],
        *,
        exclusive: bool = False,
    ) -> Path:
        path = self._path(run_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(self.artifact_root, path.parent)
        if exclusive:
            _exclusive_write_bytes(path, _json_bytes(payload))
        else:
            _atomic_write_bytes(path, _json_bytes(payload))
        return path

    def _write_bytes(self, run_id: str, relative_path: str, value: bytes, *, exclusive: bool = False) -> Path:
        path = self._path(run_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(self.artifact_root, path.parent)
        if exclusive:
            _exclusive_write_bytes(path, value)
        else:
            _atomic_write_bytes(path, value)
        return path

    def _read_json(self, run_id: str, relative_path: str) -> dict[str, object]:
        path = self._path(run_id, relative_path)
        return _read_json(path)

    def artifact_ref(self, path: Path) -> str:
        _assert_safe_path(self.artifact_root, path)
        return path.relative_to(self.artifact_root).as_posix()

    def create_run(self, request: ConvergenceRequest | Mapping[str, object]) -> Path:
        request = ConvergenceRequestValidator().validate(request)
        if request.work_item_id != self.work_item_id:
            raise ConvergenceError("request work_item_id does not match store")
        run_id = request.run_id
        with self._lock():
            path = self.run_path(run_id)
            if path.exists() or path.is_symlink():
                raise ConvergenceError(f"convergence run already exists: {run_id}")
            path.mkdir(parents=True, exist_ok=False)
            (path / "attempts").mkdir()
            manifest_base = request.to_payload()
            manifest_base["created_at"] = _utc_now()
            manifest_base["run_id"] = run_id
            manifest_base["manifest_sha256"] = canonical_sha256(manifest_base)
            self._write_json(run_id, "manifest.json", manifest_base, exclusive=True)
            self._write_json(run_id, "policy.json", request.policy.to_payload(), exclusive=True)
            lifecycle = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "work_item_id": self.work_item_id,
                "state": "active",
                "reserved_attempt_count": 0,
                "history": [{"state": "active", "at": _utc_now()}],
            }
            self._write_json(run_id, "lifecycle.json", lifecycle, exclusive=True)
        self.run_id = run_id
        return path

    # Common aliases make the store usable from small callers without
    # exposing the internal filename protocol.
    reserve_run = create_run

    def read_manifest(self, run_id: str | None = None) -> dict[str, object]:
        return self._read_json(run_id or self.run_id or "", "manifest.json")

    def read_policy(self, run_id: str | None = None) -> ConvergencePolicy:
        payload = self._read_json(run_id or self.run_id or "", "policy.json")
        return ConvergencePolicy.from_mapping(payload).validate()

    def read_run_lifecycle(self, run_id: str | None = None) -> dict[str, object]:
        actual = run_id or self.run_id or ""
        payload = self._read_json(actual, "lifecycle.json")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise HistoryCorruptionError("unsupported convergence lifecycle schema")
        if payload.get("run_id") != actual or payload.get("work_item_id") != self.work_item_id:
            raise HistoryCorruptionError("convergence lifecycle binding mismatch")
        return payload

    def _attempt_dirs(self, run_id: str) -> list[Path]:
        attempts_root = self._path(run_id, "attempts")
        if attempts_root.is_symlink() or not attempts_root.is_dir():
            raise HistoryCorruptionError("convergence attempts namespace is invalid")
        paths: list[Path] = []
        for path in attempts_root.iterdir():
            if path.is_symlink():
                raise HistoryCorruptionError("convergence attempt path is a symlink")
            if path.is_dir() and re.fullmatch(r"[0-9]{4}", path.name):
                paths.append(path)
            elif path.name.startswith("."):
                continue
            else:
                raise HistoryCorruptionError(f"unsupported convergence attempt entry: {path.name}")
        return sorted(paths, key=lambda item: int(item.name))

    def _next_attempt_number(self, run_id: str) -> int:
        paths = self._attempt_dirs(run_id)
        expected = 1
        for path in paths:
            if int(path.name) != expected:
                raise HistoryCorruptionError("convergence attempt numbers are not contiguous")
            expected += 1
        return expected

    def _attempt_relative(self, number: int, filename: str) -> str:
        if number < 1 or number > 9999:
            raise ConvergenceError("attempt number is outside the finite namespace")
        _safe_relative_path(filename, "attempt filename")
        return f"attempts/{number:04d}/{filename}"

    def _load_attempt_record(self, run_id: str, path: Path) -> dict[str, object]:
        number = int(path.name)
        relative = f"attempts/{path.name}"
        reservation = _read_json(path / "reservation.json")
        lifecycle = _read_json(path / "lifecycle.json")
        if reservation.get("schema_version") != SCHEMA_VERSION:
            raise HistoryCorruptionError(f"unsupported reservation schema at {relative}")
        if lifecycle.get("attempt_number") != number or reservation.get("attempt_number") != number:
            raise HistoryCorruptionError(f"attempt number mismatch at {relative}")
        if reservation.get("run_id") != run_id or lifecycle.get("run_id") != run_id:
            raise HistoryCorruptionError(f"attempt run binding mismatch at {relative}")
        if (
            reservation.get("work_item_id") != self.work_item_id
            or lifecycle.get("work_item_id") != self.work_item_id
        ):
            raise HistoryCorruptionError(f"attempt work item binding mismatch at {relative}")
        expected_key = canonical_sha256({
            "run_id": run_id,
            "attempt_number": number,
            "decision_sha256": reservation.get("decision_sha256"),
        })
        if reservation.get("attempt_key") != expected_key:
            raise HistoryCorruptionError(f"attempt key mismatch at {relative}")
        if lifecycle.get("attempt_key") != expected_key:
            raise HistoryCorruptionError(f"attempt lifecycle key mismatch at {relative}")
        reservation_without_digest = dict(reservation)
        claimed_reservation_digest = reservation_without_digest.pop("reservation_sha256", None)
        if claimed_reservation_digest != canonical_sha256(reservation_without_digest):
            raise HistoryCorruptionError(f"reservation digest mismatch at {relative}")
        state = lifecycle.get("state")
        if state not in {"reserved", "started", "result_bound", "terminal_unknown"}:
            raise HistoryCorruptionError(f"unsupported attempt lifecycle state: {state}")
        lifecycle_history = lifecycle.get("history")
        if not isinstance(lifecycle_history, list) or not lifecycle_history:
            raise HistoryCorruptionError(f"attempt lifecycle history is invalid at {relative}")
        observed_states = [
            item.get("state") if isinstance(item, Mapping) else None
            for item in lifecycle_history
        ]
        valid_histories = {
            "reserved": ["reserved"],
            "started": ["reserved", "started"],
            "result_bound": ["reserved", "started", "result_bound"],
            "terminal_unknown": ["reserved", "terminal_unknown"],
        }
        if state == "terminal_unknown" and observed_states == ["reserved", "started", "terminal_unknown"]:
            pass
        elif observed_states != valid_histories[state]:
            raise HistoryCorruptionError(f"attempt lifecycle transition history is invalid at {relative}")
        decision = _read_json(path / "decision.json")
        if decision.get("decision_sha256") != reservation.get("decision_sha256"):
            raise HistoryCorruptionError(f"decision binding mismatch at {relative}")
        decision_without_digest = dict(decision)
        claimed_decision_digest = decision_without_digest.pop("decision_sha256", None)
        if claimed_decision_digest != canonical_sha256(decision_without_digest):
            raise HistoryCorruptionError(f"decision digest mismatch at {relative}")
        record: dict[str, object] = {
            "attempt_number": number,
            "attempt_key": reservation["attempt_key"],
            "attempt_digest": reservation.get("reservation_sha256"),
            "lifecycle_state": state,
            "reservation": reservation,
            "retry_instruction": reservation.get("retry_instruction"),
            "decision": decision,
            "decision_ref": self.artifact_ref(path / "decision.json"),
            "reservation_ref": self.artifact_ref(path / "reservation.json"),
        }
        if state == "result_bound":
            binding = _read_json(path / "result-binding.json")
            result = self._validate_result_binding(run_id, number, binding)
            record["result_binding"] = binding
            record["result"] = result
            record["result_ref"] = binding.get("result_ref")
            record["result_sha256"] = binding.get("result_sha256")
            if (path / "usage.json").exists():
                record["usage"] = _read_json(path / "usage.json").get("samples", [])
            if (path / "progress.json").exists():
                record["progress"] = _read_json(path / "progress.json")
        elif state in {"started", "terminal_unknown"}:
            record["unresolved_started"] = True
        return record

    def _validate_result_binding(
        self,
        run_id: str,
        attempt_number: int,
        binding: Mapping[str, object],
    ) -> dict[str, object]:
        if binding.get("schema_version") != SCHEMA_VERSION:
            raise HistoryCorruptionError("unsupported result binding schema")
        if binding.get("run_id") != run_id or binding.get("attempt_number") != attempt_number:
            raise HistoryCorruptionError("evaluation result binding identity mismatch")
        result: dict[str, object]
        result_ref = binding.get("result_ref")
        finalized_ref = binding.get("finalized_ref")
        if isinstance(binding.get("inline_result"), Mapping):
            result = dict(binding["inline_result"])
            if binding.get("result_sha256") != canonical_sha256(result):
                raise HistoryCorruptionError("inline evaluation result digest mismatch")
        elif isinstance(result_ref, str) and isinstance(finalized_ref, str):
            result_path = self.artifact_root / result_ref
            marker_path = self.artifact_root / finalized_ref
            _assert_safe_path(self.artifact_root, result_path)
            _assert_safe_path(self.artifact_root, marker_path)
            result = _read_json(result_path)
            marker = _read_json(marker_path)
            actual_result_sha = sha256_file(result_path)
            if binding.get("result_sha256") != actual_result_sha or marker.get("result_sha256") != actual_result_sha:
                raise HistoryCorruptionError("evaluation result digest mismatch")
            if binding.get("finalized_sha256") is not None and binding.get("finalized_sha256") != sha256_file(marker_path):
                raise HistoryCorruptionError("evaluation finalized marker digest mismatch")
            marker_summary = marker.get("summary_sha256")
            if not isinstance(marker_summary, str) or not SHA256_PATTERN.fullmatch(marker_summary):
                raise HistoryCorruptionError("evaluation finalized marker is invalid")
        else:
            raise HistoryCorruptionError("result binding requires an inline result or finalized refs")
        manifest = self.read_manifest(run_id)
        if result.get("schema_version") != SCHEMA_VERSION:
            raise HistoryCorruptionError("unsupported evaluation result schema")
        if result.get("work_item_id") != self.work_item_id or result.get("work_item_id") != manifest.get("work_item_id"):
            raise HistoryCorruptionError("evaluation result work item binding mismatch")
        expected_target = manifest.get("target_sha256")
        if result.get("target_sha256") != expected_target:
            raise HistoryCorruptionError("evaluation result target binding mismatch")
        if isinstance(result.get("target"), Mapping) and target_sha256(result.get("target")) != expected_target:
            raise HistoryCorruptionError("evaluation result target payload mismatch")
        if result.get("verdict") not in VERDICTS:
            raise HistoryCorruptionError("evaluation result verdict is unsupported")
        return result

    def load_snapshot(self, run_id: str | None = None) -> ValidatedHistorySnapshot:
        actual = run_id or self.run_id
        if actual is None:
            raise ConvergenceError("run_id is required")
        manifest = self.read_manifest(actual)
        policy = self.read_policy(actual)
        if manifest.get("manifest_sha256") != canonical_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"}):
            raise HistoryCorruptionError("convergence manifest digest mismatch")
        if manifest.get("policy_sha256") != policy.policy_sha256:
            raise HistoryCorruptionError("convergence policy snapshot digest mismatch")
        target = manifest.get("target")
        if target_sha256(target) != manifest.get("target_sha256"):
            raise HistoryCorruptionError("convergence target digest mismatch")
        lifecycle = self.read_run_lifecycle(actual)
        paths = self._attempt_dirs(actual)
        attempts: list[dict[str, object]] = []
        previous_digest: str | None = None
        unresolved_started = False
        bound_results: set[str] = set()
        for expected, path in enumerate(paths, start=1):
            if int(path.name) != expected:
                raise HistoryCorruptionError("convergence attempt numbers are not contiguous")
            record = self._load_attempt_record(actual, path)
            parent = record["reservation"].get("parent_attempt_digest")
            if parent != previous_digest:
                raise HistoryCorruptionError("convergence parent digest chain mismatch")
            previous_digest = record.get("attempt_digest")
            unresolved_started = unresolved_started or bool(record.get("unresolved_started"))
            if record.get("lifecycle_state") == "result_bound":
                binding = record.get("result_binding", {})
                if isinstance(binding, Mapping):
                    # Inline fake results are snapshots of callback output and
                    # do not identify a durable external result artifact.  A
                    # duplicate external ``result_ref`` is the corruption
                    # case that must be rejected.
                    binding_identity = binding.get("result_ref")
                    if isinstance(binding_identity, str):
                        if binding_identity in bound_results:
                            raise HistoryCorruptionError("evaluation result is bound more than once")
                        bound_results.add(binding_identity)
            attempts.append(record)
        reserved_count = lifecycle.get("reserved_attempt_count")
        if isinstance(reserved_count, bool) or not isinstance(reserved_count, int) or reserved_count < 0:
            raise HistoryCorruptionError("reserved attempt count is invalid")
        if reserved_count != len(paths):
            raise HistoryCorruptionError("reserved attempt count does not match committed reservations")
        derived = derive_history(attempts, policy)
        return ValidatedHistorySnapshot(
            run_id=actual,
            work_item_id=self.work_item_id,
            target=target,
            policy=policy,
            attempts=tuple(attempts),
            reserved_attempt_count=reserved_count,
            derived=derived,
            validation_errors=(),
            unresolved_started=unresolved_started,
            target_sha256=manifest.get("target_sha256"),
            policy_sha256=manifest.get("policy_sha256"),
        )

    def _load_decision(self, decision: PolicyDecision | Mapping[str, object]) -> PolicyDecision:
        parsed = PolicyDecision.from_value(decision)
        if parsed.action != "retry" or parsed.retry_instruction is None:
            raise ConvergenceError("only retry decisions can reserve an attempt")
        if parsed.retry_instruction.verdict not in RETRY_VERDICTS:
            raise ConvergenceError("retry decision verdict is unsupported")
        return parsed

    def reserve_attempt(
        self,
        decision: PolicyDecision | Mapping[str, object],
        *,
        run_id: str | None = None,
    ) -> AttemptReservation:
        actual = run_id or self.run_id
        if actual is None:
            raise ConvergenceError("run_id is required")
        parsed = self._load_decision(decision)
        with self._lock():
            lifecycle = self.read_run_lifecycle(actual)
            if lifecycle.get("state") != "active":
                raise ConvergenceError("cannot reserve an attempt on a terminal run")
            policy = self.read_policy(actual)
            number = self._next_attempt_number(actual)
            if number > int(policy.max_iterations):
                raise ConvergenceError("max_iterations reached before reservation")
            previous_digest: str | None = None
            paths = self._attempt_dirs(actual)
            if paths:
                previous = _read_json(paths[-1] / "reservation.json")
                previous_digest = previous.get("reservation_sha256")
            decision_payload = parsed.to_payload()
            decision_digest = parsed.decision_sha256
            instruction = parsed.retry_instruction.normalized()
            attempt_key = canonical_sha256({
                "run_id": actual,
                "attempt_number": number,
                "decision_sha256": decision_digest,
            })
            if instruction.attempt_key is not None and instruction.attempt_key != attempt_key:
                raise ConvergenceError("retry instruction attempt_key does not match reservation")
            instruction = instruction.normalized(attempt_key=attempt_key)
            attempt_dir = self._path(actual, f"attempts/{number:04d}")
            attempt_dir.mkdir(parents=False, exist_ok=False)
            # decision is committed before the reservation is exposed to an
            # executor.  The reservation itself then becomes the iteration
            # authority for finite accounting.
            self._write_json(actual, f"attempts/{number:04d}/decision.json", decision_payload, exclusive=True)
            reservation_base: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "run_id": actual,
                "work_item_id": self.work_item_id,
                "attempt_number": number,
                "attempt_key": attempt_key,
                "decision_sha256": decision_digest,
                "parent_attempt_digest": previous_digest,
                "retry_instruction": instruction.to_payload(),
                "reserved_at": _utc_now(),
            }
            reservation_base["reservation_sha256"] = canonical_sha256(reservation_base)
            self._write_json(actual, f"attempts/{number:04d}/reservation.json", reservation_base, exclusive=True)
            attempt_lifecycle = {
                "schema_version": SCHEMA_VERSION,
                "run_id": actual,
                "work_item_id": self.work_item_id,
                "attempt_number": number,
                "attempt_key": attempt_key,
                "state": "reserved",
                "history": [{"state": "reserved", "at": _utc_now()}],
            }
            self._write_json(actual, f"attempts/{number:04d}/lifecycle.json", attempt_lifecycle, exclusive=True)
            updated = dict(lifecycle)
            updated["reserved_attempt_count"] = number
            history = lifecycle.get("history", [])
            if not isinstance(history, list):
                raise HistoryCorruptionError("run lifecycle history is not a list")
            updated["history"] = [*history, {"state": "attempt_reserved", "attempt_number": number, "at": _utc_now()}]
            self._write_json(actual, "lifecycle.json", updated)
            return AttemptReservation(
                run_id=actual,
                attempt_number=number,
                attempt_key=attempt_key,
                attempt_dir=attempt_dir,
                decision=parsed,
                parent_attempt_digest=previous_digest,
                reservation_sha256=reservation_base["reservation_sha256"],
            )

    # Prototype-friendly alias.
    reserve = reserve_attempt

    def find_unfinished_attempt(self, run_id: str | None = None) -> dict[str, object] | None:
        actual = run_id or self.run_id
        if actual is None:
            raise ConvergenceError("run_id is required")
        for path in reversed(self._attempt_dirs(actual)):
            lifecycle = _read_json(path / "lifecycle.json")
            if lifecycle.get("state") in {"reserved", "started"}:
                return self._load_attempt_record(actual, path)
        return None

    def _transition_attempt(self, run_id: str, number: int, state: str, *, reason_code: str | None = None) -> dict[str, object]:
        if state not in {"started", "result_bound", "terminal_unknown"}:
            raise ConvergenceError(f"unsupported attempt transition: {state}")
        relative = f"attempts/{number:04d}/lifecycle.json"
        current = self._read_json(run_id, relative)
        old = current.get("state")
        allowed = {
            "reserved": {"started", "terminal_unknown"},
            "started": {"result_bound", "terminal_unknown"},
            "result_bound": set(),
            "terminal_unknown": set(),
        }
        if old not in allowed or state not in allowed[old]:
            raise ConvergenceError(f"invalid convergence attempt transition: {old} -> {state}")
        updated = dict(current)
        updated["state"] = state
        history = current.get("history", [])
        if not isinstance(history, list):
            raise HistoryCorruptionError("attempt lifecycle history is not a list")
        entry: dict[str, object] = {"state": state, "at": _utc_now()}
        if reason_code is not None:
            entry["reason_code"] = reason_code
            updated["reason_code"] = reason_code
        updated["history"] = [*history, entry]
        self._write_json(run_id, relative, updated)
        return updated

    def start_attempt(self, reservation: AttemptReservation | Mapping[str, object], *, run_id: str | None = None) -> bool:
        actual = run_id or self.run_id
        if actual is None:
            raise ConvergenceError("run_id is required")
        number = reservation.attempt_number if isinstance(reservation, AttemptReservation) else reservation.get("attempt_number")
        key = reservation.attempt_key if isinstance(reservation, AttemptReservation) else reservation.get("attempt_key")
        if not isinstance(number, int) or not isinstance(key, str):
            raise ConvergenceError("invalid attempt reservation")
        with self._lock():
            current = self._read_json(actual, f"attempts/{number:04d}/lifecycle.json")
            if current.get("attempt_key") != key:
                raise HistoryCorruptionError("attempt start key mismatch")
            if current.get("state") == "started":
                # A started attempt is deliberately not re-executed.  The
                # caller must route it to human review as unknown outcome.
                return False
            if current.get("state") != "reserved":
                raise ConvergenceError("attempt is not resumable from reserved state")
            self._transition_attempt(actual, number, "started")
            return True

    def mark_terminal_unknown(self, number: int, *, run_id: str | None = None, reason_code: str = "attempt_outcome_unknown") -> None:
        actual = run_id or self.run_id
        if actual is None:
            raise ConvergenceError("run_id is required")
        with self._lock():
            current = self._read_json(actual, f"attempts/{number:04d}/lifecycle.json")
            if current.get("state") == "terminal_unknown":
                return
            self._transition_attempt(actual, number, "terminal_unknown", reason_code=reason_code)

    def _binding_for_result(
        self,
        run_id: str,
        number: int,
        result: object,
    ) -> tuple[dict[str, object], dict[str, object]]:
        # Import lazily to keep this module usable as a standalone pure policy
        # module and to avoid imposing the one-shot evaluator at import time.
        loop_dir: Path | None = None
        result_payload: dict[str, object] | None = None
        if isinstance(result, Mapping):
            result_payload = dict(result)
        else:
            candidate = getattr(result, "result", None)
            if isinstance(candidate, Mapping):
                result_payload = dict(candidate)
            candidate_dir = getattr(result, "loop_dir", None)
            if candidate_dir is not None:
                loop_dir = Path(candidate_dir)
        if result_payload is None and loop_dir is None:
            raise ConvergenceError("evaluator result must be a result object, mapping, or loop result")
        if loop_dir is not None:
            result_path = loop_dir / "result.json"
            finalized_path = loop_dir / "finalized"
            if result_path.is_symlink() or finalized_path.is_symlink() or not result_path.is_file() or not finalized_path.is_file():
                raise HistoryCorruptionError("evaluator result is not finalized")
            # Store relative references under the common artifact root.  The
            # one-shot evaluator is expected to share this artifact namespace.
            result_ref = self.artifact_ref(result_path)
            finalized_ref = self.artifact_ref(finalized_path)
            actual_result_sha = sha256_file(result_path)
            result_payload = _read_json(result_path)
            marker = _read_json(finalized_path)
            if marker.get("result_sha256") != actual_result_sha:
                raise HistoryCorruptionError("evaluator finalized marker does not bind result")
            binding = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "attempt_number": number,
                "result_ref": result_ref,
                "finalized_ref": finalized_ref,
                "result_sha256": actual_result_sha,
                "finalized_sha256": sha256_file(finalized_path),
            }
        else:
            result_sha = canonical_sha256(result_payload)
            binding = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "attempt_number": number,
                "inline_result": result_payload,
                "result_sha256": result_sha,
            }
        return binding, result_payload

    def bind_result(
        self,
        reservation: AttemptReservation | Mapping[str, object],
        result: object,
        *,
        run_id: str | None = None,
        usage: Sequence[UsageSample | Mapping[str, object]] = (),
    ) -> dict[str, object]:
        actual = run_id or self.run_id
        if actual is None:
            raise ConvergenceError("run_id is required")
        number = reservation.attempt_number if isinstance(reservation, AttemptReservation) else reservation.get("attempt_number")
        key = reservation.attempt_key if isinstance(reservation, AttemptReservation) else reservation.get("attempt_key")
        if not isinstance(number, int) or not isinstance(key, str):
            raise ConvergenceError("invalid attempt reservation")
        with self._lock():
            lifecycle = self._read_json(actual, f"attempts/{number:04d}/lifecycle.json")
            if lifecycle.get("attempt_key") != key:
                raise HistoryCorruptionError("result binding attempt key mismatch")
            if lifecycle.get("state") == "result_bound":
                existing = self._read_json(actual, f"attempts/{number:04d}/result-binding.json")
                return existing
            if lifecycle.get("state") != "started":
                raise ConvergenceError("only a started attempt may bind a result")
            binding, result_payload = self._binding_for_result(actual, number, result)
            manifest = self.read_manifest(actual)
            if result_payload.get("schema_version") != SCHEMA_VERSION:
                raise HistoryCorruptionError("evaluator result schema is unsupported")
            if result_payload.get("work_item_id") != self.work_item_id:
                raise HistoryCorruptionError("evaluator result work item does not match convergence run")
            if result_payload.get("target_sha256") != manifest.get("target_sha256"):
                raise HistoryCorruptionError("evaluator result target does not match convergence run")
            if result_payload.get("verdict") not in VERDICTS:
                raise HistoryCorruptionError("evaluator result verdict is unsupported")
            self._write_json(actual, f"attempts/{number:04d}/result-binding.json", binding, exclusive=True)
            if usage:
                samples = [UsageSample.from_value(item) for item in usage]
                self._write_json(actual, f"attempts/{number:04d}/usage.json", {
                    "schema_version": SCHEMA_VERSION,
                    "samples": [item.to_payload() for item in samples],
                }, exclusive=True)
            self._transition_attempt(actual, number, "result_bound")
            return binding

    def recover_started_attempt(
        self,
        record: Mapping[str, object],
        *,
        run_id: str | None = None,
    ) -> bool:
        """Bind one uniquely discoverable finalized one-shot result.

        Discovery is intentionally conservative.  A result that carries the
        same attempt key is preferred; otherwise a single matching finalized
        result is sufficient.  Zero or multiple candidates remain unknown and
        are handed to a human instead of being guessed.
        """

        actual = run_id or self.run_id
        if actual is None:
            raise ConvergenceError("run_id is required")
        number = record.get("attempt_number")
        attempt_key = record.get("attempt_key")
        if not isinstance(number, int) or not isinstance(attempt_key, str):
            raise HistoryCorruptionError("started attempt identity is invalid")
        convergence_manifest = self.read_manifest(actual)
        expected_target_sha = convergence_manifest.get("target_sha256")
        evaluation_root = self.work_item_root / "evaluation-loops"
        if evaluation_root.is_symlink() or not evaluation_root.is_dir():
            return False
        candidates: list[tuple[Path, dict[str, object]]] = []
        for loop_dir in sorted(evaluation_root.iterdir()):
            if loop_dir.is_symlink() or not loop_dir.is_dir():
                continue
            result_path = loop_dir / "result.json"
            marker_path = loop_dir / "finalized"
            if result_path.is_symlink() or marker_path.is_symlink() or not result_path.is_file() or not marker_path.is_file():
                continue
            try:
                result = _read_json(result_path)
                marker = _read_json(marker_path)
                result_digest = sha256_file(result_path)
                if marker.get("result_sha256") != result_digest:
                    continue
                if result.get("schema_version") != SCHEMA_VERSION:
                    continue
                if result.get("work_item_id") != self.work_item_id:
                    continue
                if result.get("target_sha256") != expected_target_sha:
                    continue
                manifest = _read_json(loop_dir / "manifest.json") if (loop_dir / "manifest.json").is_file() else {}
                if manifest.get("attempt_key") is not None and manifest.get("attempt_key") != attempt_key:
                    continue
                if result.get("attempt_key") is not None and result.get("attempt_key") != attempt_key:
                    continue
            except (ConvergenceError, HistoryCorruptionError, OSError, json.JSONDecodeError):
                continue
            candidates.append((loop_dir, result))
        if len(candidates) != 1:
            return False
        loop_dir, result = candidates[0]
        self.bind_result(
            {"attempt_number": number, "attempt_key": attempt_key},
            SimpleNamespace(result=result, loop_dir=loop_dir),
            run_id=actual,
        )
        return True

    def write_progress(self, number: int, progress: ProgressEvidence | Mapping[str, object], *, run_id: str | None = None) -> None:
        actual = run_id or self.run_id
        if actual is None:
            raise ConvergenceError("run_id is required")
        payload = progress.to_payload() if isinstance(progress, ProgressEvidence) else dict(progress)
        payload.setdefault("schema_version", SCHEMA_VERSION)
        with self._lock():
            self._write_json(actual, f"attempts/{number:04d}/progress.json", payload, exclusive=True)

    def load_history(self, run_id: str | None = None) -> ValidatedHistorySnapshot:
        return self.load_snapshot(run_id)


class HistoryLoader:
    """Small explicit boundary for validated convergence snapshots."""

    def __init__(self, store: ConvergenceStore) -> None:
        self.store = store

    def load(self, run_id: str | None = None) -> ValidatedHistorySnapshot:
        return self.store.load_snapshot(run_id)


def _last_retry(snapshot: ValidatedHistorySnapshot) -> dict[str, object] | None:
    for attempt in reversed(snapshot.attempts):
        instruction = attempt.get("retry_instruction")
        if isinstance(instruction, Mapping):
            return dict(instruction)
    return None


def build_terminal_summary(
    snapshot: ValidatedHistorySnapshot,
    decision: PolicyDecision | Mapping[str, object],
) -> dict[str, object]:
    parsed = PolicyDecision.from_value(decision)
    reservations: list[dict[str, object]] = []
    result_refs: list[dict[str, object]] = []
    for attempt in snapshot.attempts:
        reservation = attempt.get("reservation", {})
        if not isinstance(reservation, Mapping):
            reservation = {}
        record: dict[str, object] = {
            "attempt_number": attempt.get("attempt_number"),
            "attempt_key": attempt.get("attempt_key"),
            "attempt_digest": attempt.get("attempt_digest"),
            "state": attempt.get("lifecycle_state", attempt.get("state")),
            "reservation_ref": attempt.get("reservation_ref"),
            "decision_ref": attempt.get("decision_ref"),
        }
        if reservation.get("retry_instruction") is not None:
            record["retry_instruction"] = reservation.get("retry_instruction")
        reservations.append(record)
        if attempt.get("result_ref") or attempt.get("result_sha256"):
            result_refs.append({
                "attempt_number": attempt.get("attempt_number"),
                "result_ref": attempt.get("result_ref"),
                "result_sha256": attempt.get("result_sha256"),
                "binding": attempt.get("result_binding"),
            })
    usage = snapshot.derived.get("usage", {})
    budgets: dict[str, object] = {}
    if isinstance(usage, Mapping):
        for metric, limit in snapshot.policy.budget_limits().items():
            observed = usage.get(metric, {})
            budgets[metric] = {
                "limit": limit.limit,
                "unit": limit.unit,
                "currency": limit.currency,
                **(dict(observed) if isinstance(observed, Mapping) else {
                    "value": None,
                    "availability": "invalid",
                }),
            }
    unresolved = [
        int(item.get("attempt_number"))
        for item in snapshot.attempts
        if item.get("lifecycle_state") in {"reserved", "started", "terminal_unknown"}
        and isinstance(item.get("attempt_number"), int)
    ]
    current_findings = [_finding_id(item) for item in snapshot.current_open_findings]
    summary: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": snapshot.run_id,
        "work_item_id": snapshot.work_item_id,
        "target": dict(snapshot.target),
        "target_sha256": snapshot.target_sha256,
        "terminal_state": parsed.terminal_state,
        "action": parsed.action,
        "termination_reason": parsed.reason_code,
        "failure_class": parsed.failure_class,
        "acceptance_outcome": parsed.acceptance_outcome,
        "compliance_status": parsed.compliance_status,
        "policy_sha256": snapshot.policy_sha256,
        "policy": snapshot.policy.to_payload(),
        "usage": {
            "iterations": snapshot.reserved_attempt_count,
            "budgets": budgets,
            "derived": snapshot.derived.get("usage", {}),
        },
        "reservations": reservations,
        "result_refs": result_refs,
        "history_refs": [
            item.get("result_ref") or item.get("reservation_ref")
            for item in snapshot.attempts
            if item.get("result_ref") or item.get("reservation_ref")
        ],
        "remaining_open_findings": current_findings,
        "remaining_findings": current_findings,
        "last_committed_retry": _last_retry(snapshot),
        "unresolved_reservations": unresolved,
        "termination_evidence": list(parsed.evidence_refs),
        "decision_sha256": parsed.decision_sha256,
        "snapshot_sha256": snapshot.snapshot_sha256,
    }
    last_retry = summary["last_committed_retry"]
    if isinstance(last_retry, Mapping):
        summary["last_retry_reason"] = last_retry.get("retry_reason")
        summary["next_change_intent_digest"] = digest_intent(str(last_retry.get("change_intent", "")))
    else:
        summary["last_retry_reason"] = None
        summary["next_change_intent_digest"] = None
    summary["summary_payload_sha256"] = canonical_sha256(summary)
    return summary


def summary(
    history: Sequence[Mapping[str, object]],
    decision: PolicyDecision | Mapping[str, object],
    usage: Mapping[str, object],
) -> dict[str, object]:
    """Compatibility projection for the disposable prototype's summary API."""

    snapshot = build_snapshot(
        history,
        ConvergencePolicy(
            max_iterations=max(1, len(history) + 1),
            unknown_budget_policy="continue",
            partial_budget_policy="continue",
        ),
    )
    normalized = dict(snapshot.derived)
    normalized["usage"] = {
        metric: (
            dict(value)
            if isinstance(value, Mapping)
            else {
                "value": value,
                "availability": "available" if value is not None else "unavailable",
            }
        )
        for metric, value in usage.items()
        if metric in METRICS
    }
    snapshot = ValidatedHistorySnapshot(
        run_id=snapshot.run_id,
        work_item_id=snapshot.work_item_id,
        target=snapshot.target,
        policy=snapshot.policy,
        attempts=snapshot.attempts,
        reserved_attempt_count=len(history),
        derived=normalized,
        target_sha256=snapshot.target_sha256,
        policy_sha256=snapshot.policy_sha256,
    )
    return build_terminal_summary(snapshot, decision)


def render_convergence_summary(summary: Mapping[str, object]) -> str:
    """Render deterministic Markdown from the authoritative JSON payload."""

    def value(key: str) -> str:
        return json.dumps(summary.get(key), ensure_ascii=True, sort_keys=True)

    lines = [
        "# Convergence run summary",
        "",
        f"- Run: `{summary.get('run_id')}`",
        f"- Work item: `{summary.get('work_item_id')}`",
        f"- Terminal state: `{summary.get('terminal_state')}`",
        f"- Action: `{summary.get('action')}`",
        f"- Termination reason: `{summary.get('termination_reason')}`",
        f"- Acceptance outcome: `{summary.get('acceptance_outcome')}`",
        f"- Compliance status: `{summary.get('compliance_status')}`",
        f"- Remaining open findings: `{value('remaining_open_findings')}`",
        f"- Usage: `{value('usage')}`",
        "",
        "## Last committed retry",
        "",
        f"```json\n{json.dumps(summary.get('last_committed_retry'), ensure_ascii=True, indent=2, sort_keys=True)}\n```",
        "",
        "## Evidence",
        "",
        f"- Snapshot: `{summary.get('snapshot_sha256')}`",
        f"- Decision: `{summary.get('decision_sha256')}`",
        f"- Termination evidence: `{value('termination_evidence')}`",
    ]
    return "\n".join(lines) + "\n"


render_summary = render_convergence_summary


class SummaryBuilder:
    def build(self, snapshot: ValidatedHistorySnapshot, decision: PolicyDecision | Mapping[str, object]) -> dict[str, object]:
        return build_terminal_summary(snapshot, decision)

    def render(self, summary: Mapping[str, object]) -> str:
        return render_convergence_summary(summary)


class ConvergenceStoreFinalizer:
    """Write summary JSON/Markdown and the final marker in that order."""

    def __init__(self, store: ConvergenceStore) -> None:
        self.store = store

    def finalize(
        self,
        snapshot: ValidatedHistorySnapshot,
        decision: PolicyDecision | Mapping[str, object],
    ) -> dict[str, object]:
        parsed = PolicyDecision.from_value(decision)
        if parsed.terminal_state is None:
            raise ConvergenceError("only terminal decisions can be finalized")
        run_id = snapshot.run_id
        summary = build_terminal_summary(snapshot, parsed)
        summary_json = _json_bytes(summary)
        summary_md = render_convergence_summary(summary).encode("utf-8")
        json_path = self.store._path(run_id, "summary.json")
        md_path = self.store._path(run_id, "summary.md")
        marker_path = self.store._path(run_id, "finalized")
        with self.store._lock():
            if json_path.exists():
                if json_path.is_symlink() or sha256_file(json_path) != hashlib.sha256(summary_json).hexdigest():
                    raise HistoryCorruptionError("existing convergence summary.json has a different digest")
            else:
                self.store._write_bytes(run_id, "summary.json", summary_json, exclusive=True)
            if md_path.exists():
                if md_path.is_symlink() or sha256_file(md_path) != hashlib.sha256(summary_md).hexdigest():
                    raise HistoryCorruptionError("existing convergence summary.md has a different digest")
            else:
                self.store._write_bytes(run_id, "summary.md", summary_md, exclusive=True)
            marker = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "summary_sha256": sha256_file(json_path),
                "summary_markdown_sha256": sha256_file(md_path),
                "summary_payload_sha256": summary.get("summary_payload_sha256"),
            }
            if marker_path.exists():
                existing = _read_json(marker_path)
                if existing != marker:
                    raise HistoryCorruptionError("existing convergence final marker has a different digest")
            else:
                self.store._write_json(run_id, "finalized", marker, exclusive=True)
            lifecycle = self.store.read_run_lifecycle(run_id)
            updated = dict(lifecycle)
            updated["state"] = "terminal"
            updated["terminal_state"] = parsed.terminal_state
            updated["termination_reason"] = parsed.reason_code
            history = lifecycle.get("history", [])
            if not isinstance(history, list):
                raise HistoryCorruptionError("run lifecycle history is not a list")
            if not any(item.get("state") == "terminal" for item in history if isinstance(item, Mapping)):
                updated["history"] = [*history, {"state": "terminal", "at": _utc_now()}]
                self.store._write_json(run_id, "lifecycle.json", updated)
        return summary


def finalize_summary(
    store: ConvergenceStore,
    snapshot: ValidatedHistorySnapshot,
    decision: PolicyDecision | Mapping[str, object],
) -> dict[str, object]:
    return ConvergenceStoreFinalizer(store).finalize(snapshot, decision)


class EvaluationAdapter(Protocol):
    def __call__(self, instruction: RetryInstruction, request: ConvergenceRequest, attempt: AttemptReservation) -> object:
        ...


@dataclass(frozen=True)
class ConvergenceRunResult:
    run_id: str
    run_dir: Path
    decision: PolicyDecision
    summary: dict[str, object] | None
    error: str | None = None

    @property
    def action(self) -> str:
        return self.decision.action

    @property
    def terminal_state(self) -> str | None:
        return self.decision.terminal_state

    @property
    def status(self) -> str | None:
        return self.decision.terminal_state


class ConvergenceOrchestrator:
    """Compose validated policy, durable reservation, and one-shot adapter."""

    def __init__(
        self,
        *,
        workdir: Path | None = None,
        artifact_root: Path,
        evaluator: Callable[..., object] | object | None = None,
        proposal_provider: Callable[..., object] | None = None,
        reducer: PolicyReducer | None = None,
    ) -> None:
        self.workdir = Path(workdir).resolve() if workdir is not None else None
        self.artifact_root = Path(artifact_root)
        self.evaluator = evaluator
        self.proposal_provider = proposal_provider
        self.reducer = reducer or PolicyReducer()

    def run(
        self,
        request: ConvergenceRequest | Mapping[str, object],
        *,
        resume: bool = False,
    ) -> ConvergenceRunResult:
        validated = ConvergenceRequestValidator().validate(request)
        store = ConvergenceStore(self.artifact_root, validated.work_item_id, validated.run_id)
        run_path = store.run_path(validated.run_id)
        if not run_path.exists():
            store.create_run(validated)
        elif not resume:
            raise ConvergenceError("convergence run already exists; pass resume=True")
        else:
            manifest = store.read_manifest(validated.run_id)
            if (
                manifest.get("work_item_id") != validated.work_item_id
                or manifest.get("target_sha256") != target_sha256(validated.target)
                or manifest.get("policy_sha256") != validated.policy.policy_sha256
            ):
                raise HistoryCorruptionError("resume request does not match immutable convergence snapshot")
        snapshot = store.load_snapshot(validated.run_id) if (run_path / "manifest.json").exists() else build_snapshot(
            (), validated.policy, run_id=validated.run_id, work_item_id=validated.work_item_id, target=validated.target
        )
        # A bounded local guard protects against a corrupt adapter returning
        # immediately without creating a finalized result.
        for _ in range(int(validated.policy.max_iterations) + 2):
            pending = store.find_unfinished_attempt(validated.run_id)
            if pending is not None:
                state = pending.get("lifecycle_state")
                if state == "started":
                    if not store.recover_started_attempt(pending, run_id=validated.run_id):
                        store.mark_terminal_unknown(int(pending["attempt_number"]), run_id=validated.run_id)
                        snapshot = store.load_snapshot(validated.run_id)
                        decision = self.reducer.decide(snapshot)
                        summary = ConvergenceStoreFinalizer(store).finalize(snapshot, decision)
                        return ConvergenceRunResult(validated.run_id, run_path, decision, summary)
                    snapshot = store.load_snapshot(validated.run_id)
                    continue
                reservation = self._reservation_from_record(store, pending, validated.run_id)
                if not store.start_attempt(reservation, run_id=validated.run_id):
                    store.mark_terminal_unknown(reservation.attempt_number, run_id=validated.run_id)
                    snapshot = store.load_snapshot(validated.run_id)
                    decision = self.reducer.decide(snapshot)
                    summary = ConvergenceStoreFinalizer(store).finalize(snapshot, decision)
                    return ConvergenceRunResult(validated.run_id, run_path, decision, summary)
                try:
                    result = self._invoke_evaluator(validated, reservation)
                    store.bind_result(reservation, result, run_id=validated.run_id)
                except Exception as exc:  # noqa: BLE001 - crash boundary is recorded
                    store.mark_terminal_unknown(reservation.attempt_number, run_id=validated.run_id, reason_code="evaluator_failed_unknown")
                    snapshot = store.load_snapshot(validated.run_id)
                    decision = self.reducer.decide(snapshot)
                    summary = ConvergenceStoreFinalizer(store).finalize(snapshot, decision)
                    return ConvergenceRunResult(validated.run_id, run_path, decision, summary, str(exc))
                snapshot = store.load_snapshot(validated.run_id)

            # Empty history means the caller is asking to start the first
            # bounded attempt.  It is not passed through the post-result guard.
            if not snapshot.attempts:
                instruction = validated.initial_instruction
                if instruction is None:
                    instruction = self._provide_proposal(validated, snapshot)
                parsed, error = _instruction_error(
                    instruction,
                    verdict="changes_requested",
                    selected_finding_id=instruction.selected_finding_id if isinstance(instruction, RetryInstruction) else None,
                ) if instruction is not None else (None, "missing_initial_instruction")
                if parsed is None:
                    decision = _decision(
                        action="stop", terminal_state="blocked",
                        reason_code="initial_retry_instruction_invalid", failure_class="operator",
                        snapshot=snapshot, evidence=(f"initial_instruction:{error}",),
                    )
                    summary = ConvergenceStoreFinalizer(store).finalize(snapshot, decision)
                    return ConvergenceRunResult(validated.run_id, run_path, decision, summary)
                decision = _decision(
                    action="retry", terminal_state=None,
                    reason_code="initial_attempt", acceptance_outcome="unknown",
                    compliance_status="within_policy", retry_instruction=parsed,
                    snapshot=snapshot,
                )
                store.reserve_attempt(decision, run_id=validated.run_id)
                snapshot = store.load_snapshot(validated.run_id)
                continue

            signals = {
                "authority_signals": validated.authority_signals,
            }
            proposal = self._provide_proposal(validated, snapshot)
            decision = self.reducer.decide(snapshot, proposal, signals)
            if decision.action == "retry":
                store.reserve_attempt(decision, run_id=validated.run_id)
                snapshot = store.load_snapshot(validated.run_id)
                continue
            summary = ConvergenceStoreFinalizer(store).finalize(snapshot, decision)
            return ConvergenceRunResult(validated.run_id, run_path, decision, summary)
        # This path is only reachable if the durable state was changed by an
        # external actor while the process was running.
        snapshot = store.load_snapshot(validated.run_id)
        decision = _decision(
            action="stop", terminal_state="blocked",
            reason_code="orchestrator_cycle_guard", failure_class="operator", snapshot=snapshot,
        )
        summary = ConvergenceStoreFinalizer(store).finalize(snapshot, decision)
        return ConvergenceRunResult(validated.run_id, run_path, decision, summary)

    def resume(self, request: ConvergenceRequest | Mapping[str, object]) -> ConvergenceRunResult:
        return self.run(request, resume=True)

    def _reservation_from_record(self, store: ConvergenceStore, record: Mapping[str, object], run_id: str) -> AttemptReservation:
        decision = PolicyDecision.from_value(record.get("decision", {}))
        reservation = record.get("reservation", {})
        if not isinstance(reservation, Mapping):
            raise HistoryCorruptionError("attempt reservation record is invalid")
        attempt_number = record.get("attempt_number")
        attempt_key = record.get("attempt_key")
        if not isinstance(attempt_number, int) or not isinstance(attempt_key, str):
            raise HistoryCorruptionError("attempt reservation identity is invalid")
        return AttemptReservation(
            run_id=run_id,
            attempt_number=attempt_number,
            attempt_key=attempt_key,
            attempt_dir=store.run_path(run_id) / "attempts" / f"{attempt_number:04d}",
            decision=decision,
            parent_attempt_digest=reservation.get("parent_attempt_digest"),
            reservation_sha256=reservation.get("reservation_sha256"),
        )

    def _provide_proposal(self, request: ConvergenceRequest, snapshot: ValidatedHistorySnapshot) -> object | None:
        provider = self.proposal_provider
        if provider is None:
            return None
        return _invoke_callable(provider, snapshot, request)

    def _invoke_evaluator(self, request: ConvergenceRequest, reservation: AttemptReservation) -> object:
        if self.evaluator is None:
            raise ConvergenceError("convergence evaluator adapter is not configured")
        instruction = reservation.decision.retry_instruction
        if instruction is None:
            raise ConvergenceError("reserved attempt has no retry instruction")
        return _invoke_callable(self.evaluator, instruction, request, reservation)


def _invoke_callable(callable_object: object, *arguments: object) -> object:
    method = getattr(callable_object, "run", None)
    target = method if callable(method) else callable_object
    if not callable(target):
        raise TypeError("adapter must be callable or expose run()")
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target(*arguments)
    for count in range(len(arguments), 0, -1):
        try:
            signature.bind(*arguments[:count])
        except TypeError:
            continue
        return target(*arguments[:count])
    raise TypeError("adapter signature does not accept the supplied convergence arguments")


__all__ = [
    "ACTIONS",
    "AuthoritySignal",
    "BudgetLimit",
    "ConvergenceError",
    "ConvergenceOrchestrator",
    "ConvergencePolicy",
    "ConvergenceRequest",
    "ConvergenceRequestValidator",
    "ConvergenceRunResult",
    "ConvergenceStore",
    "ConvergenceStoreFinalizer",
    "FINDING_IDENTITY_VERSION",
    "HistoryCorruptionError",
    "HistoryLoader",
    "METRICS",
    "PolicyDecision",
    "Policy",
    "PolicyReducer",
    "ProgressEvidence",
    "RetryInstruction",
    "RetryProposal",
    "SCHEMA_VERSION",
    "SummaryBuilder",
    "TERMINAL_STATES",
    "UsageSample",
    "ValidatedHistorySnapshot",
    "aggregate_usage",
    "digest_intent",
    "build_snapshot",
    "build_terminal_summary",
    "canonical_sha256",
    "decide",
    "derive_history",
    "evaluate_progress",
    "finalize_summary",
    "made_progress",
    "open_findings",
    "render_convergence_summary",
    "render_summary",
    "sha256_file",
    "summary",
    "target_sha256",
    "verification_state",
]
