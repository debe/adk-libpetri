"""ADK sample ``workflows/loop``: generate a headline, grade it, loop on "unrelated".

Recorded traces: ``tests/flower.json`` (one round back through
``generate_headline``) and ``tests/computer.json`` (accepted at once). The
loop's exit is ``route="tech-related"``, which has no edge: ADK logs "none
were matched ... The branch will end", and the net takes its
``wf/route_headline/unmatched`` branch.

``generate_headline``'s instruction reads ``{topic}`` and ``{feedback?}``
from session state (written by ``process_input`` and by
``evaluate_headline``'s ``output_key``), so the compiler rejects the sample by
default and compiles it under ``state="legacy_read"``.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow

from adk_libpetri.workflow import (
    LoopBudgetExhausted,
    PetriWorkflow,
    WorkflowTranslationError,
    compile_workflow,
    verify_workflow,
)
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, llm_agents, msg, run, run_both
from . import agent as sample

BACK_EDGE = ("route_headline", "generate_headline")
LEGACY: dict[str, Any] = {"state": "legacy_read"}


def _opts(budget: int | None) -> dict[str, Any]:
    opts = dict(LEGACY)
    if budget:
        opts["back_edge_budget"] = {BACK_EDGE: budget}
    return opts


def _grade(grade: str, feedback: str) -> str:
    return json.dumps({"grade": grade, "feedback": feedback})


# The recorded model outputs (feedback texts shortened).
FLOWER_HEADLINES = [
    '"Petal Power: The Timeless Allure of Flowers"',
    "AI-Powered Petals: The Tech Revolution Blooming in Modern Floriculture",
]
FLOWER_FEEDBACK = "Consider AI for plant recognition or robotics in gardening."
FLOWER_GRADES = [
    _grade("unrelated", FLOWER_FEEDBACK),
    _grade("tech-related", "This headline is strongly tech-related."),
]
COMPUTER_HEADLINES = ["Computers: Shaping Our World"]
COMPUTER_GRADES = [_grade("tech-related", "Clearly tech-related.")]

TRACES = {
    "flower": (FLOWER_HEADLINES, FLOWER_GRADES),
    "computer": (COMPUTER_HEADLINES, COMPUTER_GRADES),
}


def make(
    headlines: list[str], grades: list[str], fakes: list[dict[str, ScriptedLlm]] | None = None
) -> Workflow:
    wf = importlib.reload(sample).root_agent
    models = {
        "generate_headline": ScriptedLlm.of(*(text(h) for h in headlines)),
        "evaluate_headline": ScriptedLlm.of(*(text(g) for g in grades)),
    }
    for a in llm_agents(wf):
        a.model = models[a.name]
    if fakes is not None:
        fakes.append(models)
    return wf


def _text(e: Any) -> str:
    if not e.content or not e.content.parts:
        return ""
    return "".join(p.text or "" for p in e.content.parts)


def signature(r: Run) -> list[tuple[Any, ...]]:
    return [
        (
            e.author,
            e.node_info.path,
            _text(e),
            e.actions.route,
            dict(e.actions.state_delta),
            e.output,
            tuple(e.node_info.output_for or ()),
        )
        for e in r.events
    ]


def _expected_paths(rounds: int) -> list[tuple[str, str]]:
    paths = [("root_agent", "root_agent@1/process_input@1")]
    for i in range(1, rounds + 1):
        paths += [
            ("generate_headline", f"root_agent@1/generate_headline@{i}"),
            ("evaluate_headline", f"root_agent@1/evaluate_headline@{i}"),
            ("root_agent", f"root_agent@1/route_headline@{i}"),
        ]
    return paths


# -- a. the fakes reproduce ADK's recorded traces --------------------------------


@pytest.mark.parametrize("topic", sorted(TRACES))
async def test_native_run_reproduces_recorded_trace(topic: str) -> None:
    headlines, grades = TRACES[topic]
    native = await run(make(headlines, grades), [topic])
    assert [(e.author, e.node_info.path) for e in native.events] == _expected_paths(len(grades))
    assert native.events[0].actions.state_delta == {"topic": topic}
    routes = [e.actions.route for e in native.events if e.actions.route]
    assert routes == [json.loads(g)["grade"] for g in grades]
    assert [t for t in native.texts if not t.startswith("{")] == headlines
    assert native.state == {"topic": topic, "feedback": json.loads(grades[-1])}
    assert native.final_output == json.loads(grades[-1])


# -- b. compiled run vs native run --------------------------------------------------


@pytest.mark.parametrize("budget", [None, 3])
@pytest.mark.parametrize("topic", sorted(TRACES))
async def test_compiled_run_matches_native(orchestrator, topic: str, budget: int | None) -> None:  # type: ignore[no-untyped-def]
    headlines, grades = TRACES[topic]
    fakes: list[dict[str, ScriptedLlm]] = []
    native, petri, _ = await run_both(
        lambda: make(headlines, grades, fakes), [topic], orchestrator, **_opts(budget)
    )
    assert signature(petri) == signature(native)
    assert petri.final_output == native.final_output == json.loads(grades[-1])
    assert petri.authors == native.authors
    assert petri.texts == native.texts
    assert petri.state == native.state
    # The retry's instruction carries the first round's feedback from state.
    native_fakes, petri_fakes = fakes
    for models in (native_fakes, petri_fakes):
        reqs = models["generate_headline"].requests
        assert len(reqs) == len(headlines)
        if len(reqs) > 1:
            assert FLOWER_FEEDBACK in str(reqs[1].config.system_instruction)
    assert [
        str(r.config.system_instruction) for r in petri_fakes["generate_headline"].requests
    ] == [str(r.config.system_instruction) for r in native_fakes["generate_headline"].requests]


async def _run_capturing(node: Any, text: str) -> tuple[list[Any], BaseException | None]:
    runner = InMemoryRunner(node=node, app_name="app")
    session = await runner.session_service.create_session(app_name="app", user_id="u")
    events: list[Any] = []
    try:
        async for e in runner.run_async(user_id="u", session_id=session.id, new_message=msg(text)):
            events.append(e)
    except Exception as err:
        return events, err
    return events, None


async def test_spent_back_edge_budget_fails_the_run_with_a_typed_error(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """Budget 1 against two "unrelated" grades: the second loop-back is refused.

    ADK bounds nothing and would ask the model a third time; the budget is the
    caller's opt-in bound (ADR 0007), so this is the documented difference.
    The failure itself is ADK-shaped: ``Runner.run_async`` raises it, and the
    workflow node's runner records one error event for ``root_agent@1``.
    """
    headlines = ["h1", "h2", "h3"]
    grades = [
        _grade("unrelated", "more tech"),
        _grade("unrelated", "still more tech"),
        _grade("tech-related", "ok"),
    ]
    native = await run(make(headlines, grades), ["flower"])
    assert len([e for e in native.events if e.author == "generate_headline"]) == 3
    cw = compile_workflow(make(headlines, grades), **_opts(1))
    events, err = await _run_capturing(
        PetriWorkflow.from_compiled(cw, orchestrator=orchestrator), "flower"
    )
    assert isinstance(err, LoopBudgetExhausted)
    assert str(err) == "back edge route_headline->generate_headline exhausted its budget of 1"
    # Two full rounds, event for event as ADK runs them, then the error event.
    assert signature(Run(events=events[:-1])) == signature(Run(events=native.events[:7]))
    assert len([e for e in events if e.author == "generate_headline"]) == 2
    errors = [e for e in events if e.error_code]
    assert errors == [events[-1]]
    assert events[-1].error_code == "LoopBudgetExhausted"
    assert events[-1].author == "root_agent"
    assert events[-1].node_info.path == "root_agent@1"


# -- c. report and proofs ------------------------------------------------------------


def _cycle_findings(cw: Any) -> list[tuple[str, str]]:
    return [(f.severity, f.subject) for f in cw.report.findings if "->" in f.subject]


def test_default_compile_rejects_the_instruction_state_reads() -> None:
    with pytest.raises(
        WorkflowTranslationError,
        match=r"generate_headline: reads session state "
        r"\(instruction \{topic\}, instruction \{feedback\}\)",
    ) as info:
        compile_workflow(make([], []))
    assert [f.subject for f in info.value.report.rejected] == ["generate_headline"]


def test_report_without_budget_lists_the_unbudgeted_cycle() -> None:
    cw = compile_workflow(make([], []), **LEGACY)
    assert not cw.report.rejected
    assert _cycle_findings(cw) == [
        ("approximated", "generate_headline->evaluate_headline->route_headline->generate_headline")
    ]
    assert {f.subject for f in cw.report.of("opaque")} == {
        "generate_headline",
        "evaluate_headline",
    }
    approximated = {(f.subject, f.message) for f in cw.report.of("approximated")}
    assert (
        "generate_headline",
        "reads legacy session state (instruction {topic}, instruction {feedback})",
    ) in approximated
    exact = {(f.subject, f.message) for f in cw.report.of("exact")}
    assert any(s == "route_headline" and "wf/route_headline/unmatched" in m for s, m in exact)
    assert [p.name for p in cw.unmatched_places()] == ["wf/route_headline/unmatched"]
    # Every node has an out edge: no terminal node, so the run has no output.
    assert cw.terminal_nodes == []


def test_report_with_budget_bounds_the_cycle() -> None:
    cw = compile_workflow(make([], []), **_opts(3))
    assert _cycle_findings(cw) == [
        ("exact", "generate_headline->evaluate_headline->route_headline->generate_headline"),
        ("exact", "route_headline->generate_headline"),
    ]
    assert (
        "exact",
        "route_headline->generate_headline",
        "back edge bounded by a budget of 3 per workflow run",
    ) in {(f.severity, f.subject, f.message) for f in cw.report.findings}


NODES = ("process_input", "generate_headline", "evaluate_headline", "route_headline")


@requires_z3
@pytest.mark.parametrize("budget", [None, 3])
def test_proofs(budget: int | None) -> None:
    cw = compile_workflow(make([], []), **_opts(budget))
    by_kind: dict[str, dict[str, str]] = {}
    for p in verify_workflow(cw, k=1):
        by_kind.setdefault(p.kind, {})[p.label] = p.result.verdict
    # "tech-related" has no edge on purpose: it is the loop's exit, so the
    # lint's no-match sink is reachable (a lint finding, not a safety violation).
    assert by_kind["route coverage"] == {
        "route coverage: unreachable(['wf/route_headline/unmatched'])": "violated"
    }
    assert by_kind["deadlock"] == {"deadlock_free": "proven"}
    # No terminal node, so no terminal-output claim.
    assert set(by_kind["safety"]) == {
        "one turn at a time: place_bound(turnActive, 1)",
        "permit never doubles: place_bound(turnPermit, 1)",
        *(f"{n} runs serially: place_bound({n}/idle, 1)" for n in NODES),
    }
    assert all(v == "proven" for v in by_kind["safety"].values()), by_kind["safety"]
