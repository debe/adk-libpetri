"""Port of Java ``PetriAgentIntegrationTest``.

End-to-end: the libpetri agent driven through the **stock ADK ``Runner``**
(``InMemoryRunner``) with no ADK source changes. ``PetriAgent`` looks like any
other ``BaseAgent`` from ADK's side.

Java's ``cleanerOwned()`` registry maps to ``finalizer_owned()`` here, with the
same stable owner map (``InMemorySessionService`` hands out session copies, so
``ctx.session`` is no owner). Teardown is the per-test registry close, never GC.

Python-only: ``PetriAgent`` as a node of an ADK 2 ``Workflow``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.adk.workflow import FunctionNode, Workflow
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.runner import PetriAgent, PetriRunner, SessionExecutorRegistry, SessionKey
from adk_libpetri.subnet import llm_agent as LA
from support.fake_llm import ScriptedLlm, text

NET_LOCAL_INVOCATION_ID = "NET-LOCAL-ID"
"""Marks ids minted inside the net, so a test can tell them from ADK's."""


# ============================================================
#  Fixtures
# ============================================================


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("integration-orchestrator")
    yield loop
    loop.close()


Track = Callable[[SessionExecutorRegistry], SessionExecutorRegistry]


@pytest.fixture
async def registries() -> AsyncIterator[Track]:
    """Registries a test creates, closed after it (never left to the finalizer)."""
    made: list[SessionExecutorRegistry] = []

    def tracked(registry: SessionExecutorRegistry) -> SessionExecutorRegistry:
        made.append(registry)
        return registry

    yield tracked
    for r in made:
        await r.aclose_all()


class _Owner:
    """A weak-referenceable owner (a bare ``object()`` is not)."""


def session_owner_map() -> Callable[[InvocationContext], Any]:
    """Stable per-session owner identity keyed by ``SessionKey``."""
    owners: dict[SessionKey, _Owner] = {}
    return lambda ctx: owners.setdefault(SessionKey.of(ctx.session), _Owner())


def petri_runner_factory(
    orch: OrchestratorLoop, llm: Any, agent_name: str, invocation_id: str | None = None
) -> Callable[[SessionKey], Any]:
    kwargs: dict[str, Any] = {}
    if invocation_id is not None:
        kwargs["invocation_id_supplier"] = lambda: invocation_id
    config = LA.Config(name=agent_name, model="fake-model", **kwargs)

    def factory(_key: SessionKey) -> Any:
        return (
            PetriRunner.builder(LA.DEF, LA.action_bindings(llm, config))
            .environment_place(C.USER_IN)
            .orchestrator(orch)
            .astart()
        )

    return factory


def agent_of(
    orch: OrchestratorLoop,
    registry: SessionExecutorRegistry,
    name: str,
    description: str,
    llm: Any,
    invocation_id: str | None = None,
) -> PetriAgent:
    return PetriAgent.of(
        name,
        description,
        registry,
        petri_runner_factory(orch, llm, name, invocation_id),
        session_owner_map(),
    )


def user_message(t: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=t)])


def text_of(event: Event) -> str:
    assert event.content is not None and event.content.parts
    return "".join(p.text or "" for p in event.content.parts)


async def run_turn(runner: InMemoryRunner, user_id: str, session_id: str, msg: str) -> list[Event]:
    return [
        e
        async for e in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=user_message(msg)
        )
    ]


def last_agent_text(events: list[Event], author: str) -> str:
    mine = [e for e in events if e.author == author]
    assert mine, f"no event from author {author!r}"
    return text_of(mine[-1])


async def new_session(runner: InMemoryRunner, user_id: str, session_id: str) -> Any:
    return await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )


# ============================================================
#  Tests
# ============================================================


async def test_agent_responds_via_adk_runner_run_async(orch, registries) -> None:
    llm = ScriptedLlm.of(text("hello from petri"))
    registry = registries(SessionExecutorRegistry.finalizer_owned())
    agent = agent_of(orch, registry, "petri_agent", "Petri-backed LLM agent", llm)

    # Stock InMemoryRunner: no fork, no special wiring.
    adk = InMemoryRunner(agent=agent, app_name="app")
    session = await new_session(adk, "user-1", "session-1")

    events = await run_turn(adk, session.user_id, session.id, "hi")

    assert events
    assert last_agent_text(events, "petri_agent") == "hello from petri"


async def test_multiple_invocations_in_same_session_reuse_the_long_lived_runner(
    orch, registries
) -> None:
    llm = ScriptedLlm.of(text("first"), text("second"), text("third"))
    registry = registries(SessionExecutorRegistry.finalizer_owned())
    agent = agent_of(orch, registry, "long_lived", "stays alive across messages", llm)
    adk = InMemoryRunner(agent=agent, app_name="app")
    session = await new_session(adk, "user-1", "sess-1")

    texts = [
        last_agent_text(await run_turn(adk, session.user_id, session.id, f"msg {i}"), "long_lived")
        for i in (1, 2, 3)
    ]

    assert texts == ["first", "second", "third"]
    # Three invocations, one registered runner: the long-lived net was reused.
    assert registry.size() == 1


async def test_run_live_bridges_to_runner_event_stream(orch, registries) -> None:
    # Without a LiveConfig, _run_live_impl returns the runner's hot adk_events();
    # the input half is the caller's (forward frames to runner.inject).
    llm = ScriptedLlm.of(text("live answer"))
    registry = registries(SessionExecutorRegistry.finalizer_owned())
    agent = agent_of(orch, registry, "live_agent", "BIDI bridge test", llm)
    adk = InMemoryRunner(agent=agent, app_name="app")
    session = await new_session(adk, "live-user", "live-sess")

    queue = LiveRequestQueue()
    live = adk.run_live(user_id=session.user_id, session_id=session.id, live_request_queue=queue)
    # Subscribe before injecting: the egress is hot, late subscribers miss events.
    first = asyncio.ensure_future(anext(live))

    key = SessionKey.of(session)
    for _ in range(100):
        if registry.get(key) is not None:
            break
        await asyncio.sleep(0.02)
    runner = registry.get(key)
    assert runner is not None
    # The registry is populated before run_live subscribes to adk_events; give the
    # agent generator a moment to reach its subscription.
    for _ in range(100):
        if runner.adk_events().subscriber_count > 0:
            break
        await asyncio.sleep(0.02)
    assert runner.inject(C.USER_IN, user_message("hi live"))

    event = await asyncio.wait_for(first, 3)
    assert text_of(event) == "live answer"

    queue.close()
    await live.aclose()


async def test_different_sessions_get_isolated_runners(orch, registries) -> None:
    llm = ScriptedLlm.of(text("from a"), text("from b"))
    registry = registries(SessionExecutorRegistry.finalizer_owned())
    agent = agent_of(orch, registry, "iso_agent", "isolated per session", llm)
    adk = InMemoryRunner(agent=agent, app_name="app")
    s1 = await new_session(adk, "u-a", "sess-a")
    s2 = await new_session(adk, "u-b", "sess-b")

    await run_turn(adk, s1.user_id, s1.id, "hello")
    await run_turn(adk, s2.user_id, s2.id, "hello")

    assert registry.size() == 2
    assert registry.get(SessionKey.of(s1)) is not None
    assert registry.get(SessionKey.of(s2)) is not None
    assert registry.get(SessionKey.of(s1)) is not registry.get(SessionKey.of(s2))


async def test_a_failed_turn_fails_that_turn_and_leaves_the_session_usable(
    orch, registries
) -> None:
    # A failed transition must fail its turn (not hang it), and must not end the
    # session's egress. The one-turn-at-a-time net would keep the failed turn's
    # permit forever; PetriAgent signals TURN_ABORT on the failure to clear it.
    llm = ScriptedLlm.of(RuntimeError("model exploded"), text("recovered"))
    registry = registries(SessionExecutorRegistry.finalizer_owned())
    agent = agent_of(orch, registry, "resilient", "survives a failed turn", llm)
    adk = InMemoryRunner(agent=agent, app_name="app")
    session = await new_session(adk, "user-1", "sess-fail")

    async def both_turns() -> str:
        # Turn 1 fails, and fails promptly rather than stalling.
        with pytest.raises(Exception):  # noqa: B017 - any failure, as Java asserts Throwable
            await run_turn(adk, session.user_id, session.id, "boom")
        # Turn 2, same session, same runner: still works.
        return last_agent_text(
            await run_turn(adk, session.user_id, session.id, "again"), "resilient"
        )

    assert await asyncio.wait_for(both_turns(), 10) == "recovered"
    assert registry.size() == 1

    # The answer and the returned permit are one firing's deposits; once the net
    # is at rest it holds the one permit and nothing else of either turn.
    runner = registry.get(SessionKey.of(session))
    assert runner is not None
    resting: dict[str, int] = {}
    for _ in range(100):
        snap = await runner.snapshot()
        if snap.is_restore_point:
            resting = {
                place: len(tokens)
                for place, tokens in snap.marking.items()
                if tokens and place != C.EVENT_OUT.name
            }
            if resting == {C.TURN_PERMIT.name: 1}:
                break
        await asyncio.sleep(0.02)
    assert resting == {C.TURN_PERMIT.name: 1}


async def test_the_turn_based_path_stamps_the_adk_invocation_id_like_sse_does(
    orch, registries
) -> None:
    llm = ScriptedLlm.of(text("stamped"))
    registry = registries(SessionExecutorRegistry.finalizer_owned())
    agent = agent_of(
        orch, registry, "stamper", "checks invocation id", llm, NET_LOCAL_INVOCATION_ID
    )
    adk = InMemoryRunner(agent=agent, app_name="app")
    session = await new_session(adk, "user-1", "sess-id")

    events = await run_turn(adk, session.user_id, session.id, "hi")

    agent_events = [e for e in events if e.author == "stamper"]
    assert agent_events
    # "one distinct id" would not catch the bug: the net is consistently wrong.
    for e in agent_events:
        assert e.invocation_id != NET_LOCAL_INVOCATION_ID
    user_events = [e for e in events if e.author == "user"]
    for e in agent_events:
        assert all(e.invocation_id == u.invocation_id for u in user_events)


# ============================================================
#  Python-only: PetriAgent as a node of an ADK 2 Workflow
# ============================================================


async def test_petri_agent_as_a_workflow_node_hands_its_final_text_to_the_next_node(
    orch, registries
) -> None:
    llm = ScriptedLlm.of(text("from the net"))
    registry = registries(SessionExecutorRegistry.strong_owned())
    agent = PetriAgent.builder(
        "net_node", registry, petri_runner_factory(orch, llm, "net_node")
    ).build()
    seen: list[Any] = []

    def after(node_input: str) -> str:
        seen.append(node_input)
        return f"after({node_input})"

    workflow = Workflow(name="wf", edges=[("START", agent, FunctionNode(func=after, name="after"))])
    adk = InMemoryRunner(node=workflow, app_name="wf_app")
    session = await new_session(adk, "u", "wf-sess")

    events = await run_turn(adk, session.user_id, session.id, "hi")

    assert seen == ["from the net"]
    # The net's own reply is still an ADK event, authored by the agent.
    assert last_agent_text(events, "net_node") == "from the net"
    assert any(e.output == "after(from the net)" for e in events)
    # The node input reached the net as the user's turn.
    assert llm.requests
    last = llm.requests[-1].contents[-1]
    assert last.parts is not None and last.parts[0].text == "hi"
