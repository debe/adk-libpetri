"""Port of Java ``PetriAgentLiveTest``: ``InMemoryRunner.run_live`` reaches the
BIDI bridge through ``PetriAgent.builder(...).live(LiveConfig(...))``.

Both outbound events are authored by the net, since that is all the bridge
returns. The ``LiveConfig`` decoder turns a server frame into net input: a
content-less frame becomes a ``CALLBACK_SIGNAL``, a model turn a
``MODEL_CHUNK`` token. Two distinct authors keep the two paths apart.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import (
    RunConfig,
    StreamingMode,  # pyright: ignore[reportPrivateImportUsage]
)
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.runner import LiveConfig, PetriAgent, PetriRunner, SessionExecutorRegistry

AGENT_NAME = "live_agent"
CALLBACK_SIGNAL: Place[None] = Place("petriAgentLive_callbackSignal")
MODEL_CHUNK: Place[types.Content] = Place("petriAgentLive_modelChunk", types.Content)


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("live-orchestrator")
    yield loop
    loop.close()


class FakeLiveConnection:
    """Records live sends and exposes a raw server stream driven by the test."""

    def __init__(self) -> None:
        self._raw: asyncio.Queue[Any] = asyncio.Queue()
        self.realtime_sends: list[types.Blob] = []
        self.content_sends: list[types.Content] = []
        self.closes = 0

    def push(self, frame: Any) -> None:
        self._raw.put_nowait(frame)

    async def send_content(self, content: types.Content) -> None:
        self.content_sends.append(content)

    async def send_realtime(self, blob: types.Blob) -> None:
        self.realtime_sends.append(blob)

    async def raw_receive(self) -> AsyncIterator[Any]:
        while True:
            yield await self._raw.get()

    async def close(self) -> None:
        self.closes += 1


def content(role: str, t: str) -> types.Content:
    return types.Content(role=role, parts=[types.Part(text=t)])


def emit_callback(ctx: Ctx) -> None:
    ctx.input(CALLBACK_SIGNAL)
    ctx.output(
        C.EVENT_OUT,
        Event(
            invocation_id="callback-invocation",
            author="callback_net",
            content=content("model", "callback fired"),
        ),
    )


def emit_model_chunk(ctx: Ctx) -> None:
    chunk = ctx.input(MODEL_CHUNK)
    ctx.output(
        C.EVENT_OUT,
        Event(invocation_id="callback-invocation", author="model_net", content=chunk, partial=True),
    )


SPEC = NetSpec(
    "petri-agent-live-test",
    (
        TransitionSpec("T_CallbackSignal", (one(CALLBACK_SIGNAL),), out(C.EVENT_OUT)),
        TransitionSpec("T_ModelChunk", (one(MODEL_CHUNK),), out(C.EVENT_OUT)),
    ),
)


def callback_signal_runner(orch: OrchestratorLoop) -> Any:
    return (
        PetriRunner.builder(
            SPEC, {"T_CallbackSignal": emit_callback, "T_ModelChunk": emit_model_chunk}
        )
        .environment_place(CALLBACK_SIGNAL)
        .environment_place(MODEL_CHUNK)
        .orchestrator(orch)
        .astart()
    )


async def until(cond: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Condition was not met within {timeout}s")


def text_of(event: Event) -> str | None:
    if event.content is None or not event.content.parts:
        return None
    return "".join(p.text or "" for p in event.content.parts)


async def test_of_live_bridges_live_queue_model_frames_and_callback_signals(orch) -> None:
    connection = FakeLiveConnection()
    callback_frames = 0

    def on_server_message(msg: types.LiveServerMessage, runner: PetriRunner) -> None:
        nonlocal callback_frames
        callback_frames += 1
        if msg.server_content is None:
            runner.signal(CALLBACK_SIGNAL)
        # Model content reaches egress only by entering the net.
        elif msg.server_content.model_turn is not None:
            runner.inject(MODEL_CHUNK, msg.server_content.model_turn)

    registry = SessionExecutorRegistry.strong_owned()
    events: list[Event] = []
    consumer: asyncio.Task[None] | None = None
    live: Any = None
    try:
        agent = (
            PetriAgent.builder(AGENT_NAME, registry, lambda key: callback_signal_runner(orch))
            .description("BIDI live bridge test")
            .live(LiveConfig(lambda ctx: connection, on_server_message))
            .build()
        )
        adk = InMemoryRunner(agent=agent, app_name="app")
        session = await adk.session_service.create_session(
            app_name="app", user_id="live-user", session_id="live-session"
        )

        queue = LiveRequestQueue()
        live = adk.run_live(
            user_id=session.user_id,
            session_id=session.id,
            live_request_queue=queue,
            run_config=RunConfig(streaming_mode=StreamingMode.BIDI),
        )

        async def consume() -> None:
            async for e in live:
                events.append(e)

        consumer = asyncio.ensure_future(consume())

        user_content = content("user", "hello over live")
        queue.send_content(user_content)
        await until(lambda: bool(connection.content_sends))
        assert connection.content_sends == [user_content]

        # The bridge is subscribed once the input pump forwarded the content.
        connection.push(types.LiveServerMessage())
        await until(lambda: len(events) >= 1)
        assert callback_frames == 1
        assert len(events) == 1
        callback_event = events[0]
        assert callback_event.author == "callback_net"
        assert text_of(callback_event) == "callback fired"

        connection.push(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(model_turn=content("model", "model says hi"))
            )
        )
        await until(lambda: len(events) >= 2)

        model_events = [e for e in events if e.author == "model_net"]
        assert model_events, "no net-authored model event"
        assert text_of(model_events[0]) == "model says hi"
        # Authored by the net's emit transition, so the turn flag is the net's too.
        assert model_events[0].partial is True
        assert callback_frames == 2

        queue.close()
        await until(lambda: connection.closes >= 1)
    finally:
        if consumer is not None and not consumer.done():
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
        if live is not None:
            await live.aclose()
        await registry.aclose_all()
