"""WF-*: a compiled workflow behaves like ADK's own ``Workflow`` under ``Runner``.

Each sample runs twice: natively (``InMemoryRunner(node=workflow)``) and as
``PetriWorkflow.from_workflow(workflow)``. Same final output, same authors of
the session's events.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow
from google.genai import types

from adk_libpetri.workflow import LoopBudgetExhausted, PetriWorkflow

from . import samples


def _msg(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


async def _run(
    runner: InMemoryRunner, text: str, session_id: str | None = None
) -> tuple[list[Any], str]:
    if session_id is None:
        s = await runner.session_service.create_session(app_name=runner.app_name, user_id="u")
        session_id = s.id
    events = [
        e
        async for e in runner.run_async(user_id="u", session_id=session_id, new_message=_msg(text))
    ]
    return events, session_id


def _final_output(events: list[Any]) -> Any:
    outs = [e.output for e in events if e.output is not None]
    return outs[-1] if outs else None


def _authors(events: list[Any]) -> list[str]:
    return sorted({e.author for e in events if e.author and e.author != "user"})


CASES: dict[str, tuple[Callable[[], Workflow], str, dict[str, Any]]] = {
    "linear": (samples.linear, "hello", {}),
    "router-bug": (samples.router, "a bug report", {}),
    "router-other": (samples.router, "a question", {}),
    "fan_join": (samples.fan_join, "MiXeD", {}),
    "concurrent": (samples.concurrent, "MiXeD", {}),
}


@pytest.mark.parametrize("case", sorted(CASES))
async def test_compiled_workflow_matches_native_run(case: str, orchestrator) -> None:  # type: ignore[no-untyped-def]
    make, text, opts = CASES[case]
    native_events, _ = await _run(InMemoryRunner(node=make(), app_name="native"), text)
    agent = PetriWorkflow.from_workflow(make(), orchestrator=orchestrator, **opts)
    compiled_events, _ = await _run(InMemoryRunner(node=agent, app_name="compiled"), text)
    assert _final_output(compiled_events) == _final_output(native_events)
    assert _authors(compiled_events) == _authors(native_events)


async def test_compiled_workflow_serves_many_turns_on_one_net(orchestrator) -> None:  # type: ignore[no-untyped-def]
    agent = PetriWorkflow.from_workflow(samples.router(), orchestrator=orchestrator)
    runner = InMemoryRunner(node=agent, app_name="turns")
    events, sid = await _run(runner, "first bug")
    assert _final_output(events) == "bug:first bug"
    events, _ = await _run(runner, "then a question", sid)
    assert _final_output(events) == "other:then a question"
    assert agent.registry.size() == 1


async def test_retry_recovers_like_adk(orchestrator) -> None:  # type: ignore[no-untyped-def]
    samples._calls["n"] = 0
    native, _ = await _run(InMemoryRunner(node=samples.retrying(), app_name="n"), "x")
    samples._calls["n"] = 0
    agent = PetriWorkflow.from_workflow(samples.retrying(), orchestrator=orchestrator)
    compiled, _ = await _run(InMemoryRunner(node=agent, app_name="c"), "x")
    assert _final_output(native) == _final_output(compiled) == "recovered"


async def test_budgeted_loop_runs_to_its_exit(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, _ = await _run(InMemoryRunner(node=samples.looping(), app_name="n"), "go")
    agent = PetriWorkflow.from_workflow(
        samples.looping(), orchestrator=orchestrator, back_edge_budget={("counter", "counter"): 5}
    )
    compiled, _ = await _run(InMemoryRunner(node=agent, app_name="c"), "go")
    assert _final_output(compiled) == _final_output(native) == "done(3)"


async def test_exhausted_back_edge_budget_fails_the_turn_with_a_typed_error(orchestrator) -> None:  # type: ignore[no-untyped-def]
    # As a failing Workflow does: ADK records an error event, then run_async raises.
    agent = PetriWorkflow.from_workflow(
        samples.looping(), orchestrator=orchestrator, back_edge_budget={("counter", "counter"): 1}
    )
    runner = InMemoryRunner(node=agent, app_name="c")
    s = await runner.session_service.create_session(app_name="c", user_id="u")
    events: list[Any] = []
    with pytest.raises(LoopBudgetExhausted, match="counter->counter"):
        async for e in runner.run_async(user_id="u", session_id=s.id, new_message=_msg("go")):
            events.append(e)
    assert events[-1].error_code == "LoopBudgetExhausted"


def _interrupt_call(events: list[Any]) -> Any:
    for e in events:
        for p in e.content.parts if e.content and e.content.parts else []:
            if p.function_call and p.function_call.name == "adk_request_input":
                return p.function_call
    return None


async def _hitl_round_trip(runner: InMemoryRunner) -> tuple[Any, Any]:
    events, sid = await _run(runner, "please ship it")
    call = _interrupt_call(events)
    assert call is not None, [e.model_dump(exclude_none=True) for e in events]
    answer = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response={"approved": True}
                )
            )
        ],
    )
    resumed = [e async for e in runner.run_async(user_id="u", session_id=sid, new_message=answer)]
    return events, resumed


async def test_request_input_interrupts_and_resumes_like_adk(orchestrator) -> None:  # type: ignore[no-untyped-def]
    _, native = await _hitl_round_trip(InMemoryRunner(node=samples.hitl(), app_name="n"))
    agent = PetriWorkflow.from_workflow(
        samples.hitl(), orchestrator=orchestrator, interruptible=["ask"]
    )
    _, compiled = await _hitl_round_trip(InMemoryRunner(node=agent, app_name="c"))
    assert _final_output(compiled) == _final_output(native)
    assert _final_output(compiled) is not None
