"""WF-*: structure, report and proofs of compiled ADK workflows (no execution)."""

from __future__ import annotations

import pytest
from google.adk.workflow import FunctionNode, Workflow

from adk_libpetri.workflow import WorkflowTranslationError, compile_workflow, verify_workflow
from support.smt_proofs import requires_z3

from . import samples


def _verdicts(cw, k=2, kinds=("safety", "deadlock")):  # type: ignore[no-untyped-def]
    return {p.label: p.result.verdict for p in verify_workflow(cw, k=k) if p.kind in kinds}


def test_every_node_gets_a_run_transition_and_a_seeded_idle() -> None:
    cw = compile_workflow(samples.linear())
    names = set(cw.spec.transition_names)
    assert {"Wf_Start", "Wf_upper_Run", "Wf_finish_Run", "Wf_EndTurnOutput_finish"} <= names
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


def test_retry_stays_on_the_node_for_adks_runner() -> None:
    cw = compile_workflow(samples.retrying())
    assert [t for t in cw.spec.transition_names if t.startswith("Wf_flaky_")] == ["Wf_flaky_Run"]
    assert cw._plans["flaky"].node.retry_config is not None
    assert any("retry_config kept" in f.message for f in cw.report.of("exact"))


def test_terminal_nodes_keep_their_last_output_and_two_with_output_conflict() -> None:
    cw = compile_workflow(samples.fan_join())
    (end,) = cw.terminal_nodes
    run = cw.spec.transition(f"Wf_{end}_Run")
    assert [p.name for p in run.resets] == [f"wf/{end}/terminalOutput"]
    two = Workflow(
        name="two_ends",
        edges=[("START", samples.node(samples.upper)), ("START", samples.node(samples.lower))],
    )
    names = compile_workflow(two).spec.transition_names
    assert "Wf_EndTurnConflict_upper_lower" in names


def test_state_reads_are_found_beyond_function_parameters() -> None:
    from google.adk.agents.llm_agent import LlmAgent

    def via_ctx(ctx, node_input: str) -> str:  # type: ignore[no-untyped-def]
        ctx.state["seen"] = node_input  # a write: the legacy bridge, allowed
        return ctx.state.get("user_name", "")

    def via_input_binding(a: int, b: int) -> int:
        return a + b

    with pytest.raises(WorkflowTranslationError, match=r"ctx.state.get\('user_name'\)"):
        compile_workflow(Workflow(name="w", edges=[("START", FunctionNode(func=via_ctx))]))
    with pytest.raises(WorkflowTranslationError, match=r"instruction \{topic\}"):
        compile_workflow(
            Workflow(name="w", edges=[("START", LlmAgent(name="a", instruction="on {topic?}"))])
        )
    bound = FunctionNode(func=via_input_binding, parameter_binding="node_input")
    compile_workflow(Workflow(name="w", edges=[("START", bound)]))


def test_task_mode_agents_are_rejected() -> None:
    from google.adk.agents.llm_agent import LlmAgent

    wf = Workflow(name="w", edges=[("START", LlmAgent(name="a", mode="task"))])
    with pytest.raises(WorkflowTranslationError, match="mode='task'"):
        compile_workflow(wf)


def test_a_non_workflow_root_is_a_type_error() -> None:
    from google.adk.agents.llm_agent import LlmAgent

    with pytest.raises(TypeError, match=r"expects a google\.adk Workflow"):
        compile_workflow(LlmAgent(name="a"))  # type: ignore[arg-type]


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
        samples.retrying,
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
    proofs = verify_workflow(compile_workflow(wf), k=2)
    assert [p.result.verdict for p in proofs if p.kind == "route coverage"] == ["violated"]
    assert all(p.proven for p in proofs if p.kind != "route coverage")
