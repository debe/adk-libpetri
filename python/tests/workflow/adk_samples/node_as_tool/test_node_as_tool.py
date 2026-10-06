"""ADK sample ``workflows/node_as_tool``: a ``Workflow`` and a node as agent tools.

The root is an ``Agent`` (``customer_service_agent``), not a ``Workflow``. Its
tools are ``customer_lookup_workflow`` (a one-node ``Workflow`` with
``input_schema=CustomerLookupArgs``) and ``calculate_discount`` (a ``@node``
that raises ``RequestInput`` for VIP tiers). The sample wraps the agent in an
``App`` with resumability on; the tests drive that ``App``.

The recorded trace (``tests/go.json``) is two turns: the question, which ends
on the VIP confirmation interrupt, and the ``yes`` answer, after which the
discount node outputs ``20% off`` and the agent summarises. The model's three
responses below are the trace's.

``compile_workflow`` applies to a ``Workflow``, so the root is refused with a
``TypeError``. The one ``Workflow`` in the sample is the lookup tool; the tests
compile it alone (report, proofs) and then swap the compiled ``PetriWorkflow``
into the agent's tools in place of the original, which is what "drop-in for a
``Workflow``" promises. ``PetriWorkflow`` keeps the Workflow's
``input_schema``/``output_schema`` and hands the tool args to the node
unchanged, so the run matches native event for event, except for the lookup
Workflow's own resumability checkpoints (strict xfail below). The sample's own
files are not changed for that: the swap is done on the reloaded module's agent.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

import pytest

from adk_libpetri.workflow import (
    PetriWorkflow,
    compile_workflow,
    verify_workflow,
)
from support.fake_llm import ScriptedLlm, call, text
from support.smt_proofs import requires_z3

from .._harness import Run, function_response, msg

QUESTION = "What discount does customer c123 get?"
TIER = "Verified VIP Member"
SUMMARY = "Customer c123 is a Verified VIP Member and gets a 20% discount."
LOOKUP = {"user_id": "c123", "tier": TIER}


def _sample() -> tuple[ModuleType, ScriptedLlm]:
    from . import agent

    mod = importlib.reload(agent)
    fake = ScriptedLlm.of(
        call("customer_lookup_workflow", {"user_id": "c123"}),
        call("calculate_discount", {"tier": TIER}),
        text(SUMMARY),
    )
    mod.root_agent.model = fake
    return mod, fake


def _answer_yes(events: list[Any]) -> Any:
    for e in events:
        for p in e.content.parts if e.content and e.content.parts else []:
            if p.function_call and p.function_call.name == "adk_request_input":
                return function_response(p.function_call.id, p.function_call.name, {"text": "yes"})
    raise AssertionError("no adk_request_input interrupt in the first turn")


async def _run_app(app: Any) -> Run:
    """Both recorded turns through the sample's own resumable ``App``."""
    from google.adk.runners import InMemoryRunner

    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id="u")
    result = Run()
    for turn in (msg(QUESTION), _answer_yes):
        message = turn(result.events) if callable(turn) else turn
        events = [
            e
            async for e in runner.run_async(user_id="u", session_id=session.id, new_message=message)
        ]
        result.turns.append(events)
        result.events.extend(events)
    return result


def _tool_responses(fake: ScriptedLlm) -> list[Any]:
    """The function responses the model saw in its last request."""
    last = fake.requests[-1]
    return [
        (p.function_response.name, p.function_response.response)
        for c in last.contents
        for p in c.parts or []
        if p.function_response
    ]


def _shape(events: list[Any]) -> list[tuple[Any, ...]]:
    """Each event as (author, node path, output_for, output, text, function
    calls/responses, agent_state, end_of_agent); ids and branch suffixes are
    per run and left out."""
    rows = []
    for e in events:
        parts = e.content.parts if e.content and e.content.parts else []
        rows.append(
            (
                e.author,
                e.node_info.path if e.node_info else None,
                e.node_info.output_for if e.node_info else None,
                e.output,
                "".join(p.text or "" for p in parts),
                [p.function_call.name for p in parts if p.function_call]
                + [p.function_response.name for p in parts if p.function_response],
                e.actions.agent_state if e.actions else None,
                bool(e.actions and e.actions.end_of_agent),
            )
        )
    return rows


def _is_workflow_checkpoint(row: tuple[Any, ...]) -> bool:
    """A resumability checkpoint of the lookup Workflow itself (the agent's own
    ``end_of_agent`` is in both runs)."""
    return row[0] == "customer_lookup_workflow" and (row[6] is not None or row[7])


def _swap_in_compiled(mod: ModuleType, orchestrator: Any) -> PetriWorkflow:
    compiled = compile_workflow(mod.customer_lookup_workflow)
    node = PetriWorkflow.from_compiled(compiled, orchestrator=orchestrator)
    mod.root_agent.tools = [node, mod.calculate_discount]
    return node


async def test_native_run_reproduces_the_recorded_trace() -> None:
    mod, fake = _sample()
    native = await _run_app(mod.app)
    first, second = native.turns
    assert native.authors == [
        "calculate_discount",
        "customer_lookup_workflow",
        "customer_service_agent",
    ]
    interrupt = _answer_yes(first).parts[0].function_response
    asks = [
        p.function_call.args["message"]
        for e in first
        for p in (e.content.parts if e.content and e.content.parts else [])
        if p.function_call and p.function_call.id == interrupt.id
    ]
    assert asks == [f"Apply VIP discount for tier '{TIER}'?"]
    assert [e.output for e in first if e.output is not None] == [LOOKUP]
    assert [e.output for e in second if e.output is not None] == ["20% off"]
    assert native.texts[-1] == SUMMARY
    assert len(fake.requests) == 3
    assert _tool_responses(fake) == [
        ("customer_lookup_workflow", LOOKUP),
        ("calculate_discount", {"result": "20% off"}),
    ]


def test_an_agent_root_is_rejected_with_a_typed_error() -> None:
    """The root is an ``LlmAgent``: there is no graph to compile, and
    ``compile_workflow`` says so with a ``TypeError`` naming ``Workflow``."""
    mod, _ = _sample()
    with pytest.raises(TypeError, match="Workflow"):
        compile_workflow(mod.root_agent)


def test_lookup_workflow_translation_report() -> None:
    mod, _ = _sample()
    compiled = compile_workflow(mod.customer_lookup_workflow)
    report = compiled.report
    assert not report.rejected
    assert compiled.node_names == ["lookup_customer_data"]
    # lookup_customer_data(node_input, ctx): no state parameters.
    assert [(f.subject, f.message) for f in report.of("exact")] == [
        ("lookup_customer_data", "FunctionNode, run by ADK's node runner")
    ]
    assert {f.subject for f in report.of("approximated")} == {"branches", "event replay"}
    # It takes ``ctx``, so whatever it runs via ctx.run_node is unmodelled.
    assert [(f.subject, f.message) for f in report.of("opaque")] == [
        (
            "lookup_customer_data",
            "takes ctx: children it runs via ctx.run_node are ADK-scheduled inside"
            " this transition, and the proofs do not see them",
        )
    ]


@requires_z3
def test_lookup_workflow_proofs() -> None:
    mod, _ = _sample()
    proofs = {p.label: p for p in verify_workflow(compile_workflow(mod.customer_lookup_workflow))}
    assert set(proofs) == {
        "one turn at a time: place_bound(turnActive, 1)",
        "permit never doubles: place_bound(turnPermit, 1)",
        (
            "lookup_customer_data keeps one output:"
            " place_bound(lookup_customer_data/terminalOutput, 1)"
        ),
        "lookup_customer_data runs serially: place_bound(lookup_customer_data/idle, 1)",
        "deadlock_free",
    }
    assert all(p.proven for p in proofs.values()), {k: p.result.verdict for k, p in proofs.items()}
    assert proofs["deadlock_free"].kind == "deadlock"
    assert {p.kind for label, p in proofs.items() if label != "deadlock_free"} == {"safety"}


async def test_compiled_lookup_workflow_is_a_drop_in_tool(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """``PetriWorkflow.from_compiled`` keeps the Workflow's ``input_schema``, so
    ``NodeTool`` accepts it as a tool, and the tool's args reach
    ``lookup_customer_data`` unchanged (no ``str()`` Content wrapping)."""
    native_mod, native_fake = _sample()
    native = await _run_app(native_mod.app)

    mod, fake = _sample()
    node = _swap_in_compiled(mod, orchestrator)
    assert node.input_schema is mod.CustomerLookupArgs
    assert node.output_schema is mod.customer_lookup_workflow.output_schema
    petri = await _run_app(mod.app)
    errors = [e.error_message for e in petri.events if e.error_code]
    assert errors == [], errors
    assert (
        _tool_responses(fake)
        == _tool_responses(native_fake)
        == [
            ("customer_lookup_workflow", LOOKUP),
            ("calculate_discount", {"result": "20% off"}),
        ]
    )
    assert petri.texts == native.texts
    assert petri.authors == native.authors
    # Event for event, per turn, once ADK's resumability checkpoints are set
    # aside (see the next test).
    assert [_shape(t) for t in petri.turns] == [
        [row for row in _shape(t) if not _is_workflow_checkpoint(row)] for t in native.turns
    ]


@pytest.mark.xfail(
    strict=True,
    reason="PetriWorkflow emits no resumability checkpoints (agent_state, end_of_agent)",
)
async def test_compiled_lookup_workflow_records_resumability_checkpoints(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """The App is resumable, so ADK's ``Workflow`` records a node-status
    snapshot (``agent_state``) when ``lookup_customer_data`` starts and when it
    completes, and an ``end_of_agent`` marker on a clean finish
    (``_workflow.py:_emit_node_checkpoint`` / ``_emit_end_of_agent``, both
    gated on ``ic.is_resumable``). ``PetriWorkflow._run_impl`` (``agent.py``)
    resumes from the net's marking and never emits either, so the session
    misses three ``customer_lookup_workflow`` events per tool call."""
    native_mod, _ = _sample()
    native = await _run_app(native_mod.app)
    mod, _ = _sample()
    _swap_in_compiled(mod, orchestrator)
    petri = await _run_app(mod.app)
    assert [_shape(t) for t in petri.turns] == [_shape(t) for t in native.turns]
