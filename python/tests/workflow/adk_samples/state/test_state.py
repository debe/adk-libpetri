"""ADK sample ``workflows/state``: four ways to write and read session state.

Recorded trace ``tests/go.json`` (user says ``go``): ``process_initial_input``
writes ``original_text`` via ``ctx.state``, ``update_state_via_event`` yields a
state delta, ``read_state_via_ctx`` reads both via ``ctx.state`` and writes
``appended_text``, and ``read_state_via_param`` gets ``appended_text`` by
parameter injection. No model.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

import pytest
from google.adk.workflow import Workflow

from adk_libpetri.workflow import WorkflowTranslationError, compile_workflow, verify_workflow
from support.smt_proofs import requires_z3

from .._harness import Run, run, run_both
from . import agent

FINAL = "Final Result: GO (Original was: go)!"
STATE = {
    "original_text": "go",
    "uppercased_text": "GO",
    "appended_text": "GO (Original was: go)",
}


def make() -> Workflow:
    return importlib.reload(agent).root_agent


def outputs(r: Run) -> list[tuple[str, Any]]:
    """Every output event as (node path, output), in order."""
    return [(e.node_info.path, e.output) for e in r.events if e.output is not None]


def shapes(r: Run) -> list[tuple[Any, ...]]:
    """Every event as (author, node path, output, output_for, state delta)."""
    return [
        (
            e.author,
            e.node_info.path if e.node_info else None,
            e.output,
            e.node_info.output_for if e.node_info else None,
            dict(e.actions.state_delta) if e.actions and e.actions.state_delta else None,
        )
        for e in r.events
    ]


def deltas(r: Run) -> list[dict[str, Any]]:
    return [dict(e.actions.state_delta) for e in r.events if e.actions and e.actions.state_delta]


async def test_native_run_reproduces_the_recorded_trace() -> None:
    r = await run(make(), ["go"])
    assert r.final_output == FINAL
    assert r.authors == ["state_sample"]
    assert outputs(r) == [
        ("state_sample@1/process_initial_input@1", "go"),
        ("state_sample@1/read_state_via_ctx@1", "GO (Original was: go)"),
        ("state_sample@1/read_state_via_param@1", FINAL),
    ]
    assert deltas(r) == [
        {"original_text": "go"},
        {"uppercased_text": "GO"},
        {"appended_text": "GO (Original was: go)"},
    ]
    assert r.state == STATE


def test_default_compile_rejects_the_injected_state_parameter() -> None:
    """Documented rule: a FunctionNode reading session state needs
    state='legacy_read' (commitment 2)."""
    with pytest.raises(
        WorkflowTranslationError,
        match=re.escape("read_state_via_param: reads session state (parameter appended_text)"),
    ):
        compile_workflow(make())


async def test_compiled_run_matches_native_with_legacy_read(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(make, ["go"], orchestrator, state="legacy_read")
    assert petri.final_output == native.final_output == FINAL
    assert petri.authors == native.authors
    assert petri.texts == native.texts
    assert deltas(petri) == deltas(native)
    assert petri.state == native.state == STATE


async def test_compiled_run_emits_the_same_output_events(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """ADK delegates the terminal node's output to the workflow (output_for
    names both paths) and emits no second event; neither does the compiled
    workflow, whose terminal node runs with use_as_output."""
    native, petri, _ = await run_both(make, ["go"], orchestrator, state="legacy_read")
    assert outputs(petri) == outputs(native)
    assert shapes(petri) == shapes(native)


def test_report_with_legacy_read() -> None:
    cw = compile_workflow(make(), state="legacy_read")
    approx = {(f.subject, f.message) for f in cw.report.of("approximated")}
    assert (
        "read_state_via_param",
        "reads legacy session state (parameter appended_text)",
    ) in approx
    assert (
        "read_state_via_ctx",
        "reads legacy session state (ctx.state['original_text'], ctx.state['uppercased_text'])",
    ) in approx
    assert not cw.report.rejected
    exact = {f.subject for f in cw.report.of("exact")}
    assert {"process_initial_input", "update_state_via_event", "read_state_via_ctx"} <= exact
    opaque = {f.subject for f in cw.report.of("opaque")}
    assert opaque == {"process_initial_input", "read_state_via_ctx"}  # the ctx-taking nodes


def test_ctx_state_reader_is_rejected_without_legacy_read() -> None:
    """``read_state_via_ctx`` reads ``ctx.state`` (commitment 2's case exactly),
    so a graph without the parameter-injected reader is still rejected under
    the default state='reject'; the writer ``process_initial_input`` is not."""
    m = importlib.reload(agent)
    wf = Workflow(
        name="ctx_state_only",
        edges=[
            (
                "START",
                m.process_initial_input,
                m.update_state_via_event,
                m.read_state_via_ctx,
            )
        ],
    )
    with pytest.raises(WorkflowTranslationError) as err:
        compile_workflow(wf)
    assert (
        "read_state_via_ctx: reads session state "
        "(ctx.state['original_text'], ctx.state['uppercased_text'])"
    ) in str(err.value)
    assert "process_initial_input" not in str(err.value)


@requires_z3
@pytest.mark.timeout(300)
def test_every_safety_claim_and_deadlock_freedom_is_proven() -> None:
    cw = compile_workflow(make(), state="legacy_read")
    proofs = verify_workflow(cw, k=1)
    verdicts = {p.label: p.result.verdict for p in proofs}
    assert {p.kind for p in proofs} == {"safety", "deadlock"}
    assert "deadlock_free" in verdicts
    assert (
        "read_state_via_param keeps one output: "
        "place_bound(read_state_via_param/terminalOutput, 1)" in verdicts
    )
    assert len(verdicts) == 3 + 4 + 1  # turn, permit, terminal; 4 idles; deadlock
    assert all(v == "proven" for v in verdicts.values()), verdicts
