"""Port of Java ``BidiPetriAgentTest``: the generic BIDI pump, no live WebSocket.

A fake :class:`LiveConnection` and ADK's real ``LiveRequestQueue`` drive a real
running :class:`PetriRunner`. **The net authors every event**: the bridge maps
no frames, so ``partial`` and ``turn_complete`` come from whichever transition
fired. The fixture net::

    [MODEL_CHUNK]   --T_EmitPartial--> [EVENT_OUT]   partial=True, content
    [TURN_COMPLETE] --T_EmitFinal----> [EVENT_OUT]   partial=False, turn_complete=True
                        o---[MODEL_CHUNK]            inhibitor: no terminal while
                                                     chunks are queued

The inhibitor is what orders a burst's egress (see the burst test).

Java's ``TestSubscriber.cancel()`` is ``aclose()`` on the returned generator.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.events.event import Event
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.runner import PetriRunner
from adk_libpetri.runner.bidi_petri_agent import bridge
from adk_libpetri.runner.live_connection import LiveConnection

MODEL_CHUNK: Place[types.Content] = Place("bridgeTest_modelChunk", types.Content)
"""Model content the consumer callback injects, one token per streamed chunk."""

TURN_COMPLETE: Place[None] = Place("bridgeTest_turnComplete")
"""Turn boundary the consumer callback signals; drives the terminal event."""


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("bidi-orchestrator")
    yield loop
    loop.close()


# ============================================================
#  Fake connection
# ============================================================


_END = object()


class FakeLiveConnection:
    """Records sends; the test pushes server frames (or an error) through :meth:`push`."""

    def __init__(self) -> None:
        self._raw: asyncio.Queue[Any] = asyncio.Queue()
        self.realtime_sends: list[types.Blob] = []
        self.content_sends: list[types.Content] = []
        self.closed = False
        self.receiving = False

    def push(self, frame: Any) -> None:
        self._raw.put_nowait(frame)

    async def send_content(self, content: types.Content) -> None:
        self.content_sends.append(content)

    async def send_realtime(self, blob: types.Blob) -> None:
        self.realtime_sends.append(blob)

    async def raw_receive(self) -> AsyncIterator[Any]:
        self.receiving = True
        try:
            while True:
                frame = await self._raw.get()
                if frame is _END:
                    return
                if isinstance(frame, BaseException):
                    raise frame
                yield frame
        finally:
            self.receiving = False

    async def close(self) -> None:
        self.closed = True


def test_fake_connection_satisfies_the_live_connection_protocol() -> None:
    assert isinstance(FakeLiveConnection(), LiveConnection)


# ============================================================
#  Fixture
# ============================================================


def turn_complete_frame() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


def model_content(t: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(role="model", parts=[types.Part(text=t)])
        )
    )


def emit_partial(ctx: Ctx) -> None:
    chunk = ctx.input(MODEL_CHUNK)
    ctx.output(C.EVENT_OUT, Event(invocation_id="inv", author="net", content=chunk, partial=True))


def emit_final(ctx: Ctx) -> None:
    ctx.input(TURN_COMPLETE)
    ctx.output(
        C.EVENT_OUT,
        Event(invocation_id="inv", author="net", partial=False, turn_complete=True),
    )


SPEC = NetSpec(
    "bridge-test",
    (
        TransitionSpec("T_EmitPartial", (one(MODEL_CHUNK),), out(C.EVENT_OUT)),
        # The terminal cannot fire while a chunk is still queued: this orders egress.
        TransitionSpec(
            "T_EmitFinal", (one(TURN_COMPLETE),), out(C.EVENT_OUT), inhibitors=(MODEL_CHUNK,)
        ),
    ),
)


def decode(msg: types.LiveServerMessage, r: PetriRunner) -> None:
    """The consumer half: decode the frame, inject into the net, fire-and-forget."""
    sc = msg.server_content
    if sc is not None and sc.model_turn is not None:
        r.inject(MODEL_CHUNK, sc.model_turn)
    if sc is not None and sc.turn_complete:
        r.signal(TURN_COMPLETE)


@dataclass
class Fixture:
    runner: PetriRunner
    conn: FakeLiveConnection
    queue: LiveRequestQueue
    out: Any
    events: list[Event] = field(default_factory=list)
    error: BaseException | None = None
    task: asyncio.Task[None] | None = None

    async def consume(self) -> None:
        try:
            async for e in self.out:
                self.events.append(e)
        except Exception as err:
            self.error = err

    async def cancel(self) -> None:
        """Java ``TestSubscriber.cancel()``: stop consuming and close the stream."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        await self.out.aclose()


@pytest.fixture
async def f(orch: OrchestratorLoop) -> AsyncIterator[Fixture]:
    actions = {"T_EmitPartial": emit_partial, "T_EmitFinal": emit_final}
    runner = await (
        PetriRunner.builder(SPEC, actions)
        .environment_place(MODEL_CHUNK)
        .environment_place(TURN_COMPLETE)
        .orchestrator(orch)
        .astart()
    )
    conn = FakeLiveConnection()
    queue = LiveRequestQueue()
    fx = Fixture(runner, conn, queue, bridge(queue, conn, runner, decode))
    fx.task = asyncio.ensure_future(fx.consume())
    # The bridge subscribes to egress on its first step; wait for it so no event is missed.
    await until(lambda: runner.adk_events().subscriber_count > 0 and conn.receiving)
    try:
        yield fx
    finally:
        await fx.cancel()
        await runner.aclose()


async def until(cond: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Condition was not met within {timeout}s")


def text_of(event: Event) -> str | None:
    if event.content is None or not event.content.parts:
        return None
    return "".join(p.text or "" for p in event.content.parts)


# ============================================================
#  Tests
# ============================================================


async def test_model_chunk_injected_by_the_callback_is_authored_by_the_net_as_a_partial(
    f: Fixture,
) -> None:
    f.conn.push(model_content("the answer is 42"))

    await until(lambda: len(f.events) >= 1)
    assert len(f.events) == 1
    e = f.events[0]
    assert e.author == "net"  # net-authored, not bridge-authored
    assert text_of(e) == "the answer is 42"
    assert e.partial is True
    assert not e.turn_complete


async def test_turn_complete_signal_is_authored_by_the_net_as_a_terminal_event(
    f: Fixture,
) -> None:
    # No model content at all: a pure decoded turn edge still drives egress.
    f.conn.push(turn_complete_frame())

    await until(lambda: len(f.events) >= 1)
    assert len(f.events) == 1
    e = f.events[0]
    assert e.author == "net"
    assert not e.partial
    assert e.turn_complete is True


async def test_a_burst_streamed_turn_yields_every_partial_before_the_terminal_event(
    f: Fixture,
) -> None:
    # Back to back, nothing awaited in between: the frames land faster than the
    # orchestrator drains them. Without T_EmitFinal's inhibitor the terminal
    # could overtake its own partials.
    f.conn.push(model_content("Sure, "))
    f.conn.push(model_content("the answer "))
    f.conn.push(model_content("is 42."))
    f.conn.push(turn_complete_frame())

    await until(lambda: len(f.events) >= 4)
    events = f.events
    assert len(events) == 4
    assert [(e.partial, text_of(e)) for e in events[:3]] == [
        (True, "Sure, "),
        (True, "the answer "),
        (True, "is 42."),
    ]
    assert events[3].turn_complete is True
    assert not events[3].partial


async def test_inbound_queue_frames_are_forwarded_to_the_connection(f: Fixture) -> None:
    audio = types.Blob(data=bytes([1, 2, 3]), mime_type="audio/pcm")
    content = types.Content(role="user", parts=[types.Part(text="hi")])

    f.queue.send_realtime(audio)
    f.queue.send_content(content)

    await until(lambda: bool(f.conn.realtime_sends) and bool(f.conn.content_sends))
    assert f.conn.realtime_sends == [audio]
    assert f.conn.content_sends == [content]


async def test_a_transport_error_terminates_the_returned_stream(f: Fixture) -> None:
    # Otherwise a dead connection leaves the consumer on an egress nothing feeds.
    boom = RuntimeError("websocket closed")
    f.conn.push(boom)

    assert f.task is not None
    await asyncio.wait_for(asyncio.shield(f.task), 2)
    assert f.error is boom


async def test_cancelling_the_outbound_stream_disposes_both_pumps(f: Fixture) -> None:
    f.queue.send_realtime(types.Blob(data=bytes([1]), mime_type="audio/pcm"))
    await until(lambda: len(f.conn.realtime_sends) == 1)
    f.conn.realtime_sends.clear()

    await f.cancel()

    # Output pump gone: nothing reads the raw server stream any more.
    await until(lambda: not f.conn.receiving)
    # Input pump gone: further queue frames are not forwarded.
    f.queue.send_realtime(types.Blob(data=bytes([9]), mime_type="audio/pcm"))
    await asyncio.sleep(0.05)
    assert f.conn.realtime_sends == []


async def test_cancelling_the_outbound_stream_closes_the_connection(f: Fixture) -> None:
    # A consumer that simply cancels (user hangs up, ADK abandons the turn) must
    # close the transport, not just stop reading it.
    assert not f.conn.closed

    await f.cancel()

    await until(lambda: f.conn.closed)
    assert f.conn.closed


async def test_a_queue_close_request_closes_the_connection(f: Fixture) -> None:
    # Python-only: the input pump maps LiveRequest(close=True) to close().
    f.queue.close()

    await until(lambda: f.conn.closed)
