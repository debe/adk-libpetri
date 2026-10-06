"""ADK sample ``workflows/loop_self``: a node routes back to itself until it guesses right.

Recorded trace: ``tests/3.json`` (target 3, ``random.randint`` mocked to
10, 1, 0, 4, 3). ``guess_number(target_number: int)`` binds ``target_number``
from ``ctx.state`` (ADK's default ``parameter_binding="state"``), so the
compiler rejects the sample by default and compiles it under
``state="legacy_read"``. The loop's exit is the run that yields no route:
ADK logs "none were matched ... The branch will end", and the net takes its
``wf/guess_number/unmatched`` branch.
"""

from __future__ import annotations

import importlib
import random
from collections.abc import Callable, Iterator
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
from support.smt_proofs import requires_z3

from .._harness import Run, msg, run, run_both
from . import agent as sample

SELF_EDGE = ("guess_number", "guess_number")
RECORDED_GUESSES = [10, 1, 0, 4, 3]
LEGACY: dict[str, Any] = {"state": "legacy_read"}


def maker(monkeypatch: pytest.MonkeyPatch, guesses: list[int]) -> Callable[[], Workflow]:
    """``make`` for run_both: a fresh module and a fresh ``random.randint`` script per run."""

    def make() -> Workflow:
        it: Iterator[int] = iter(list(guesses))
        monkeypatch.setattr(random, "randint", lambda a, b: next(it))
        return importlib.reload(sample).root_agent

    return make


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
            e.error_code,
        )
        for e in r.events
    ]


def _recorded_texts(guesses: list[int]) -> list[str]:
    return [f"Guessing {g}..." for g in guesses] + ["Correct!"]


# -- a. the mock reproduces ADK's recorded trace ------------------------------------


async def test_native_run_reproduces_recorded_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    native = await run(maker(monkeypatch, RECORDED_GUESSES)(), ["3"])
    assert native.events[0].actions.state_delta == {"target_number": 3}
    assert native.texts == _recorded_texts(RECORDED_GUESSES)
    paths = [e.node_info.path for e in native.events]
    assert paths[0] == "root_agent@1/validate_input@1"
    assert paths[-1] == "root_agent@1/guess_number@5"
    assert [e.actions.route for e in native.events if e.actions.route] == ["guessed_wrong"] * 4
    assert native.state == {"target_number": 3}
    assert native.final_output is None


# -- b. compiled run vs native run ---------------------------------------------------


@pytest.mark.parametrize("budget", [None, 4, 10])
async def test_compiled_run_matches_native(
    orchestrator,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    budget: int | None,
) -> None:
    opts = dict(LEGACY)
    if budget:
        opts["back_edge_budget"] = {SELF_EDGE: budget}
    native, petri, _ = await run_both(
        maker(monkeypatch, RECORDED_GUESSES), ["3"], orchestrator, **opts
    )
    assert signature(petri) == signature(native)
    assert petri.texts == native.texts == _recorded_texts(RECORDED_GUESSES)
    assert petri.authors == native.authors == ["root_agent"]
    assert petri.state == native.state == {"target_number": 3}
    assert petri.final_output is native.final_output is None


async def test_two_turns_on_one_session_match_native(
    orchestrator,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second turn reads the second turn's state, and the net serves both."""
    guesses = [*RECORDED_GUESSES, 9, 7]
    native, petri, _ = await run_both(
        maker(monkeypatch, guesses), ["3", "7"], orchestrator, **LEGACY
    )
    assert signature(petri) == signature(native)
    assert [_text(e) for e in petri.turns[1] if _text(e)] == [
        "Guessing 9...",
        "Guessing 7...",
        "Correct!",
    ]
    assert petri.state == native.state == {"target_number": 7}


async def test_second_turn_restarts_node_run_ids_like_adk(
    orchestrator,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guesses = [*RECORDED_GUESSES, 9, 7]
    native, petri, _ = await run_both(
        maker(monkeypatch, guesses), ["3", "7"], orchestrator, **LEGACY
    )
    assert [e.node_info.path for e in native.turns[1]] == [
        "root_agent@1/validate_input@1",
        "root_agent@1/guess_number@1",
        "root_agent@1/guess_number@1",
        "root_agent@1/guess_number@2",
        "root_agent@1/guess_number@2",
    ]
    assert [e.node_info.path for e in petri.turns[1]] == [e.node_info.path for e in native.turns[1]]


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


async def test_spent_self_loop_budget_fails_the_run_with_a_typed_error(
    orchestrator,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four wrong guesses against a budget of 3: the fourth loop-back is refused (ADR 0007).

    ADK bounds nothing; the budget is the caller's opt-in bound. The failure
    is ADK-shaped: ``Runner.run_async`` raises it, and the workflow node's
    runner records one error event for ``root_agent@1``.
    """
    make = maker(monkeypatch, RECORDED_GUESSES)
    native = await run(make(), ["3"])
    cw = compile_workflow(make(), back_edge_budget={SELF_EDGE: 3}, **LEGACY)
    events, err = await _run_capturing(
        PetriWorkflow.from_compiled(cw, orchestrator=orchestrator), "3"
    )
    assert isinstance(err, LoopBudgetExhausted)
    assert str(err) == "back edge guess_number->guess_number exhausted its budget of 3"
    assert [_text(e) for e in events if _text(e)] == [
        "Guessing 10...",
        "Guessing 1...",
        "Guessing 0...",
        "Guessing 4...",
    ]
    # validate_input plus four guesses (message + route each) as ADK runs them.
    assert signature(Run(events=events[:-1])) == signature(Run(events=native.events[:9]))
    assert [e for e in events if e.error_code] == [events[-1]]
    assert events[-1].error_code == "LoopBudgetExhausted"
    assert events[-1].node_info.path == "root_agent@1"


async def test_invalid_input_raises_like_adk(
    orchestrator,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing node fails the run as under ``Workflow``: the node's exception
    reaches ``Runner.run_async``, with ADK's own error event only."""
    make = maker(monkeypatch, [])
    native, native_err = await _run_capturing(make(), "11")
    node = PetriWorkflow.from_compiled(
        compile_workflow(make(), **LEGACY), orchestrator=orchestrator
    )
    petri, petri_err = await _run_capturing(node, "11")
    assert type(petri_err) is type(native_err) is ValueError
    assert str(petri_err) == str(native_err) == "Invalid input."
    assert [_text(e) for e in native] == ["Please provide a number between 0 and 10.", ""]
    assert native[-1].error_code == "ValueError"
    assert signature(Run(events=petri)) == signature(Run(events=native))
    assert [e.node_info.path for e in petri if e.error_code] == ["root_agent@1/validate_input@1"]


# -- c. report and proofs ---------------------------------------------------------------


def test_default_compile_rejects_the_state_bound_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        WorkflowTranslationError,
        match=r"guess_number: reads session state \(parameter target_number\)",
    ) as info:
        compile_workflow(maker(monkeypatch, [])())
    # validate_input binds node_input only: not a state read.
    assert [f.subject for f in info.value.report.rejected] == ["guess_number"]


def _opts(budget: int | None) -> dict[str, Any]:
    opts = dict(LEGACY)
    if budget:
        opts["back_edge_budget"] = {SELF_EDGE: budget}
    return opts


@pytest.mark.parametrize("budget", [None, 10])
def test_report_under_legacy_read(monkeypatch: pytest.MonkeyPatch, budget: int | None) -> None:
    cw = compile_workflow(maker(monkeypatch, [])(), **_opts(budget))
    assert not cw.report.rejected
    findings = {(f.severity, f.subject, f.message) for f in cw.report.findings}
    assert (
        "approximated",
        "guess_number",
        "reads legacy session state (parameter target_number)",
    ) in findings
    cycle = [(f.severity, f.subject) for f in cw.report.findings if "->" in f.subject]
    if budget:
        assert cycle == [("exact", "guess_number->guess_number")] * 2
        assert (
            "exact",
            "guess_number->guess_number",
            f"back edge bounded by a budget of {budget} per workflow run",
        ) in findings
    else:
        assert cycle == [("approximated", "guess_number->guess_number")]
    assert any(
        sev == "exact" and subj == "guess_number" and "wf/guess_number/unmatched" in m
        for sev, subj, m in findings
    )
    assert [p.name for p in cw.unmatched_places()] == ["wf/guess_number/unmatched"]
    assert cw.terminal_nodes == []


@requires_z3
@pytest.mark.parametrize("budget", [None, 10])
def test_proofs(monkeypatch: pytest.MonkeyPatch, budget: int | None) -> None:
    cw = compile_workflow(maker(monkeypatch, [])(), **_opts(budget))
    by_kind: dict[str, dict[str, str]] = {}
    for p in verify_workflow(cw, k=1):
        by_kind.setdefault(p.kind, {})[p.label] = p.result.verdict
    # "Correct!" yields no route: the lint's no-match sink is the loop's exit.
    assert by_kind["route coverage"] == {
        "route coverage: unreachable(['wf/guess_number/unmatched'])": "violated"
    }
    assert by_kind["deadlock"] == {"deadlock_free": "proven"}
    # guess_number routes back to itself: no terminal node, no terminal claim.
    assert set(by_kind["safety"]) == {
        "one turn at a time: place_bound(turnActive, 1)",
        "permit never doubles: place_bound(turnPermit, 1)",
        "validate_input runs serially: place_bound(validate_input/idle, 1)",
        "guess_number runs serially: place_bound(guess_number/idle, 1)",
    }
    assert all(v == "proven" for v in by_kind["safety"].values()), by_kind["safety"]
