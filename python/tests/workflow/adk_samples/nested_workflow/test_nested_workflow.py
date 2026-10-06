"""ADK sample ``workflows/nested_workflow``: a nested Workflow beside an agent, then a join.

Recorded trace: ``tests/1984.json`` (one turn, ``"1984"``).
"""

from __future__ import annotations

import importlib
import json
import re
from typing import Any

import pytest
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow

from adk_libpetri.workflow import (
    PetriWorkflow,
    WorkflowTranslationError,
    compile_workflow,
    verify_workflow,
)
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import llm_agents, msg, run, run_both
from . import agent

NAME = "Scarlett Johansson"
BIO = (
    "Scarlett Johansson is an acclaimed actress renowned for her distinctive voice and "
    "versatile performances across a wide range of genres."
)
EVENT = (
    "In December 1984, the Union Carbide chemical plant in Bhopal, India, experienced a "
    "catastrophic gas leak, releasing deadly methyl isocyanate."
)
COMBINED = f"# Year: 1984\n\n## Famous Person Bio:\n\n{BIO}\n\n## Historical Event:\n\n{EVENT}"
SCRIPT = {"find_name": NAME, "generate_bio": BIO, "find_historical_event": EVENT}


def make() -> Workflow:
    """A fresh copy of the sample's graph, every LlmAgent on its own scripted fake."""
    module = importlib.reload(agent)
    wf = module.root_agent
    for a in llm_agents(wf):
        a.model = ScriptedLlm.of(text(SCRIPT[a.name]))
    return wf


def _requests(wf: Workflow) -> dict[str, list[Any]]:
    return {a.name: a.model.requests for a in llm_agents(wf)}  # type: ignore[union-attr]


def _system(req: Any) -> str:
    return str(req.config.system_instruction or "")


async def test_native_run_reproduces_the_recorded_trace() -> None:
    wf = make()
    r = await run(wf, ["1984"])
    assert r.state["year"] == "1984"
    # aggregate_results yields a message, not an output: the last output is the join's.
    assert r.final_output == {"find_famous_person": BIO, "find_historical_event": EVENT}
    assert r.texts[-1] == COMBINED
    assert r.authors == ["find_historical_event", "find_name", "generate_bio", "root_agent"]
    reqs = _requests(wf)
    # {year} in the instructions is filled from session state (process_input's delta).
    assert "born in this year: 1984" in _system(reqs["find_name"][0])
    assert "occurred in this year: 1984" in _system(reqs["find_historical_event"][0])


def test_reading_year_from_state_is_rejected_by_default() -> None:
    """``aggregate_results(node_input, year)`` reads session state: commitment 2."""
    with pytest.raises(WorkflowTranslationError, match=r"aggregate_results: reads session state"):
        compile_workflow(make())


def test_report_with_legacy_state_reads() -> None:
    cw = compile_workflow(make(), state="legacy_read")
    report = {(f.severity, f.subject): f.message for f in cw.report.findings}
    assert (
        report[("approximated", "aggregate_results")]
        == "reads legacy session state (parameter year)"
    )
    # "{year}" in the agents' instructions is a state read too, nested ones included.
    assert (
        report[("approximated", "find_historical_event")]
        == "reads legacy session state (instruction {year})"
    )
    assert (
        report[("approximated", "find_famous_person/find_name")]
        == "reads legacy session state (instruction {year})"
    )
    assert ("approximated", "find_famous_person/generate_bio") not in report
    assert ("approximated", "process_input") not in report  # node_input only
    assert ("exact", "process_input") in report
    assert report[("opaque", "find_famous_person")].startswith("nested Workflow")
    assert ("opaque", "find_historical_event") in report
    assert ("approximated", "fan-out order") in report
    assert "join_for_aggregation" in str(cw.report)
    assert set(cw.node_names) == {
        "process_input",
        "find_famous_person",
        "find_historical_event",
        "join_for_aggregation",
        "aggregate_results",
    }


def test_every_state_read_is_rejected_by_default() -> None:
    with pytest.raises(WorkflowTranslationError) as err:
        compile_workflow(make())
    rejected = {f.subject for f in err.value.report.rejected}
    assert rejected == {
        "aggregate_results",
        "find_historical_event",
        "find_famous_person/find_name",
    }


async def test_compiled_run_matches_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(make, ["1984"], orchestrator, state="legacy_read")
    assert petri.final_output == native.final_output
    assert petri.authors == native.authors
    assert petri.texts == native.texts
    assert petri.state == native.state
    assert _trace(petri) == _trace(native)
    # Node run ids are ADK's own (one workflow run, each node once).
    assert _paths(petri.events) == _paths(native.events)
    # Every event as data (ids and timestamps aside); the workflow adds none of its own.
    assert sorted(_dumps(petri.events)) == sorted(_dumps(native.events))


def _dumps(events: list[Any]) -> list[str]:
    """Events as canonical JSON. Keys are sorted: ADK builds a join's input dict
    from a *set* of predecessor names, so its key order follows string hashing
    (it changes with PYTHONHASHSEED); the compiled join uses edge order."""
    return [
        json.dumps(
            e.model_dump(exclude={"id", "timestamp", "invocation_id"}, exclude_none=True),
            sort_keys=True,
            default=str,
        )
        for e in events
    ]


def _trace(r: Any) -> list[tuple[str, str | None, str, Any]]:
    """(author, branch, node path without run ids, output) per event, in order."""
    out = []
    for e in r.events:
        path = re.sub(r"@\d+", "", e.node_info.path or "") if e.node_info else ""
        out.append((e.author, e.branch, path, e.output))
    return out


@requires_z3
def test_proofs() -> None:
    cw = compile_workflow(make(), state="legacy_read")
    verdicts = {p.label: p.result.verdict for p in verify_workflow(cw, k=1)}
    assert all(v == "proven" for v in verdicts.values()), verdicts
    assert "deadlock_free" in verdicts


INVALID = "Please provide a valid 4-digit year (e.g., 1955)."


async def _turn_until_raise(node: Any, text_in: str) -> tuple[list[Any], BaseException | None]:
    runner = InMemoryRunner(node=node, app_name="app")
    s = await runner.session_service.create_session(app_name="app", user_id="u")
    events: list[Any] = []
    try:
        async for e in runner.run_async(user_id="u", session_id=s.id, new_message=msg(text_in)):
            events.append(e)
    except Exception as err:
        return events, err
    return events, None


async def test_native_invalid_year_emits_the_message_then_raises() -> None:
    events, err = await _turn_until_raise(make(), "hello")
    assert isinstance(err, ValueError) and str(err) == "Invalid year format."
    assert [e.content.parts[0].text for e in events if e.content] == [INVALID]
    assert [e.error_code for e in events if e.error_code] == ["ValueError"]


async def test_compiled_invalid_year_raises_like_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """A failing node fails the run as under ``Workflow``: ``run_async`` raises
    the node's own exception, after the same message and node error event."""
    native, native_err = await _turn_until_raise(make(), "hello")
    node = PetriWorkflow.from_compiled(
        compile_workflow(make(), state="legacy_read"), orchestrator=orchestrator
    )
    events, err = await _turn_until_raise(node, "hello")
    assert isinstance(err, ValueError) and str(err) == "Invalid year format."
    assert type(err) is type(native_err)
    assert [e.content.parts[0].text for e in events if e.content] == [INVALID]
    assert [(e.error_code, e.error_message) for e in events if e.error_code] == [
        ("ValueError", "Invalid year format.")
    ]
    assert _paths(events) == _paths(native)


def _paths(events: list[Any]) -> list[tuple[str, str]]:
    return [(e.author, e.node_info.path if e.node_info else "") for e in events]
