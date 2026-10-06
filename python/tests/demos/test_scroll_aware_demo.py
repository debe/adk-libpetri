"""Port of Java ``ScrollAwareDemoTest``.

End-to-end demo for **non-``Content`` external signals**: a UI-style scroll
event is injected onto a dedicated typed env place from a separate thread,
the running net records it, and an ADK ``Runner.run_async`` invocation reads
the recorded count when emitting its response ``Event``.

What it proves:

1. ``PetriRunner.Builder.environment_place`` lets an ADK-integrated runner
   declare any number of typed env places beyond ``USER_IN``; here
   ``SCROLL_IN``.
2. The runner's ``inject(place, token)`` is the one ingress surface: a non-ADK
   thread finds the session's runner through ``SessionExecutorRegistry.get``
   and injects without going through the ADK adapter (commitment 1).
3. The ADK egress (``adk_events`` on ``EVENT_OUT``) keeps working: it is the
   named ADK-contract bridge, not a general observation API.

Topology::

    [SCROLL_IN] + [SCROLL_COUNT] --Scroll_Record--> [SCROLL_COUNT]   (+1)
    [USER_IN] + read(SCROLL_COUNT) --Scroll_Echo--> [EVENT_OUT]

``SCROLL_COUNT`` holds exactly one token at all times (seeded with ``0``;
``Scroll_Record`` consumes the old count and produces the next). The marking
is the state (commitment 2).

Python difference: Java waits for ``executor.isQuiescent()`` with nothing in
flight; libpetri-py exposes no such probe, so the test polls the runner's
snapshot until ``SCROLL_IN`` is empty and no action is in flight.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Iterator
from dataclasses import dataclass

import pytest
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.runner import PetriAgent, PetriRunner, SessionExecutorRegistry, SessionKey
from adk_libpetri.subnet import bind


@dataclass(frozen=True, slots=True)
class Scroll:
    """A UI scroll event: the kind of non-ADK external signal the integration
    must accept. Any type works; this one is deliberately trivial."""

    dx: int
    dy: int


SCROLL_IN: Place[Scroll] = Place("scrollIn", Scroll)
"""External ingress for ``Scroll`` events."""

SCROLL_COUNT: Place[int] = Place("scrollCount", int)
"""In-net accumulator: the marking is the state."""

T_RECORD_SCROLL = "Scroll_Record"
T_ECHO = "Scroll_Echo"

SPEC = NetSpec(
    "scroll-aware",
    (
        TransitionSpec(T_RECORD_SCROLL, (one(SCROLL_IN), one(SCROLL_COUNT)), out(SCROLL_COUNT)),
        TransitionSpec(T_ECHO, (one(C.USER_IN),), out(C.EVENT_OUT), reads=(SCROLL_COUNT,)),
    ),
)


def record_scroll(ctx: Ctx) -> None:
    ctx.input(SCROLL_IN)  # consume the scroll event
    current = ctx.input(SCROLL_COUNT)  # consume the running count
    ctx.output(SCROLL_COUNT, current + 1)  # produce the incremented count


def echo(ctx: Ctx) -> None:
    user = ctx.input(C.USER_IN)
    count = ctx.read(SCROLL_COUNT)
    said = "".join(p.text or "" for p in (user.parts or []))
    ctx.output(
        C.EVENT_OUT,
        Event(
            invocation_id="scroll-demo",
            author="scroll_aware_agent",
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"you scrolled {count} times; you said: {said}")],
            ),
        ),
    )


# Checked binding: every transition bound exactly once.
ACTIONS = bind(SPEC, {T_RECORD_SCROLL: record_scroll, T_ECHO: echo})


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("scroll-aware-demo")
    yield loop
    loop.close()


async def await_quiescent(runner: PetriRunner, timeout_s: float = 2.0) -> None:
    """Until every injected scroll is recorded: ``SCROLL_IN`` empty, nothing in flight.

    Inject acceptance only guarantees the token reached the env place; the
    recording transition must have completed before ``Scroll_Echo`` reads.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = await runner.snapshot()
        if not snap.action_in_flight and snap.marking.count(SCROLL_IN.name) == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Runner did not reach quiescence within {timeout_s}s")


async def test_scroll_events_injected_from_a_separate_thread_are_visible_in_the_adk_response(
    orch: OrchestratorLoop,
) -> None:
    def factory(key: SessionKey) -> Awaitable[PetriRunner]:
        return (
            PetriRunner.builder(SPEC, ACTIONS)
            .environment_place(C.USER_IN)
            .environment_place(SCROLL_IN)
            .initial_marking({SCROLL_COUNT: [0]})
            .orchestrator(orch)
            .astart()
        )

    # Two env places on one ADK-integrated runner.
    registry = SessionExecutorRegistry.strong_owned()
    agent = (
        PetriAgent.builder("scroll_aware_agent", registry, factory)
        .description("Echoes user message + recorded scroll count")
        .build()
    )
    try:
        adk_runner = InMemoryRunner(agent=agent, app_name="scroll_app")
        session = await adk_runner.session_service.create_session(
            app_name=adk_runner.app_name, user_id="user-1", session_id="session-1"
        )

        # Create the session's runner BEFORE any side-channel inject, as a
        # session-start hook (websocket open) would; until then
        # registry.get(key) is None. Same call PetriAgent makes.
        key = SessionKey.of(session)
        await registry.aget_or_create(key, factory)

        # Inject from a separate, non-ADK thread, before the ADK turn: an
        # arbitrary external signal reaches the running net through the same
        # env-place injection model as USER_IN.
        def scroller() -> list[bool]:
            runner = registry.get(key)
            assert runner is not None
            return [runner.inject(SCROLL_IN, Scroll(0, 40)) for _ in range(5)]

        accepted = await asyncio.wait_for(asyncio.to_thread(scroller), timeout=2)
        assert accepted == [True] * 5

        runner = registry.get(key)
        assert runner is not None
        await await_quiescent(runner)

        # Scroll_Echo reads SCROLL_COUNT and answers through the stock Runner.
        events = [
            e
            async for e in adk_runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="hello")]),
            )
        ]
        from_agent = [e for e in events if e.author == "scroll_aware_agent"]
        assert from_agent, f"no agent event among {events}"
        last = from_agent[-1]
        assert last.content is not None and last.content.parts
        assert last.content.parts[0].text == "you scrolled 5 times; you said: hello"
    finally:
        # strong_owned(), the default: this is how a session's runner ends
        # (from a session-end hook in an app).
        await registry.aclose_all()
