"""ADK sample ``workflows/multi_triggers``: a fan-out whose three outputs each trigger one node.

Recorded trace (``tests/go.json``, user turn ``go``): the branches output
``GO``, ``2`` and ``og``; ``send_message`` runs three times (``@1``..``@3``),
once per upstream output, on that upstream's branch, and emits one message
each, never an output. No model, no state.
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
TERMINAL_CLAIM = "send_message keeps one output: place_bound(send_message/terminalOutput, 1)"


def make(*, sequential: bool = False) -> Workflow:
    wf = importlib.reload(sample).root_agent
    if sequential:
        # What ADK's own replay runner does (agent_test_runner._make_nodes_sequential).
        wf.max_concurrency = 1
    return wf


def expected_texts(s: str) -> list[str]:
    return [f"Triggered for input: {v}" for v in (s.upper(), len(s), s[::-1])]


def rows(r: Run) -> list[tuple[Any, ...]]:
    """(author, branch, node path, output, text, error) per event."""
    out = []
    for e in r.events:
        parts = e.content.parts if e.content and e.content.parts else []
        text = "".join(p.text or "" for p in parts)
        path = e.node_info.path if e.node_info else None
        out.append((e.author, e.branch, path, json.dumps(e.output), text, e.error_code))
    return out


async def test_native_run_reproduces_the_recorded_trace() -> None:
    r = await run(make(), ["go"])
    assert rows(r) == [
        ("root_agent", "make_uppercase@1", "root_agent@1/make_uppercase@1", '"GO"', "", None),
        ("root_agent", "count_characters@1", "root_agent@1/count_characters@1", "2", "", None),
        ("root_agent", "reverse_string@1", "root_agent@1/reverse_string@1", '"og"', "", None),
        (
            "root_agent",
            "make_uppercase@1",
            "root_agent@1/send_message@1",
            "null",
            "Triggered for input: GO",
            None,
        ),
        (
            "root_agent",
            "count_characters@1",
            "root_agent@1/send_message@2",
            "null",
            "Triggered for input: 2",
            None,
        ),
        (
            "root_agent",
            "reverse_string@1",
            "root_agent@1/send_message@3",
            "null",
            "Triggered for input: og",
            None,
        ),
    ]
    assert r.final_output == "og"
    assert r.authors == ["root_agent"]


@pytest.mark.parametrize("text", SAMPLE_INPUTS)
async def test_compiled_run_matches_native(text: str, orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(make, [text], orchestrator)
    assert petri.authors == native.authors == ["root_agent"]
    # Under default concurrency the net completes the three branches in a
    # scheduling-dependent order (ADK's: edge order), so send_message's run ids
    # pair with different inputs. Compare what was said, as a multiset.
    assert Counter(petri.texts) == Counter(native.texts) == Counter(expected_texts(text))
    assert Counter(r[3] for r in rows(petri)) == Counter(r[3] for r in rows(native))
    assert all(r[5] is None for r in rows(petri))
    assert petri.state == native.state


@pytest.mark.parametrize("text", SAMPLE_INPUTS)
async def test_sequential_compiled_run_matches_native_event_for_event(
    text: str,
    orchestrator,  # type: ignore[no-untyped-def]
) -> None:
    """With ``max_concurrency=1`` (ADK replay mode) paths, branches and order match."""
    native, petri, _ = await run_both(lambda: make(sequential=True), [text], orchestrator)
    assert rows(petri) == rows(native)
    assert petri.texts == expected_texts(text)


def test_translation_report() -> None:
    cw = compile_workflow(make())
    report = cw.report
    assert not report.rejected
    assert {f.subject for f in report.of("exact")} == {
        "make_uppercase",
        "count_characters",
        "reverse_string",
        "send_message",
    }
    assert not report.of("opaque")
    assert {f.subject for f in report.of("approximated")} == {
        "fan-out order",
        "branches",
        "event replay",
    }
    plan = cw._plans["send_message"]
    assert plan.terminal and not plan.is_join


@requires_z3
def test_every_safety_claim_and_deadlock_freedom_are_proven() -> None:
    """send_message is terminal and triggered three times. Its terminal output
    place is reset on each run (last output wins, as ADK's ``node_outputs``),
    so it holds at most one token."""
    proofs = verify_workflow(compile_workflow(make()), k=1)
    verdicts = {p.label: p.result.verdict for p in proofs}
    assert {p.kind for p in proofs} == {"safety", "deadlock"}
    assert TERMINAL_CLAIM in verdicts
    assert len(verdicts) == 8  # 2 turn claims, 1 terminal, 4 serial nodes, deadlock freedom
    assert all(v == "proven" for v in verdicts.values()), verdicts


def with_returning_terminal() -> Workflow:
    """The sample's graph, with a terminal that returns its message as output."""
    m = importlib.reload(sample)

    def send_output(node_input: Any) -> str:
        return f"Triggered for input: {node_input}"

    return Workflow(
        name="root_agent",
        edges=[("START", (m.make_uppercase, m.count_characters, m.reverse_string), send_output)],
        input_schema=str,
        max_concurrency=1,
    )


async def test_native_adk_keeps_the_last_output_of_a_terminal_run_many_times() -> None:
    r = await run(with_returning_terminal(), ["go"])
    assert [e.error_code for e in r.events if e.error_code] == []
    assert r.final_output == "Triggered for input: og"


async def test_compiled_terminal_run_many_times_matches_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(with_returning_terminal, ["go"], orchestrator)
    assert [e.error_code for e in petri.events if e.error_code] == []
    assert petri.final_output == native.final_output == "Triggered for input: og"
    assert rows(petri) == rows(native)
