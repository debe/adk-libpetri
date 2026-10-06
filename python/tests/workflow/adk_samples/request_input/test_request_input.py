"""ADK sample ``workflows/request_input``: draft, human review, revise loop.

The recorded traces (``traces/*.json``, copied from ADK's ``tests/``) script
the fake model and pin the native run. The compiled run needs
``state="legacy_read"`` (``request_human_review`` and ``send_email`` read
``draft`` from session state) and ``interruptible=["request_human_review"]``.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from google.adk.runners import InMemoryRunner

from adk_libpetri.workflow import (
    LoopBudgetExhausted,
    NotInterruptibleError,
    PetriWorkflow,
    WorkflowTranslationError,
    compile_workflow,
    verify_workflow,
)
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, function_response, llm_agents, msg, run, run_both
from . import agent

TRACES = Path(__file__).parent / "traces"
OPTS: dict[str, Any] = {"state": "legacy_read", "interruptible": ["request_human_review"]}


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((TRACES / name).read_text())["events"]


def model_texts(trace: list[dict[str, Any]]) -> list[str]:
    return [
        "".join(p.get("text", "") for p in e["content"]["parts"])
        for e in trace
        if e["author"] == "draft_email"
    ]


def answer(result: str) -> Callable[[list[Any]], Any]:
    """Answer the latest ``adk_request_input`` interrupt with ``result``."""

    def build(events: list[Any]) -> Any:
        for e in reversed(events):
            for p in e.content.parts if e.content and e.content.parts else []:
                fc = p.function_call
                if fc and fc.name == "adk_request_input":
                    return function_response(fc.id, fc.name, {"result": result})
        raise AssertionError("no pending adk_request_input")

    return build


def user_turns(trace: list[dict[str, Any]]) -> list[Any]:
    turns: list[Any] = []
    for e in trace:
        if e["author"] != "user":
            continue
        part = e["content"]["parts"][0]
        if "text" in part:
            turns.append(part["text"])
        else:
            turns.append(answer(part["functionResponse"]["response"]["result"]))
    return turns


def trace_rows(trace: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    rows = []
    for e in trace:
        if e["author"] == "user":
            continue
        parts = (e.get("content") or {}).get("parts", [])
        txt = "".join(p.get("text", "") for p in parts) or None
        fc = next((p["functionCall"]["name"] for p in parts if "functionCall" in p), None)
        actions = e.get("actions", {})
        rows.append(
            (
                e["author"],
                e["nodeInfo"]["path"],
                txt,
                fc,
                actions.get("stateDelta", {}),
                actions.get("route"),
            )
        )
    return rows


def rows(r: Run, *, paths: bool = True) -> list[tuple[Any, ...]]:
    out = []
    for e in r.events:
        parts = e.content.parts if e.content and e.content.parts else []
        txt = "".join(p.text or "" for p in parts) or None
        fc = next((p.function_call.name for p in parts if p.function_call), None)
        path = e.node_info.path if paths and e.node_info else None
        out.append((e.author, path, txt, fc, dict(e.actions.state_delta), e.actions.route))
    return out


class Factory:
    """Fresh vendored module (fresh nodes) and fresh fakes per ``make()``."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.fakes: list[ScriptedLlm] = []

    def __call__(self) -> Any:
        wf = importlib.reload(agent).root_agent
        for a in llm_agents(wf):
            fake = ScriptedLlm.of(*(text(t) for t in self.texts))
            self.fakes.append(fake)
            a.model = fake
        return wf


def instructions(fake: ScriptedLlm) -> list[str]:
    return [str(r.config.system_instruction) for r in fake.requests]


TRACE_FILES = ["phone_broke.json", "phone_broke_reject.json"]


@pytest.mark.parametrize("name", TRACE_FILES)
async def test_native_run_reproduces_the_recorded_trace(name: str) -> None:
    trace = load(name)
    make = Factory(model_texts(trace))
    native = await run(make(), user_turns(trace), "native")
    assert rows(native) == trace_rows(trace)
    assert native.final_output is None  # the terminal nodes only send a message
    assert native.authors == ["draft_email", "request_input"]


@pytest.mark.parametrize("name", TRACE_FILES)
async def test_compiled_run_matches_native(name: str, orchestrator: Any) -> None:
    trace = load(name)
    make = Factory(model_texts(trace))
    native, petri, _ = await run_both(make, user_turns(trace), orchestrator, **OPTS)
    assert rows(petri) == rows(native)
    assert petri.final_output == native.final_output
    assert petri.authors == native.authors
    assert petri.texts == native.texts
    assert petri.state == native.state
    # Both runs rendered the same instructions (complaint, then the feedback).
    native_fake, petri_fake = make.fakes
    assert instructions(petri_fake) == instructions(native_fake)
    if name == "phone_broke.json":
        assert '"shorter"' in instructions(petri_fake)[1]
        assert petri.state["feedback"] == "shorter"


async def test_compiled_run_appends_no_events_of_its_own(orchestrator: Any) -> None:
    trace = load("phone_broke.json")
    native, petri, _ = await run_both(
        Factory(model_texts(trace)), user_turns(trace), orchestrator, **OPTS
    )
    assert [len(t) for t in petri.turns] == [len(t) for t in native.turns]

    # A parked turn ends on the node's own interrupt event, as in ADK (the ids
    # are fresh uuids per run, so compare their presence and author).
    def ends(r: Run) -> list[tuple[str, int]]:
        return [(t[-1].author, len(t[-1].long_running_tool_ids or ())) for t in r.turns]

    assert ends(petri) == ends(native) == [("request_input", 1)] * 2 + [("request_input", 0)]


async def test_a_new_complaint_while_parked_restarts_with_adk_run_ids(orchestrator: Any) -> None:
    turns = ["phone broke", "order lost", answer("approve")]
    native, petri, _ = await run_both(Factory(["D1", "D2"]), turns, orchestrator, **OPTS)
    # The new message drops the pending interrupt and restarts the workflow run.
    assert (
        petri.state
        == native.state
        == {
            "complaint": "order lost",
            "feedback": "",
            "draft": "D2",
        }
    )
    # Run ids restart with the new workflow run, as in ADK (process_input@1).
    assert rows(petri) == rows(native)


async def test_budgeted_revise_edge_keeps_parity(orchestrator: Any) -> None:
    trace = load("phone_broke.json")
    native, petri, compiled = await run_both(
        Factory(model_texts(trace)),
        user_turns(trace),
        orchestrator,
        back_edge_budget={("handle_human_review", "draft_email"): 3},
        **OPTS,
    )
    assert rows(petri) == rows(native)
    assert petri.state == native.state
    assert any("bounded by a back-edge budget" in f.message for f in compiled.report.findings)


async def drive(node: Any, turns: list[Any], app: str, events: list[Any]) -> None:
    """Like ``run``, but appends to ``events`` as they come, so a raising turn keeps them."""
    runner = InMemoryRunner(node=node, app_name=app)
    session = await runner.session_service.create_session(app_name=app, user_id="u")
    for turn in turns:
        message = turn(events) if callable(turn) else msg(turn)
        async for e in runner.run_async(user_id="u", session_id=session.id, new_message=message):
            events.append(e)


async def test_revise_budget_spans_the_paused_turns(orchestrator: Any) -> None:
    """The budget is seeded by Wf_Start and not reset by Wf_EndTurnWaiting, so it
    bounds revisions over the whole workflow run, across its interrupts. A spent
    budget fails the turn as a failing ``Workflow`` does: ADK's node runner
    records an error event, then ``Runner.run_async`` raises."""
    turns = ["phone broke", answer("shorter"), answer("friendlier")]
    make = Factory(["D1", "D2", "D3"])
    native = await run(make(), turns, "native")
    assert [t for t in native.texts if t.startswith("D")] == ["D1", "D2", "D3"]  # unbounded
    compiled = compile_workflow(
        make(), back_edge_budget={("handle_human_review", "draft_email"): 1}, **OPTS
    )
    node = PetriWorkflow.from_compiled(compiled, orchestrator=orchestrator)
    events: list[Any] = []
    with pytest.raises(LoopBudgetExhausted, match="handle_human_review->draft_email"):
        await drive(node, turns, "compiled", events)
    texts = Run(events=events).texts
    assert [t for t in texts if t.startswith("D")] == ["D1", "D2"]  # no second revision
    assert events[-1].error_code == "LoopBudgetExhausted"
    assert events[-1].author == "request_input"


async def test_an_interrupt_from_a_node_not_compiled_interruptible_fails_the_turn(
    orchestrator: Any,
) -> None:
    """Without ``interruptible`` the net cannot park ``request_human_review``: the
    turn fails with a typed error (ADK's node runner records the error event)
    where ADK would wait for the answer."""
    make = Factory(["D1"])
    native = await run(make(), ["phone broke"], "native")
    assert native.turns[-1][-1].long_running_tool_ids  # ADK waits
    compiled = compile_workflow(make(), state="legacy_read")
    node = PetriWorkflow.from_compiled(compiled, orchestrator=orchestrator)
    events: list[Any] = []
    with pytest.raises(NotInterruptibleError, match="request_human_review"):
        await drive(node, ["phone broke"], "compiled", events)
    assert events[-1].error_code == "NotInterruptibleError"


def test_default_compile_rejects_the_state_reading_nodes() -> None:
    with pytest.raises(WorkflowTranslationError, match="request_human_review: reads session"):
        compile_workflow(Factory([])())
    with pytest.raises(WorkflowTranslationError, match="send_email: reads session state"):
        compile_workflow(Factory([])(), interruptible=["request_human_review"])


def test_report_lists_what_is_approximated() -> None:
    cw = compile_workflow(Factory([])(), **OPTS)
    report = cw.report
    assert not report.rejected
    approx = {(f.subject, f.message) for f in report.of("approximated")}
    assert ("request_human_review", "reads legacy session state (parameter draft)") in approx
    assert ("send_email", "reads legacy session state (parameter draft)") in approx
    assert (
        "draft_email",
        "reads legacy session state (instruction {complaint}, instruction {feedback})",
    ) in approx
    assert any(
        s == "draft_email->request_human_review->handle_human_review->draft_email"
        and "unbudgeted cycle" in m
        for s, m in approx
    )
    assert [f.subject for f in report.of("opaque")] == ["draft_email"]
    assert cw._plans["handle_human_review"].unmatched  # three routes, no DEFAULT_ROUTE
    exact = {(f.subject, f.message) for f in report.of("exact")}
    assert any(
        s == "handle_human_review" and "wf/handle_human_review/unmatched" in m for s, m in exact
    )
    assert any(s == "terminal output" and "WorkflowConfigurationError" in m for s, m in exact)
    names = cw.spec.transition_names
    assert "Wf_request_human_review_ResumeRun" in names
    assert {
        "Wf_EndTurnOutput_send_email",
        "Wf_EndTurnOutput_reject_email",
        "Wf_EndTurnConflict_send_email_reject_email",
    } <= set(names)


@requires_z3
@pytest.mark.timeout(300)
def test_proofs() -> None:
    cw = compile_workflow(Factory([])(), **OPTS)
    proofs = verify_workflow(cw, k=1)
    suffix = " [on the match-free over-approximation]"
    assert all(p.label.endswith(suffix) for p in proofs)
    assert not any(p.kind == "deadlock" for p in proofs)  # interruptible
    by_kind: dict[str, dict[str, str]] = {}
    for p in proofs:
        by_kind.setdefault(p.kind, {})[p.label.removesuffix(suffix)] = p.result.verdict
    # The lint: the net cannot see the else branch, so it cannot rule out an
    # unmatched route (which ADK would treat as the end of the branch).
    assert by_kind["route coverage"] == {
        "route coverage: unreachable(['wf/handle_human_review/unmatched'])": "violated"
    }
    safety = by_kind["safety"]
    assert set(safety.values()) == {"proven"}, safety
    assert "send_email keeps one output: place_bound(send_email/terminalOutput, 1)" in safety
    assert "reject_email keeps one output: place_bound(reject_email/terminalOutput, 1)" in safety
    assert (
        "at most one terminal node outputs (ADK raises otherwise): unreachable(terminalConflict)"
        in safety
    )
    assert len(safety) == 2 + 2 + 1 + 6  # turn/permit, 2 terminals, conflict, 6 nodes
