"""ADK sample ``workflows/auth_api_key``: a FunctionNode gated by ``auth_config``.

Recorded trace: ``tests/go.json``. Turn 1 (``"go"``): ``fetch_weather`` pauses
with an ``adk_request_credential`` call (interrupt id ``wf_auth:<node path>``).
Turn 2: the client answers with ``{"result": "12345678"}``, ``fetch_weather``
reruns with the key and ``summarize`` prints the masked key. No real service:
the API key is any string.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from google.adk.workflow import Workflow

from adk_libpetri.workflow import compile_workflow, verify_workflow
from support.smt_proofs import requires_z3

from .._harness import Run, function_response, run, run_both
from . import agent

OUTPUT = {
    "city": "San Francisco",
    "temperature": "18C",
    "condition": "Sunny",
    "api_key_used": "1234****",
}
SUMMARY = "Weather for San Francisco: 18C, Sunny. (Authenticated with key: 1234****)"
INTERRUPT = "wf_auth:auth_api_key@1/fetch_weather@1"


def make() -> Workflow:
    return importlib.reload(agent).root_agent


def _credential_calls(events: list[Any]) -> list[Any]:
    return [
        p.function_call
        for e in events
        for p in (e.content.parts if e.content and e.content.parts else [])
        if p.function_call and p.function_call.name == "adk_request_credential"
    ]


def answer(events: list[Any]) -> Any:
    """The client's reply to the last credential request (as in the trace)."""
    fc = _credential_calls(events)[-1]
    return function_response(fc.id, fc.name, {"result": "12345678"})


TURNS = ["go", answer]


def _turn_view(r: Run) -> list[list[tuple[str, Any, list[str]]]]:
    """Per turn: (node path, output, texts) of each event that carries content or output."""
    view = []
    for events in r.turns:
        rows = []
        for e in events:
            texts = [p.text for p in (e.content.parts if e.content else []) if p.text]
            if e.output is not None or texts or _credential_calls([e]):
                rows.append((e.node_info.path, e.output, texts))
        view.append(rows)
    return view


async def test_native_run_reproduces_the_recorded_trace() -> None:
    r = await run(make(), TURNS)
    first, second = r.turns
    assert [fc.id for fc in _credential_calls(first)] == [INTERRUPT]
    assert first[-1].long_running_tool_ids == {INTERRUPT}
    assert [(e.node_info.path, e.output) for e in second if e.output is not None] == [
        ("auth_api_key@1/fetch_weather@1", OUTPUT)
    ]
    assert r.texts == [SUMMARY]
    assert r.authors == ["auth_api_key"]


def _events(r: Run) -> list[list[dict[str, Any]]]:
    """Per turn, each event as data, its per-run ids and timestamp dropped."""
    return [
        [e.model_dump(exclude={"id", "timestamp", "invocation_id"}, exclude_none=True) for e in t]
        for t in r.turns
    ]


OPTIONS = {"auto": {}, "named": {"interruptible": ["fetch_weather"]}}


def test_report() -> None:
    """``auth_config`` makes the node interruptible without naming it."""
    cw = compile_workflow(make())
    findings = [(f.severity, f.subject, f.message) for f in cw.report.findings]
    report = {(sev, subj): m for sev, subj, m in findings}
    assert (
        "exact",
        "fetch_weather",
        "requests credentials (auth_config): compiled interruptible",
    ) in findings
    assert ("exact", "fetch_weather", "FunctionNode, run by ADK's node runner") in findings
    # fetch_weather takes ctx: what it runs via ctx.run_node is opaque to the proofs.
    assert report[("opaque", "fetch_weather")].startswith("takes ctx:")
    assert ("exact", "summarize") in report
    assert not cw.report.rejected
    assert cw.interruptible_nodes == ["fetch_weather"]
    assert "Wf_fetch_weather_ResumeRun" in cw.spec.transition_names
    assert "Wf_EndTurnOutput_summarize" in cw.spec.transition_names


def test_naming_the_auth_node_adds_no_finding() -> None:
    named = compile_workflow(make(), interruptible=["fetch_weather"])
    assert not any("compiled interruptible" in f.message for f in named.report.findings)
    assert named.spec.transition_names == compile_workflow(make()).spec.transition_names


@pytest.mark.parametrize("opts", list(OPTIONS.values()), ids=list(OPTIONS))
async def test_compiled_first_turn_parks_like_native(opts: dict[str, Any], orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(make, ["go"], orchestrator, **opts)
    assert _turn_view(petri) == _turn_view(native)
    assert [fc.id for fc in _credential_calls(petri.events)] == [INTERRUPT]
    # No event of the workflow's own: the credential request ends the turn, as natively.
    assert petri.events[-1].long_running_tool_ids == {INTERRUPT}
    assert _events(petri) == _events(native)


@pytest.mark.parametrize("opts", list(OPTIONS.values()), ids=list(OPTIONS))
async def test_compiled_run_matches_native(opts: dict[str, Any], orchestrator) -> None:  # type: ignore[no-untyped-def]
    """The resume reruns ``fetch_weather@1`` (the interrupted run's id), so the
    auth gate finds the answer keyed by it."""
    native, petri, _ = await run_both(make, TURNS, orchestrator, **opts)
    assert petri.final_output == native.final_output == OUTPUT
    assert petri.texts == native.texts == [SUMMARY]
    assert _turn_view(petri) == _turn_view(native)
    assert [fc.id for fc in _credential_calls(petri.turns[1])] == []
    assert petri.state == native.state
    assert _events(petri) == _events(native)


async def test_compiled_second_run_starts_at_run_id_one(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """A new message after a finished run is a new workflow run: ADK's ids restart at @1."""
    turns = [*TURNS, "again", answer]
    native, petri, _ = await run_both(make, turns, orchestrator)
    assert [fc.id for fc in _credential_calls(petri.turns[2])] == [INTERRUPT]
    assert _events(petri) == _events(native)


@requires_z3
@pytest.mark.parametrize("opts", list(OPTIONS.values()), ids=list(OPTIONS))
def test_proofs(opts: dict[str, Any]) -> None:
    cw = compile_workflow(make(), **opts)
    proofs = verify_workflow(cw, k=1)
    verdicts = {p.label: p.result.verdict for p in proofs}
    assert all(v == "proven" for v in verdicts.values()), verdicts
    assert all(p.label.endswith("[on the match-free over-approximation]") for p in proofs)
    assert {p.kind for p in proofs} == {"safety"}  # no routes, no deadlock claim
    assert "deadlock_free" not in verdicts  # not claimed with interruptible nodes
    suffix = " [on the match-free over-approximation]"
    assert set(verdicts) == {
        "one turn at a time: place_bound(turnActive, 1)" + suffix,
        "permit never doubles: place_bound(turnPermit, 1)" + suffix,
        "summarize keeps one output: place_bound(summarize/terminalOutput, 1)" + suffix,
        "fetch_weather runs serially: place_bound(fetch_weather/idle, 1)" + suffix,
        "summarize runs serially: place_bound(summarize/idle, 1)" + suffix,
    }
