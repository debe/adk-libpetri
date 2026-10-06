"""The turn protocol across turns: what a session's net does between, during and after them."""

from __future__ import annotations

import asyncio
import gc
import weakref
from typing import Any

import pytest
from google.adk.agents.llm_agent import LlmAgent
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow
from google.genai import types

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.net import NetRunError, PetriNet
from adk_libpetri.runner import SessionExecutorRegistry
from support.fake_llm import ScriptedLlm, text

from ._harness import runner_of, session, text_of
from .conftest import BLUEPRINTS, Serve

TURNS = BLUEPRINTS / "bp_turns"
IN_WF = BLUEPRINTS / "bp_inwf"


def load(serve: Serve, name: str) -> PetriNet:
    node = serve(PetriNet.from_config(str(TURNS / name)))
    for item in node.nodes:  # a child net runs on the package's loop too
        for el in item if isinstance(item, list | tuple) else (item,):
            if isinstance(el, PetriNet):
                serve(el)
    return node


def outputs(events: list[Event], author: str) -> list[Any]:
    """The distinct outputs ``author`` gave, in order (ADK repeats a node's final one)."""
    return list(dict.fromkeys(e.output for e in events if e.author == author and e.output))


async def marking(node: PetriNet, s: Any) -> dict[str, int]:
    snap = await runner_of(node, s).snapshot()
    return {p.name: n for p in node.spec.places if (n := snap.marking.count(p.name))}


def message(t: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=t)])


async def run_once(runner: InMemoryRunner, session_id: str, t: str) -> list[Event]:
    return [
        e
        async for e in runner.run_async(user_id="u", session_id=session_id, new_message=message(t))
    ]


# ----------------------------------------------------------------------------
#  Node runs outside a turn
# ----------------------------------------------------------------------------


async def test_a_timed_node_after_the_answer_runs_in_the_next_turn(serve: Serve) -> None:
    # After_Later fires 200 ms after the answer, when no turn is open. Its node
    # waits for the next turn instead of failing, so the permit comes back.
    node = load(serve, "after_turn.yaml")
    s = await session(node)
    first = await s.say("one")
    assert first.error is None and first.texts == ["one"]
    await asyncio.sleep(0.4)  # After_Later has fired, and waits for a turn
    second = await asyncio.wait_for(s.say("two"), 5)
    assert second.error is None, second.error
    assert second.texts == ["two"]
    await asyncio.sleep(0.4)
    # The second turn's After_Later is in flight, waiting for a third turn.
    assert await marking(node, s) == {"eventOut": 2}


async def test_a_seeded_node_transition_runs_inside_the_first_turn(serve: Serve) -> None:
    # Seeded_Warm is enabled when the session's net starts, before any turn.
    node = load(serve, "seeded.yaml")
    s = await session(node)
    t = await asyncio.wait_for(s.say("hi"), 5)
    assert t.error is None, t.error
    assert t.texts == ["hi"]


async def test_a_node_waiting_for_a_turn_fails_when_the_session_closes(
    orchestrator: OrchestratorLoop,
) -> None:
    registry = SessionExecutorRegistry.strong_owned()
    node = PetriNet.from_config(str(TURNS / "after_turn.yaml")).serve_on(
        orchestrator, registry=registry
    )
    s = await session(node)
    await s.say("one")
    await asyncio.sleep(0.4)  # After_Later waits for a turn that never comes
    runner = runner_of(node, s)
    await asyncio.wait_for(asyncio.to_thread(registry.close_all), 5)  # does not hang
    assert runner.closed


# ----------------------------------------------------------------------------
#  One turn at a time
# ----------------------------------------------------------------------------


async def test_two_invocations_of_one_session_are_served_one_after_the_other(
    serve: Serve,
) -> None:
    node = load(serve, "inner.yaml")
    runner = InMemoryRunner(node=node, app_name="app")
    s = await runner.session_service.create_session(app_name="app", user_id="u")
    slow, fast = await asyncio.wait_for(
        asyncio.gather(run_once(runner, s.id, "slow one"), run_once(runner, s.id, "fast one")),
        5,
    )
    # Each turn gets its own answer, not the other's.
    assert outputs(slow, "inner_net") == ["tag[slow one]"]
    assert outputs(fast, "inner_net") == ["tag[fast one]"]


async def test_one_petri_net_node_run_by_two_transitions_at_once(serve: Serve) -> None:
    node = load(serve, "twice.yaml")
    s = await session(node)
    t = await asyncio.wait_for(s.say("q"), 5)
    assert t.error is None, t.error
    assert outputs(t.events, "twice_net")[-1] == "join[tag[q] | tag[b:q]]"


# ----------------------------------------------------------------------------
#  turnAbort of a mounted child
# ----------------------------------------------------------------------------


def _assistant(name: str) -> PetriNet:
    agent = LlmAgent(
        name="helper",
        model=ScriptedLlm.of(RuntimeError("llm down"), text("ok")),
        instruction="Be brief.",
    )
    return PetriNet(
        name=name,
        nodes=[[agent]],  # pyright: ignore[reportArgumentType]
        subnets={
            "assistant": {
                "stock": "llm_agent",
                "from": "helper",
                "bind": {"userIn": "userIn", "eventOut": "eventOut"},
            }
        },
    )


def test_a_stock_llm_agents_turn_abort_is_the_nets_own() -> None:
    # No turnAbort bind needed: the child's turnAbort is the one the runner signals.
    node = _assistant("flat")
    names = {p.name for p in node.spec.places}
    assert "turnAbort" in names and "assistant/turnAbort" not in names


async def test_a_nested_stock_llm_agent_is_aborted_and_answers_the_next_turn(
    serve: Serve,
) -> None:
    inner = _assistant("child")
    outer = serve(
        PetriNet(
            name="outer",
            nodes=[[inner]],  # pyright: ignore[reportArgumentType]
            subnets={
                "inner": {"net": "child", "bind": {"userIn": "userIn", "eventOut": "eventOut"}}
            },
        )
    )
    assert "turnAbort" in {p.name for p in outer.spec.places}
    s = await session(outer)
    failed = await asyncio.wait_for(s.say("q0"), 5)
    assert failed.error is not None
    ok = await asyncio.wait_for(s.say("q1"), 5)  # the abort cleared the failed turn
    assert ok.error is None, ok.error
    assert ok.texts == ["ok"]


# ----------------------------------------------------------------------------
#  Node outputs on coloured places
# ----------------------------------------------------------------------------


async def test_a_node_output_that_is_no_value_fails_its_transition_by_name(
    serve: Serve,
) -> None:
    node = load(serve, "content.yaml")
    s = await session(node)
    t = await s.say("hi")
    assert isinstance(t.error, NetRunError), t.error
    assert "node 'to_content'" in str(t.error)
    assert "place 'c'" in str(t.error) and "Event(output=content)" in str(t.error)


async def test_a_node_writing_eventout_streams_the_answer_before_slow_runs_finish(
    serve: Serve,
) -> None:
    node = load(serve, "direct.yaml")
    s = await session(node)
    t = await s.say("q")
    assert t.error is None, t.error
    assert t.texts == ["fast q"]
    assert t.first_event_after is not None and t.first_event_after < 0.4
    assert t.elapsed >= 0.5  # the invocation still waits for the slow run


# ----------------------------------------------------------------------------
#  Inside a Workflow, and the turn's input
# ----------------------------------------------------------------------------


async def test_a_petri_net_inside_a_workflow_hands_its_answer_on(serve: Serve) -> None:
    from google.adk.agents.config_agent_utils import from_config

    root = from_config(str(IN_WF / "root_agent.yaml"))
    assert isinstance(root, Workflow)
    assert root.graph is not None
    for n in root.graph.nodes:
        if isinstance(n, PetriNet):
            serve(n)
    runner = InMemoryRunner(node=root, app_name="app")
    s = await runner.session_service.create_session(app_name="app", user_id="u")
    events = await asyncio.wait_for(run_once(runner, s.id, "hi"), 5)
    got = list(dict.fromkeys(e.output for e in events if e.output is not None))
    # net_text's emitted text is its output too; net_value takes the str
    # after[...] as a user Content on its Content-typed userIn.
    assert got == ["HI", "after['HI']", "AFTER['HI']", "after2[\"AFTER['HI']\"]"]


async def test_an_input_of_the_wrong_type_for_userin_is_named(serve: Serve) -> None:
    node = serve(PetriNet.from_config(str(IN_WF / "net_int.yaml")))
    s = await session(node)
    t = await s.say("hi")
    assert isinstance(t.error, NetRunError), t.error
    assert "userIn takes int" in str(t.error) and "Content" in str(t.error)


# ----------------------------------------------------------------------------
#  env: places, from outside
# ----------------------------------------------------------------------------


async def test_inject_fills_an_env_place_during_a_turn(serve: Serve) -> None:
    node = load(serve, "approval.yaml")
    runner = InMemoryRunner(node=node, app_name="app")
    s = await runner.session_service.create_session(app_name="app", user_id="u")
    with pytest.raises(RuntimeError, match="starts with the session's first turn"):
        node.inject(s, "approval", True)
    with pytest.raises(ValueError, match="not an env: place"):
        node.inject(s, "waiting")

    async def approve() -> None:
        while node.registry.get(node.session_key(s)) is None:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        assert node.inject(s, "approval", True)

    events, _ = await asyncio.wait_for(
        asyncio.gather(run_once(runner, s.id, "plan"), approve()), 2.5
    )
    assert [t for e in events if e.author == "approval" and (t := text_of(e))] == ["draft of plan"]


# ----------------------------------------------------------------------------
#  Runners: one per node, torn down with the node
# ----------------------------------------------------------------------------


async def test_two_nets_of_one_name_on_one_registry_keep_their_own_runners(
    orchestrator: OrchestratorLoop,
) -> None:
    registry = SessionExecutorRegistry.strong_owned()
    a = PetriNet.from_config(str(TURNS / "same_a.yaml")).serve_on(orchestrator, registry=registry)
    b = PetriNet.from_config(str(TURNS / "same_b.yaml")).serve_on(orchestrator, registry=registry)
    try:
        inner = Workflow(name="inner", edges=[("START", b)])
        root = Workflow(name="root", edges=[("START", a, inner)])
        runner = InMemoryRunner(node=root, app_name="app")
        s = await runner.session_service.create_session(app_name="app", user_id="u")
        events = await asyncio.wait_for(run_once(runner, s.id, "Hello"), 5)
        assert outputs(events, "worker") == ["HELLO", "lower:hello"]
        assert a.session_scope() != b.session_scope()
    finally:
        registry.close_all()


async def test_a_collected_nets_session_runners_are_torn_down(
    orchestrator: OrchestratorLoop,
) -> None:
    # A node with its own registry (as ADK's loader builds it) has no caller
    # to close its runners: they close when the node is collected.
    node = PetriNet.from_config(str(TURNS / "inner.yaml")).serve_on(orchestrator)
    s = await session(node)
    assert (await s.say("x")).error is None
    runner = runner_of(node, s)
    alive = weakref.ref(node)
    del node, s
    for _ in range(5):
        gc.collect()
        if alive() is None:
            break
        await asyncio.sleep(0.05)
    assert alive() is None
    await asyncio.wait_for(runner.wait_closed(), 5)
    assert runner.closed
