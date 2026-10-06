"""ADK sample ``workflows/request_input_rerun``: one ``rerun_on_resume`` review node.

The recorded trace (``traces/phone_broke.json``, copied from ADK's ``tests/``)
scripts the fake model and pins the native run. The compiled run needs
``state="legacy_read"`` (``human_review`` and ``send_email`` read ``draft``)
and ``interruptible=["human_review"]``; ``human_review`` reruns on resume and
reads the answer from ``ctx.resume_inputs``.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from adk_libpetri.workflow import WorkflowTranslationError, compile_workflow, verify_workflow
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, function_response, llm_agents, run, run_both
from . import agent

TRACES = Path(__file__).parent / "traces"
OPTS: dict[str, Any] = {"state": "legacy_read", "interruptible": ["human_review"]}
ROOT = "request_input_rerun@1"


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


AUTHORS = ["draft_email", "request_input_rerun"]


async def test_native_run_reproduces_the_recorded_trace() -> None:
    trace = load("phone_broke.json")
    native = await run(Factory(model_texts(trace))(), user_turns(trace), "native")
    assert rows(native) == trace_rows(trace)
    assert native.final_output is None
    assert native.authors == AUTHORS
    # The resumed run keeps the interrupted run's id: human_review@1 twice.
    assert [p for _, p, *_ in rows(native) if "human_review" in p] == [
        f"{ROOT}/human_review@1",
        f"{ROOT}/human_review@1",
        f"{ROOT}/human_review@2",
        f"{ROOT}/human_review@2",
    ]


SCENARIOS = {
    "recorded": None,
    "reject": ["phone broke", "reject"],
    "approve_first": ["phone broke", "approve"],
}


def scenario(key: str) -> tuple[list[str], list[Any]]:
    if key == "recorded":
        trace = load("phone_broke.json")
        return model_texts(trace), user_turns(trace)
    first, *answers = SCENARIOS[key]  # type: ignore[misc]
    return ["D1", "D2"], [first, *(answer(a) for a in answers)]


@pytest.mark.parametrize("key", list(SCENARIOS))
async def test_compiled_run_matches_native(key: str, orchestrator: Any) -> None:
    texts, turns = scenario(key)
    make = Factory(texts)
    native, petri, _ = await run_both(make, turns, orchestrator, **OPTS)
    assert rows(petri) == rows(native)
    assert petri.final_output == native.final_output
    assert petri.authors == native.authors == AUTHORS
    assert petri.texts == native.texts
    assert petri.state == native.state
    native_fake, petri_fake = make.fakes
    assert instructions(petri_fake) == instructions(native_fake)
    if key == "recorded":
        assert petri.state["feedback"] == "shorter"
        assert petri.texts[-1] == "Draft approved and sent successfully."
    if key == "reject":
        assert petri.texts[-1] == "Draft rejected."


async def test_compiled_resume_reuses_the_interrupted_run_id(orchestrator: Any) -> None:
    texts, turns = scenario("recorded")
    native, petri, _ = await run_both(Factory(texts), turns, orchestrator, **OPTS)
    assert rows(petri) == rows(native)

    def review_paths(r: Run) -> list[str]:
        return [p for _, p, *_ in rows(r) if "human_review" in p]

    assert (
        review_paths(petri)
        == review_paths(native)
        == [
            f"{ROOT}/human_review@1",
            f"{ROOT}/human_review@1",
            f"{ROOT}/human_review@2",
            f"{ROOT}/human_review@2",
        ]
    )


async def test_compiled_run_appends_no_events_of_its_own(orchestrator: Any) -> None:
    texts, turns = scenario("recorded")
    native, petri, _ = await run_both(Factory(texts), turns, orchestrator, **OPTS)
    assert [len(t) for t in petri.turns] == [len(t) for t in native.turns]
    # Each parked turn ends on human_review's own interrupt event, as in ADK.
    for r in (petri, native):
        assert [t[-1].long_running_tool_ids for t in r.turns[:-1]] == [{"human_review"}] * 2


async def test_budgeted_revise_edge_keeps_parity(orchestrator: Any) -> None:
    texts, turns = scenario("recorded")
    native, petri, compiled = await run_both(
        Factory(texts),
        turns,
        orchestrator,
        back_edge_budget={("human_review", "draft_email"): 2},
        **OPTS,
    )
    assert rows(petri) == rows(native)
    assert petri.state == native.state
    assert any("bounded by a back-edge budget" in f.message for f in compiled.report.findings)


def test_default_compile_rejects_the_state_reading_nodes() -> None:
    with pytest.raises(WorkflowTranslationError, match="human_review: reads session state"):
        compile_workflow(Factory([])())


def test_report_lists_what_is_approximated() -> None:
    cw = compile_workflow(Factory([])(), **OPTS)
    report = cw.report
    assert not report.rejected
    approx = {(f.subject, f.message) for f in report.of("approximated")}
    # ``ctx`` is not a state read (it is the context parameter); ``draft`` is.
    assert ("human_review", "reads legacy session state (parameter draft)") in approx
    assert ("send_email", "reads legacy session state (parameter draft)") in approx
    assert (
        "draft_email",
        "reads legacy session state (instruction {complaint}, instruction {feedback})",
    ) in approx
    assert any(s == "draft_email->human_review->draft_email" for s, _ in approx)
    assert cw._plans["human_review"].node.rerun_on_resume
    assert cw._plans["human_review"].unmatched
    # The ctx-taking node is opaque to the proofs (children it could run).
    opaque = {(f.subject, f.message) for f in report.of("opaque")}
    assert [f.subject for f in report.of("opaque")] == ["draft_email", "human_review"]
    assert any(s == "human_review" and "takes ctx" in m for s, m in opaque)
    exact = {(f.subject, f.message) for f in report.of("exact")}
    assert any(s == "human_review" and "wf/human_review/unmatched" in m for s, m in exact)
    assert any(s == "terminal output" and "WorkflowConfigurationError" in m for s, m in exact)
    names = set(cw.spec.transition_names)
    assert {
        "Wf_human_review_ResumeRun",
        "Wf_EndTurnOutput_send_email",
        "Wf_EndTurnOutput_reject_email",
        "Wf_EndTurnConflict_send_email_reject_email",
    } <= names


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
    # The lint: the routes come from ctx.resume_inputs, which the net cannot see.
    assert by_kind["route coverage"] == {
        "route coverage: unreachable(['wf/human_review/unmatched'])": "violated"
    }
    safety = by_kind["safety"]
    assert set(safety.values()) == {"proven"}, safety
    assert "send_email keeps one output: place_bound(send_email/terminalOutput, 1)" in safety
    assert "reject_email keeps one output: place_bound(reject_email/terminalOutput, 1)" in safety
    assert (
        "at most one terminal node outputs (ADK raises otherwise): unreachable(terminalConflict)"
        in safety
    )
    assert len(safety) == 2 + 2 + 1 + 5  # turn/permit, 2 terminals, conflict, 5 nodes
