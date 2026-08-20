from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.convergence_policy import (
    AuthoritySignal,
    BudgetLimit,
    ConvergenceError,
    ConvergenceOrchestrator,
    ConvergencePolicy,
    ConvergenceRequest,
    ConvergenceStore,
    HistoryCorruptionError,
    PolicyDecision,
    PolicyReducer,
    RetryInstruction,
    UsageSample,
    aggregate_usage,
    build_snapshot,
    build_terminal_summary,
    decide,
    target_sha256,
)


TARGET = {"kind": "work_item", "id": "wi-1", "source_ref": "05-work-breakdown.md#wi-1"}
TARGET_SHA = target_sha256(TARGET)


def result(
    verdict: str,
    *,
    findings: list[dict[str, object]] | None = None,
    reason: str = "review_result",
    usage: object | None = None,
    work_item_id: str = "wi-1",
    target_sha: str = TARGET_SHA,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "work_item_id": work_item_id,
        "target": TARGET,
        "target_sha256": target_sha,
        "verdict": verdict,
        "decision_reason": reason,
        "findings": findings or [],
        "observations": {"verify": {"state": "succeeded"}},
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def finding(identifier: str = "f-1", severity: str = "high") -> dict[str, object]:
    return {"id": identifier, "severity": severity, "status": "open"}


def policy(**overrides: object) -> ConvergencePolicy:
    values: dict[str, object] = {
        "max_iterations": 4,
        "max_finding_occurrences": 3,
        "max_consecutive_no_progress": 3,
        "unknown_budget_policy": "continue",
        "partial_budget_policy": "continue",
    }
    values.update(overrides)
    return ConvergencePolicy(**values)


def proposal(
    *,
    verdict: str = "changes_requested",
    finding_id: str | None = "f-1",
    reason: str = "fix_finding",
    operation: str = "edit_parser",
    intent: str = "change parser branch",
    relation: str = "new",
) -> RetryInstruction:
    return RetryInstruction(
        verdict=verdict,
        retry_reason=reason,
        strategy={"operation": operation, "target_ref": "target.txt"},
        expected_evidence=({"kind": "check", "ref": "tests"},),
        selected_finding_id=finding_id,
        change_intent=intent,
        prior_strategy_relation=relation,
    )


class ContractTests(unittest.TestCase):
    def snapshot(self, history: list[dict[str, object]], **overrides: object):
        return build_snapshot(
            history,
            policy(**overrides),
            run_id="run-1",
            work_item_id="wi-1",
            target=TARGET,
        )

    def test_max_iterations_is_required_and_failure_policies_are_independent(self) -> None:
        with self.assertRaisesRegex(ConvergenceError, "max_iterations"):
            ConvergencePolicy().validate()
        configured = ConvergencePolicy(
            max_iterations=3,
            retry_limits={"changes_requested": 1, "execution_failed": 2, "invalid_output": 0},
            retryable_reasons={"execution_failed": ("timeout",), "invalid_output": ("parser",)},
        )
        configured.validate()
        self.assertEqual(configured.retry_limits["execution_failed"], 2)
        self.assertEqual(configured.retry_limits["invalid_output"], 0)
        with self.assertRaisesRegex(ConvergenceError, "unsupported fields"):
            ConvergencePolicy.from_mapping({"max_iterations": 2, "unknown": True})

    def test_budget_and_usage_reject_invalid_values_without_zero_fallback(self) -> None:
        with self.assertRaises(ConvergenceError):
            BudgetLimit("tokens", float("nan"))
        with self.assertRaises(ConvergenceError):
            UsageSample("tokens", availability="available", value=None)
        samples = aggregate_usage([
            {"usage": [UsageSample("tokens", value=3, sample_id="a").to_payload()]},
            {"usage": [UsageSample("tokens", value=3, sample_id="a").to_payload()]},
        ])
        self.assertEqual(samples["tokens"]["value"], 3)
        self.assertEqual(samples["wall_seconds"]["availability"], "unavailable")

    def test_satisfied_is_terminal_and_preserves_post_run_overrun(self) -> None:
        snap = self.snapshot(
            [{"result": result("satisfied", usage={"tokens": 10})}],
            budgets={"tokens": BudgetLimit("tokens", 10)},
        )
        decision = PolicyReducer().decide(snap)
        self.assertEqual(decision.action, "finish")
        self.assertEqual(decision.terminal_state, "satisfied")
        self.assertEqual(decision.compliance_status, "overrun_observed")

    def test_changes_requested_selects_one_finding_and_requires_new_strategy(self) -> None:
        current = {"result": result("changes_requested", findings=[finding("low", "low"), finding("high", "high")])}
        snap = self.snapshot([current])
        decision = PolicyReducer().decide(snap, proposal(finding_id="high"))
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.selected_finding_id, "high")
        self.assertEqual(decision.retry_instruction.selected_finding_id, "high")

        same = self.snapshot([
            {"result": result("changes_requested", findings=[finding()]), "retry_instruction": proposal().to_payload()}
        ])
        blocked = PolicyReducer().decide(same, proposal())
        self.assertEqual(blocked.reason_code, "same_strategy_reused")

    def test_plan_defect_and_invalid_proposal_do_not_retry(self) -> None:
        plan = self.snapshot([{"result": result("plan_defect", findings=[finding()])}])
        decision = PolicyReducer().decide(plan)
        self.assertEqual((decision.action, decision.terminal_state), ("handoff", "waiting_for_human"))
        invalid = self.snapshot([{"result": result("changes_requested", findings=[finding()])}])
        decision = PolicyReducer().decide(invalid, None)
        self.assertEqual(decision.reason_code, "retry_instruction_invalid")

    def test_failure_verdicts_have_separate_limits_and_reason_allowlists(self) -> None:
        execution = self.snapshot(
            [{"result": result("execution_failed", reason="timeout")}],
            retry_limits={"changes_requested": 1, "execution_failed": 1, "invalid_output": 0},
            retryable_reasons={"execution_failed": ("timeout",), "invalid_output": ("parser",)},
        )
        retried = PolicyReducer().decide(execution, proposal(verdict="execution_failed", finding_id=None, reason="timeout"))
        self.assertEqual(retried.action, "retry")
        invalid = self.snapshot(
            [{"result": result("invalid_output", reason="parser")}],
            retry_limits={"changes_requested": 1, "execution_failed": 1, "invalid_output": 0},
            retryable_reasons={"execution_failed": ("timeout",), "invalid_output": ("parser",)},
        )
        self.assertEqual(PolicyReducer().decide(invalid, proposal(verdict="invalid_output", finding_id=None, reason="parser")).reason_code, "invalid_output_retry_limit")
        not_allowed = self.snapshot(
            [{"result": result("execution_failed", reason="permanent")}],
            retryable_reasons={"execution_failed": ("timeout",), "invalid_output": ("parser",)},
        )
        self.assertEqual(PolicyReducer().decide(not_allowed, proposal(verdict="execution_failed", finding_id=None, reason="permanent")).reason_code, "execution_failed_not_retryable")

    def test_iteration_recurrence_and_no_progress_are_non_success_stops(self) -> None:
        recurrent = self.snapshot(
            [{"result": result("changes_requested", findings=[finding()])} for _ in range(3)],
            max_consecutive_no_progress=9,
        )
        self.assertEqual(PolicyReducer().decide(recurrent, proposal()).reason_code, "finding_recurrence_limit")
        no_progress = self.snapshot(
            [{"result": result("changes_requested", findings=[finding()])} for _ in range(2)],
            max_finding_occurrences=9,
            max_consecutive_no_progress=1,
        )
        self.assertEqual(PolicyReducer().decide(no_progress, proposal()).reason_code, "no_progress_limit")
        at_limit = self.snapshot(
            [{"result": result("changes_requested", findings=[finding()])} for _ in range(2)],
            max_iterations=2,
            max_finding_occurrences=9,
            max_consecutive_no_progress=9,
        )
        self.assertEqual(PolicyReducer().decide(at_limit, proposal()).reason_code, "max_iterations_reached")

    def test_authority_signal_is_bound_to_run_and_target(self) -> None:
        snap = self.snapshot([{"result": result("changes_requested", findings=[finding()])}])
        signal = AuthoritySignal("run-1", TARGET_SHA, "operator", "convergence.approve")
        decision = PolicyReducer().decide(snap, proposal(), {"authority_signals": [signal]})
        self.assertEqual((decision.action, decision.terminal_state), ("handoff", "waiting_for_human"))
        stale = AuthoritySignal("other-run", TARGET_SHA, "operator", "convergence.approve")
        decision = PolicyReducer().decide(snap, proposal(), {"authority_signals": [stale]})
        self.assertEqual(decision.action, "retry")


class StoreAndOrchestratorTests(unittest.TestCase):
    def test_store_reservation_is_durable_and_result_binding_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = ConvergenceStore(root, "wi-1", "run-1")
            request = ConvergenceRequest(
                work_item_id="wi-1",
                target=TARGET,
                policy=policy(max_iterations=2),
                run_id="run-1",
                initial_instruction=proposal(),
            )
            store.create_run(request)
            empty = store.load_snapshot()
            decision = PolicyReducer().decide(empty)
            self.assertEqual(decision.reason_code, "empty_history")
            first = _initial_decision(empty, request.initial_instruction)
            reservation = store.reserve_attempt(first)
            self.assertTrue(store.start_attempt(reservation))
            store.bind_result(reservation, result("satisfied"))
            loaded = store.load_snapshot()
            self.assertEqual(loaded.current_verdict, "satisfied")
            self.assertEqual(loaded.reserved_attempt_count, 1)
            self.assertTrue((reservation.attempt_dir / "reservation.json").is_file())
            with self.assertRaises(ConvergenceError):
                store.start_attempt(reservation)

    def test_orchestrator_stops_at_finite_limit_and_writes_authoritative_summary(self) -> None:
        calls: list[str] = []

        def evaluator(instruction: RetryInstruction) -> object:
            calls.append(instruction.change_intent)
            return result("changes_requested", findings=[finding()])

        with tempfile.TemporaryDirectory() as tmpdir:
            request = ConvergenceRequest(
                work_item_id="wi-1",
                target=TARGET,
                policy=policy(max_iterations=2, max_finding_occurrences=9, max_consecutive_no_progress=9),
                run_id="run-loop",
                initial_instruction=proposal(intent="first strategy"),
            )
            output = ConvergenceOrchestrator(
                artifact_root=Path(tmpdir),
                evaluator=evaluator,
                proposal_provider=lambda _snapshot: proposal(intent="second strategy", operation="edit_test_adapter"),
            ).run(request)
            self.assertEqual(output.decision.reason_code, "max_iterations_reached")
            self.assertEqual(output.terminal_state, "blocked")
            self.assertEqual(len(calls), 2)
            self.assertEqual(output.summary["usage"]["iterations"], 2)
            self.assertIsNotNone(output.summary["last_committed_retry"])
            self.assertTrue((output.run_dir / "finalized").is_file())
            self.assertEqual(
                output.summary["summary_payload_sha256"],
                json.loads((output.run_dir / "summary.json").read_text())["summary_payload_sha256"],
            )

    def test_started_attempt_is_not_reexecuted_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            request = ConvergenceRequest(
                work_item_id="wi-1",
                target=TARGET,
                policy=policy(max_iterations=2),
                run_id="run-crash",
                initial_instruction=proposal(),
            )
            store = ConvergenceStore(Path(tmpdir), "wi-1", "run-crash")
            store.create_run(request)
            empty = store.load_snapshot()
            reservation = store.reserve_attempt(_initial_decision(empty, request.initial_instruction))
            self.assertTrue(store.start_attempt(reservation))
            calls: list[int] = []

            def evaluator(_instruction: RetryInstruction) -> object:
                calls.append(1)
                return result("satisfied")

            resumed = ConvergenceOrchestrator(artifact_root=Path(tmpdir), evaluator=evaluator).run(request, resume=True)
            self.assertEqual(resumed.terminal_state, "waiting_for_human")
            self.assertEqual(resumed.decision.reason_code, "attempt_outcome_unknown")
            self.assertEqual(calls, [])

    def test_corrupt_reservation_digest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            request = ConvergenceRequest(
                work_item_id="wi-1", target=TARGET, policy=policy(max_iterations=1), run_id="run-corrupt", initial_instruction=proposal()
            )
            store = ConvergenceStore(Path(tmpdir), "wi-1", "run-corrupt")
            store.create_run(request)
            empty = store.load_snapshot()
            reservation = store.reserve_attempt(_initial_decision(empty, request.initial_instruction))
            path = reservation.attempt_dir / "reservation.json"
            payload = json.loads(path.read_text())
            payload["attempt_key"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(HistoryCorruptionError):
                store.load_snapshot()

    def test_lifecycle_rollback_and_cross_attempt_binding_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            request = ConvergenceRequest(
                work_item_id="wi-1", target=TARGET, policy=policy(max_iterations=2),
                run_id="run-history-binding", initial_instruction=proposal(),
            )
            store = ConvergenceStore(Path(tmpdir), "wi-1", request.run_id)
            store.create_run(request)
            reservation = store.reserve_attempt(
                _initial_decision(store.load_snapshot(), request.initial_instruction)
            )
            self.assertTrue(store.start_attempt(reservation))

            lifecycle_path = reservation.attempt_dir / "lifecycle.json"
            lifecycle = json.loads(lifecycle_path.read_text())
            lifecycle["state"] = "reserved"
            lifecycle_path.write_text(json.dumps(lifecycle), encoding="utf-8")
            with self.assertRaisesRegex(HistoryCorruptionError, "transition history"):
                store.load_snapshot()

        with tempfile.TemporaryDirectory() as tmpdir:
            request = ConvergenceRequest(
                work_item_id="wi-1", target=TARGET, policy=policy(max_iterations=1),
                run_id="run-result-binding", initial_instruction=proposal(),
            )
            store = ConvergenceStore(Path(tmpdir), "wi-1", request.run_id)
            store.create_run(request)
            reservation = store.reserve_attempt(
                _initial_decision(store.load_snapshot(), request.initial_instruction)
            )
            self.assertTrue(store.start_attempt(reservation))
            store.bind_result(reservation, result("satisfied"))
            binding_path = reservation.attempt_dir / "result-binding.json"
            binding = json.loads(binding_path.read_text())
            binding["attempt_number"] = 2
            binding_path.write_text(json.dumps(binding), encoding="utf-8")
            with self.assertRaisesRegex(HistoryCorruptionError, "binding identity"):
                store.load_snapshot()


def _initial_decision(snapshot, instruction: RetryInstruction | None):
    if instruction is None:
        raise AssertionError("test requires initial instruction")
    return PolicyDecision(
        action="retry",
        terminal_state=None,
        reason_code="initial_attempt",
        acceptance_outcome="unknown",
        compliance_status="within_policy",
        retry_instruction=instruction.normalized(),
        evidence_refs=(f"snapshot:{snapshot.snapshot_sha256}",),
        snapshot_sha256=snapshot.snapshot_sha256,
        policy_sha256=snapshot.policy_sha256,
    )


if __name__ == "__main__":
    unittest.main()
