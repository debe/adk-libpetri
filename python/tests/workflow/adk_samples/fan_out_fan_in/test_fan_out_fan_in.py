"""ADK sample ``workflows/fan_out_fan_in``: three parallel nodes, a JoinNode, an aggregate.

Recorded trace (``tests/go.json``, user turn ``go``): the three branches output
``GO``, ``2`` and ``og``; ``join_for_results`` outputs their dict; ``aggregate``
emits one message and no output. No model, no state.
"""

from __future__ import annotations

import importlib
import json
from collections import Counter
from typing import Any

import pytest
from google.adk.workflow import Workflow

from adk_libpetri.workflow import compile_workflow, verify_workflow
from support.smt_proofs import requires_z3

from .._harness import Run, run, run_both
from . import agent as sample

SAMPLE_INPUTS = ["go", "Hello World", "ADK workflows", "testing concurrent nodes"]


def make(*, sequential: bool = False) -> Workflow:
    wf = importlib.reload(sample).root_agent
    if sequential:
        # What ADK's own replay runner does (agent_test_runner._make_nodes_sequential).
        wf.max_concurrency = 1
    return wf


def expected_text(s: str) -> str:
    return f"Uppercase: {s.upper()}\n\nCharacter Count: {len(s)}\n\nReversed: {s[::-1]}\n\n"


def _norm(output: Any) -> str:
    return json.dumps(output, sort_keys=True, default=str)


def rows(r: Run) -> list[tuple[Any, ...]]:
    """(author, branch, node path, output, text) per event."""
    out = []
    for e in r.events:
        text = (
            "".join(p.text or "" for p in e.content.parts) if e.content and e.content.parts else ""
        )
        path = e.node_info.path if e.node_info else None
        out.append((e.author, e.branch, path, _norm(e.output), text, e.error_code))
    return out


async def test_native_run_reproduces_the_recorded_trace() -> None:
    r = await run(make(), ["go"])
    assert sorted(rows(r), key=str) == sorted(
        [
            ("root_agent", "make_uppercase@1", "root_agent@1/make_uppercase@1", '"GO"', "", None),
            ("root_agent", "count_characters@1", "root_agent@1/count_characters@1", "2", "", None),
            ("root_agent", "reverse_string@1", "root_agent@1/reverse_string@1", '"og"', "", None),
            (
                "root_agent",
                None,
                "root_agent@1/join_for_results@1",
                _norm(r.final_output),
                "",
                None,
            ),
            ("root_agent", None, "root_agent@1/aggregate@1", "null", expected_text("go"), None),
        ],
        key=str,
    )
    assert r.final_output == {"make_uppercase": "GO", "count_characters": 2, "reverse_string": "og"}
    assert r.authors == ["root_agent"]
    assert r.texts == [expected_text("go")]


@pytest.mark.parametrize("text", SAMPLE_INPUTS)
async def test_compiled_run_matches_native(text: str, orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(make, [text], orchestrator)
    assert petri.final_output == native.final_output
    assert petri.authors == native.authors == ["root_agent"]
    assert petri.texts == native.texts == [expected_text(text)]
    # The three branches complete in a scheduling-dependent order in the net;
    # ADK's own replay comparator sorts events too, so compare as multisets.
    assert Counter(rows(petri)) == Counter(rows(native))
    assert petri.state == native.state


@pytest.mark.parametrize("text", SAMPLE_INPUTS)
async def test_sequential_compiled_run_matches_native_event_for_event(
    text: str,
    orchestrator,  # type: ignore[no-untyped-def]
) -> None:
    """With ``max_concurrency=1`` (ADK replay mode) the order matches exactly."""
    native, petri, _ = await run_both(lambda: make(sequential=True), [text], orchestrator)
    assert rows(petri) == rows(native)


async def test_two_turns_on_one_session_match_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(make, ["go", "Hello World"], orchestrator)
    assert [e.output for e in petri.turns[-1] if e.output is not None][-1] == {
        "make_uppercase": "HELLO WORLD",
        "count_characters": 11,
        "reverse_string": "dlroW olleH",
    }
    assert petri.texts == native.texts == [expected_text("go"), expected_text("Hello World")]
    # Run ids (node path @N, branch) restart per workflow run, as in ADK.
    assert Counter(rows(petri)) == Counter(rows(native))
    for turn in (0, 1):
        assert len(petri.turns[turn]) == len(native.turns[turn])


async def test_second_turn_node_paths_and_branches_match_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(make, ["go", "Hello World"], orchestrator)

    def ids(r: Run) -> set[tuple[Any, Any]]:
        return {
            (e.branch, e.node_info.path if e.node_info else None)
            for e in r.turns[-1]
            if e.author != "user"
        }

    assert ("make_uppercase@1", "root_agent@1/make_uppercase@1") in ids(native)
    assert ids(petri) == ids(native)


async def test_sequential_two_turns_match_native_event_for_event(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(
        lambda: make(sequential=True), ["go", "Hello World"], orchestrator
    )
    assert [rows(Run(events=t)) for t in petri.turns] == [rows(Run(events=t)) for t in native.turns]


def test_translation_report() -> None:
    cw = compile_workflow(make())
    report = cw.report
    assert not report.rejected
    exact = {(f.subject, f.message) for f in report.of("exact")}
    assert (
        "join_for_results",
        "join over ['make_uppercase', 'count_characters', 'reverse_string'] (consume semantics); "
        "its input dict is in edge order, where ADK's follows set iteration",
    ) in exact
    assert {f.subject for f in report.of("exact")} == {
        "make_uppercase",
        "count_characters",
        "reverse_string",
        "join_for_results",
        "aggregate",
    }
    assert [f.subject for f in report.of("opaque")] == ["join_for_results"]
    assert {f.subject for f in report.of("approximated")} == {
        "fan-out order",
        "branches",
        "event replay",
    }
    run_t = cw.spec.transition("Wf_join_for_results_Run")
    assert {i.place.name for i in run_t.inputs} >= {
        "wf/join_for_results/from/make_uppercase",
        "wf/join_for_results/from/count_characters",
        "wf/join_for_results/from/reverse_string",
    }


@requires_z3
@pytest.mark.parametrize("sequential", [False, True])
def test_every_safety_claim_and_deadlock_freedom_are_proven(sequential: bool) -> None:
    proofs = verify_workflow(compile_workflow(make(sequential=sequential)), k=1)
    verdicts = {p.label: p.result.verdict for p in proofs}
    assert {p.kind for p in proofs} == {"safety", "deadlock"}  # no route: no coverage lint
    assert "deadlock_free" in verdicts
    # 2 turn claims, aggregate's terminal output, 5 serial nodes, deadlock freedom.
    assert len(verdicts) == 9
    assert "aggregate keeps one output: place_bound(aggregate/terminalOutput, 1)" in verdicts
    assert all(v == "proven" for v in verdicts.values()), verdicts
