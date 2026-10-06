"""ADK sample ``workflows/request_input_advanced``: structured ``RequestInput``.

The recorded trace (``traces/2_sick_days.json``, copied from ADK's ``tests/``)
scripts the fake model and pins the native run. The compiled run needs
``state="legacy_read"`` (``evaluate_request`` and ``process_decision`` read
``request``) and ``interruptible=["evaluate_request"]``. The interrupt carries
a ``payload`` and a ``response_schema``; the answer is JSON text that ADK
parses into ``TimeOffDecision`` for ``process_decision``.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from google.adk.runners import InMemoryRunner
from google.adk.workflow._errors import WorkflowDataError

from adk_libpetri.workflow import (
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
OPTS: dict[str, Any] = {"state": "legacy_read", "interruptible": ["evaluate_request"]}


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((TRACES / name).read_text())["events"]


def model_texts(trace: list[dict[str, Any]]) -> list[str]:
    return [
        "".join(p.get("text", "") for p in e["content"]["parts"])
        for e in trace
        if e["author"] == "process_request"
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


AUTHORS = ["process_request", "request_input_advanced"]


def interrupt_args(r: Run) -> dict[str, Any]:
    for e in r.events:
        for p in e.content.parts if e.content and e.content.parts else []:
            if p.function_call and p.function_call.name == "adk_request_input":
                return dict(p.function_call.args or {})
    raise AssertionError("no interrupt")


async def test_native_run_reproduces_the_recorded_trace() -> None:
    trace = load("2_sick_days.json")
    native = await run(Factory(model_texts(trace))(), user_turns(trace), "native")
    assert rows(native) == trace_rows(trace)
    assert native.authors == AUTHORS
    assert native.final_output == {"days": 2, "reason": "sick"}  # process_request's output
    assert native.texts[-1] == "Time Off Approved! 2 out of 2 days granted."
    assert native.state == {"request": {"days": 2, "reason": "sick"}}


SCENARIOS: dict[str, tuple[str, list[str], str]] = {
    "recorded": (
        '{"days":2,"reason":"sick"}',
        ['{"approved": true}'],
        "Time Off Approved! 2 out of 2 days granted.",
    ),
    "partial": (
        '{"days":5,"reason":"Disney World"}',
        ['{"approved": true, "approved_days": 3}'],
        "Time Off Approved! 3 out of 5 days granted.",
    ),
    "denied": (
        '{"days":5,"reason":"Disney World"}',
        ['{"approved": false, "approved_days": 0}'],
        "Time Off Denied.",
    ),
    "auto_approved": (
        '{"days":1,"reason":"under the weather"}',
        [],
        "Time Off Approved! 1 out of 1 days granted.",
    ),
}


@pytest.mark.parametrize("key", list(SCENARIOS))
async def test_compiled_run_matches_native(key: str, orchestrator: Any) -> None:
    model, answers, last = SCENARIOS[key]
    turns = ["time off please", *(answer(a) for a in answers)]
    native, petri, _ = await run_both(Factory([model]), turns, orchestrator, **OPTS)
    assert rows(petri) == rows(native)
    assert petri.final_output == native.final_output
    assert petri.authors == native.authors == AUTHORS
    assert petri.texts == native.texts
    assert petri.texts[-1] == native.texts[-1] == last
    assert petri.state == native.state
    if answers:
        # The structured interrupt reaches the client unchanged.
        assert interrupt_args(petri) == interrupt_args(native)
        assert interrupt_args(petri)["interruptId"] == "manager_approval"
        assert interrupt_args(petri)["payload"] == json.loads(model)
        assert "approved" in interrupt_args(petri)["response_schema"]["properties"]


async def test_compiled_run_appends_no_events_of_its_own(orchestrator: Any) -> None:
    trace = load("2_sick_days.json")
    native, petri, _ = await run_both(
        Factory(model_texts(trace)), user_turns(trace), orchestrator, **OPTS
    )
    assert [len(t) for t in petri.turns] == [len(t) for t in native.turns]
    # The parked turn ends on evaluate_request's own interrupt event, as in ADK.
    assert petri.turns[0][-1].long_running_tool_ids == {"manager_approval"}
    assert native.turns[0][-1].long_running_tool_ids == {"manager_approval"}


async def outcomes_of_bad_answers(node: Any, app: str) -> list[str]:
    """Start a request, then answer it badly, then well; what each answer did."""
    runner = InMemoryRunner(node=node, app_name=app)
    session = await runner.session_service.create_session(app_name=app, user_id="u")
    sid = session.id
    async for _ in runner.run_async(user_id="u", session_id=sid, new_message=msg("2 days")):
        pass
    outcomes = []
    for result in ["yes please", '{"approved": false}']:
        reply = function_response("manager_approval", "adk_request_input", {"result": result})
        try:
            events = [
                e async for e in runner.run_async(user_id="u", session_id=sid, new_message=reply)
            ]
            outcomes.append(Run(events=events).texts[-1])
        except WorkflowDataError as err:
            outcomes.append(type(err).__name__)
    return outcomes


async def test_an_answer_off_the_response_schema_fails_like_adk(orchestrator: Any) -> None:
    """ADK validates the answer against ``response_schema`` while rehydrating the
    root from session events, before the root node runs: the compiled root sees
    the same check. The bad answer stays in the events, so a later good answer
    fails too, in both."""
    make = Factory(['{"days":2,"reason":"sick"}'])
    native = await outcomes_of_bad_answers(make(), "native")
    compiled = compile_workflow(make(), **OPTS)
    node = PetriWorkflow.from_compiled(compiled, orchestrator=orchestrator)
    petri = await outcomes_of_bad_answers(node, "compiled")
    assert petri == native == ["WorkflowDataError", "WorkflowDataError"]


def test_default_compile_rejects_the_state_reading_nodes() -> None:
    with pytest.raises(WorkflowTranslationError, match="evaluate_request: reads session state"):
        compile_workflow(Factory([])())
    with pytest.raises(WorkflowTranslationError, match="process_decision: reads session state"):
        compile_workflow(Factory([])())


def test_report_lists_what_is_approximated() -> None:
    cw = compile_workflow(Factory([])(), **OPTS)
    report = cw.report
    assert not report.rejected
    approx = {(f.subject, f.message) for f in report.of("approximated")}
    assert ("evaluate_request", "reads legacy session state (parameter request)") in approx
    assert ("process_decision", "reads legacy session state (parameter request)") in approx
    assert not any("cycle" in m for _, m in approx)
    assert [f.subject for f in report.of("opaque")] == ["process_request"]
    assert not cw.unmatched_places()
    assert not any(f.subject == "terminal output" for f in report.findings)  # one terminal
    names = set(cw.spec.transition_names)
    assert {"Wf_evaluate_request_ResumeRun", "Wf_EndTurnOutput_process_decision"} <= names


@requires_z3
@pytest.mark.timeout(300)
def test_proofs() -> None:
    cw = compile_workflow(Factory([])(), **OPTS)
    proofs = verify_workflow(cw, k=1)
    suffix = " [on the match-free over-approximation]"
    assert all(p.label.endswith(suffix) for p in proofs)
    assert {p.kind for p in proofs} == {"safety"}  # interruptible: no deadlock claim
    verdicts = {p.label.removesuffix(suffix): p.result.verdict for p in proofs}
    assert set(verdicts.values()) == {"proven"}, verdicts
    assert (
        "process_decision keeps one output: place_bound(process_decision/terminalOutput, 1)"
        in verdicts
    )
    assert not any("terminalConflict" in label for label in verdicts)
    assert len(verdicts) == 2 + 1 + 3  # turn/permit, one terminal, three nodes
