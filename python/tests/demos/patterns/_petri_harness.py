"""Shared driver for the Petri-net pattern demos: one ADK turn, then drain and inspect.

``PetriAgent`` ends a turn at the net's first non-partial event, so "the agent
yielded one event" says nothing about at-most-once commit on its own: a second
commit would land after the turn returned. The driver therefore keeps a
subscription to the runner's egress for the runner's whole life, drains the
runner once the turn is over (in-flight branch actions run to completion), and
hands back every event the net ever published plus the final marking.

:func:`orchestrator` is the one ``OrchestratorLoop`` per module. It runs one
untimed warm-up turn first: in a fresh process the first turn pays ADK's and
libpetri's first-call costs (150-250 ms measured), which would otherwise eat the
latency margins of whichever demo happens to run first.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import libpetri as lp
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import Action, Ctx, NetSpec, TransitionSpec, one, out
from adk_libpetri.runner import PetriAgent, PetriRunner, SessionExecutorRegistry, SessionKey

APP = "patterns"
USER = "u"


@dataclass
class NetRun:
    elapsed: float
    """Wall time of the ADK turn, from ``run_async`` to its last event."""
    events: list[Event]
    """What the stock ``InMemoryRunner`` yielded for the turn."""
    egress: list[Event]
    """Every event the net published on ``EVENT_OUT`` over the runner's whole life."""
    final: lp.MarkingView
    """The marking after the drained runner came to rest."""

    def authored_by(self, author: str) -> list[Event]:
        return [e for e in self.events if e.author == author]


def text_of(event: Event) -> str:
    assert event.content is not None and event.content.parts
    return "".join(p.text or "" for p in event.content.parts)


def model_event(author: str, invocation_id: str, text: str) -> Event:
    return Event(
        invocation_id=invocation_id,
        author=author,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


async def run_turn_then_drain(
    orch: OrchestratorLoop,
    spec: NetSpec,
    actions: dict[str, Action],
    *,
    agent_name: str,
    description: str,
    text: str,
) -> NetRun:
    """Drive ``spec`` through ``PetriAgent`` under a stock ``InMemoryRunner`` for one turn."""
    started: list[tuple[PetriRunner, Any]] = []

    async def factory(_key: SessionKey) -> PetriRunner:
        runner = await (
            PetriRunner.builder(spec, actions)
            .environment_place(C.USER_IN)
            .event_store(lp.InMemoryEventStore())
            .orchestrator(orch)
            .astart()
        )
        # Subscribed before the turn injects USER_IN, so nothing is missed.
        started.append((runner, runner.adk_events().subscribe()))
        return runner

    registry = SessionExecutorRegistry.strong_owned()
    agent = PetriAgent.builder(agent_name, registry, factory).description(description).build()
    try:
        adk = InMemoryRunner(agent=agent, app_name=APP)
        session = await adk.session_service.create_session(app_name=APP, user_id=USER)
        message = types.Content(role="user", parts=[types.Part(text=text)])
        start = time.monotonic()
        events = [
            e async for e in adk.run_async(user_id=USER, session_id=session.id, new_message=message)
        ]
        elapsed = time.monotonic() - start
    finally:
        await registry.aclose_all()  # drain: in-flight losers finish, then the run ends

    assert len(started) == 1, "one session, one runner"
    runner, egress_sub = started[0]
    final = await runner.wait_closed()
    egress = [e async for e in egress_sub]  # the stream completes with the run
    return NetRun(elapsed=elapsed, events=events, egress=egress, final=final)


WARM_UP = NetSpec("warm-up", (TransitionSpec("Warm_Echo", (one(C.USER_IN),), out(C.EVENT_OUT)),))


def _echo(ctx: Ctx) -> None:
    ctx.input(C.USER_IN)
    ctx.output(C.EVENT_OUT, model_event("warm_up", "warm-up", "ok"))


def orchestrator(name: str) -> Iterator[OrchestratorLoop]:
    """Body of a module-scoped fixture: one loop, warmed up, closed at module end."""
    loop = OrchestratorLoop(name)
    try:
        asyncio.run(
            run_turn_then_drain(
                loop,
                WARM_UP,
                {"Warm_Echo": _echo},
                agent_name="warm_up",
                description="untimed warm-up turn",
                text="hi",
            )
        )
        yield loop
    finally:
        loop.close()
