"""WF-*: structure, report and proofs of compiled ADK workflows (no execution)."""

from __future__ import annotations

import pytest
from google.adk.workflow import FunctionNode, Workflow

from adk_libpetri.workflow import WorkflowTranslationError, compile_workflow, verify_workflow
from support.smt_proofs import requires_z3

from . import samples


def _verdicts(cw, k=2):  # type: ignore[no-untyped-def]
    return {p.label: p.result.verdict for p in verify_workflow(cw, k=k)}


def test_every_node_gets_a_run_transition_and_a_seeded_idle() -> None:
    cw = compile_workflow(samples.linear())
    names = set(cw.spec.transition_names)
    assert {"Wf_Start", "Wf_upper_Run", "Wf_finish_Run", "Wf_EndTurnOutput"} <= names
    assert cw.initial_marking()["wf/upper/idle"] == [None]


def test_routes_become_xor_branches_and_default_is_the_fallback() -> None:
    cw = compile_workflow(samples.router())
    plan = cw._plans["classify"]
    assert set(plan.route_to_branch) == {"bug"}
    assert plan.default_branch is not None
    assert not plan.unmatched


def test_join_consumes_one_token_per_predecessor() -> None:
    cw = compile_workflow(samples.fan_join())
    run = cw.spec.transition("Wf_join_Run")
    assert {i.place.name for i in run.inputs} >= {"wf/join/from/upper", "wf/join/from/lower"}


def test_retry_is_unrolled_into_attempts_with_backoff() -> None:
    cw = compile_workflow(samples.retrying())
    names = cw.spec.transition_names
    assert "Wf_flaky_Run3" in names and "Wf_flaky_Backoff2" in names
    assert cw.spec.transition("Wf_flaky_Backoff1").timing.earliest_ms == 10


def test_state_reading_function_nodes_are_rejected_by_default() -> None:
    def reads_state(user_name: str) -> str:
        return user_name

    wf = Workflow(name="stateful", edges=[("START", FunctionNode(func=reads_state, name="s"))])
    with pytest.raises(WorkflowTranslationError, match="commitment 2"):
        compile_workflow(wf)
    cw = compile_workflow(wf, state="legacy_read")
    assert any(f.subject == "s" for f in cw.report.of("approximated"))


def test_unbudgeted_cycles_are_reported() -> None:
    cw = compile_workflow(samples.looping())
    assert any("unbudgeted cycle" in f.message for f in cw.report.findings)
    budgeted = compile_workflow(samples.looping(), back_edge_budget={("counter", "counter"): 3})
    assert any("bounded by a back-edge budget" in f.message for f in budgeted.report.findings)
    assert "Wf_Edge_counter_counter_Exhausted" in budgeted.spec.transition_names


def test_unknown_interruptible_and_budget_names_are_rejected() -> None:
    with pytest.raises(WorkflowTranslationError):
        compile_workflow(samples.linear(), interruptible=["nope"])
    with pytest.raises(WorkflowTranslationError):
        compile_workflow(samples.linear(), back_edge_budget={("a", "b"): 1})


@requires_z3
@pytest.mark.parametrize(
    "make",
    [
        samples.linear,
        samples.router,
        samples.fan_join,
        # Z3 time on the unrolled retry net varies widely (8s to 50s locally).
        pytest.param(samples.retrying, marks=pytest.mark.timeout(300)),
        samples.concurrent,
    ],
)
def test_compiled_dags_prove_every_safety_claim_and_deadlock_freedom(make) -> None:  # type: ignore[no-untyped-def]
    verdicts = _verdicts(compile_workflow(make()))
    assert all(v == "proven" for v in verdicts.values()), verdicts


@requires_z3
def test_hitl_workflow_proves_safety() -> None:
    verdicts = _verdicts(compile_workflow(samples.hitl(), interruptible=["ask"]))
    assert all(v == "proven" for v in verdicts.values()), verdicts


@requires_z3
def test_budgeted_cycle_proves_safety_and_deadlock_freedom() -> None:
    cw = compile_workflow(samples.looping(), back_edge_budget={("counter", "counter"): 3})
    verdicts = _verdicts(cw)
    assert all(v == "proven" for v in verdicts.values()), verdicts


@requires_z3
def test_a_route_with_no_edge_is_a_reachable_unmatched_sink() -> None:
    """ADK logs a warning and ends the branch; the net names the place, and the
    verifier shows the workflow can reach it."""
    from google.adk.workflow import Workflow

    from .samples import handle_bug, handle_other, node

    c = node(samples.classify)
    wf = Workflow(
        name="no_default",
        edges=[("START", c), (c, {"bug": node(handle_bug), "feature": node(handle_other)})],
    )
    verdicts = _verdicts(compile_workflow(wf))
    unmatched = [v for k, v in verdicts.items() if "unmatched" in k]
    assert unmatched == ["violated"]
