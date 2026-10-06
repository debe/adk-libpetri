"""Port of Java ``SyncGeminiLiveConnectionTest``, plus the wire path over a fake genai session.

Java tests only the decode and its effect on the Vad/BargeIn subnets; its wire
path needs a real genai ``Client``. Python's ``client.aio.live.connect`` is an
async context manager over an ``AsyncSession`` whose surface is small, so a
fake session stands in for the websocket and the wire path is tested too, with
no network: send mapping, the cross-turn raw stream, close, and the whole
connection driven through ``BidiPetriAgent.bridge`` into a running Vad+BargeIn net.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any

import libpetri as lp
import pytest
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.genai import errors, types

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import NetSpec, Place
from adk_libpetri.runner import PetriRunner
from adk_libpetri.runner.bidi_petri_agent import bridge
from adk_libpetri.runner.live_connection import LiveConnection
from adk_libpetri.subnet import merge

from . import barge_in_subnet as BI
from . import vad_subnet as VAD
from ._support import until, until_marked
from .genai_live_connection import GenaiLiveConnection, VoiceSignal

VA = types.VoiceActivityType


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("genai-live-orchestrator")
    yield loop
    loop.close()


# ============================================================
#  Fakes: genai's AsyncSession and client.aio.live.connect
# ============================================================


class FakeSession:
    """The ``AsyncSession`` surface the connection uses.

    ``receive()`` ends after a completed turn, as genai's does. ``close()``
    makes a pending ``receive`` raise the ``APIError`` genai raises when the
    websocket it reads from has closed.
    """

    session_id = "fake-session"

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.client_content: list[dict[str, Any]] = []
        self.realtime: list[dict[str, Any]] = []
        self.closed = False
        self.receive_calls = 0

    def push(self, item: types.LiveServerMessage | BaseException) -> None:
        self._inbox.put_nowait(item)

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        self.receive_calls += 1
        while True:
            item = await self._inbox.get()
            if isinstance(item, BaseException):
                raise item
            yield item
            if item.server_content is not None and item.server_content.turn_complete:
                return

    async def send_client_content(self, *, turns: Any = None, turn_complete: bool = True) -> None:
        self.client_content.append({"turns": turns, "turn_complete": turn_complete})

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.realtime.append(kwargs)

    async def close(self) -> None:
        self.closed = True
        self.push(errors.APIError(1000, {"message": "websocket closed"}))


class FakeLive:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.connects: list[tuple[str, Any]] = []
        self.exited = False

    @contextlib.asynccontextmanager
    async def connect(self, *, model: str, config: Any = None) -> AsyncIterator[FakeSession]:
        self.connects.append((model, config))
        try:
            yield self.session
        finally:
            self.exited = True
            await self.session.close()


class FakeClient:
    """``client.aio.live`` only."""

    def __init__(self, session: FakeSession) -> None:
        self.live = FakeLive(session)
        self.aio = self


# ============================================================
#  Messages
# ============================================================


def vad_edge(t: types.VoiceActivityType) -> types.LiveServerMessage:
    return types.LiveServerMessage(voice_activity=types.VoiceActivity(voice_activity_type=t))


def server_content(**kwargs: Any) -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kwargs))


def model_text(t: str) -> types.LiveServerMessage:
    return server_content(model_turn=types.Content(role="model", parts=[types.Part(text=t)]))


def user(t: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=t)])


# ============================================================
#  Decode: the edges the net models, surfaced as distinct signals
# ============================================================


def test_vad_activity_start_decodes_to_speech_started() -> None:
    signals = GenaiLiveConnection.voice_signals(vad_edge(VA.ACTIVITY_START))
    assert signals == [VoiceSignal.SPEECH_STARTED]


def test_vad_activity_end_decodes_to_speech_stopped() -> None:
    signals = GenaiLiveConnection.voice_signals(vad_edge(VA.ACTIVITY_END))
    assert signals == [VoiceSignal.SPEECH_STOPPED]


def test_interrupted_content_decodes_to_interrupted() -> None:
    signals = GenaiLiveConnection.voice_signals(server_content(interrupted=True))
    assert signals == [VoiceSignal.INTERRUPTED]


def test_turn_complete_content_decodes_to_turn_complete() -> None:
    signals = GenaiLiveConnection.voice_signals(server_content(turn_complete=True))
    assert signals == [VoiceSignal.TURN_COMPLETE]


def test_interrupt_and_turn_complete_in_one_message_decode_to_both() -> None:
    msg = server_content(interrupted=True, turn_complete=True)
    assert GenaiLiveConnection.voice_signals(msg) == [
        VoiceSignal.INTERRUPTED,
        VoiceSignal.TURN_COMPLETE,
    ]


def test_message_with_no_modeled_edge_decodes_to_empty() -> None:
    # A setup-complete / usage-metadata / plain-audio frame carries no edge
    # the net models, so it must not spuriously inject anything.
    assert GenaiLiveConnection.voice_signals(types.LiveServerMessage()) == []
    setup = types.LiveServerMessage(setup_complete=types.LiveServerSetupComplete())
    assert GenaiLiveConnection.voice_signals(setup) == []


def test_unspecified_vad_type_decodes_to_empty() -> None:
    assert GenaiLiveConnection.voice_signals(vad_edge(VA.TYPE_UNSPECIFIED)) == []


# ============================================================
#  Integration: decoded signals reach the Vad/BargeIn subnets
# ============================================================


def place_for(signal: VoiceSignal) -> Place[None] | None:
    """The call-site routing the exemplar documents."""
    match signal:
        case VoiceSignal.SPEECH_STARTED:
            return VAD.Places.SPEECH_STARTED
        case VoiceSignal.SPEECH_STOPPED:
            return VAD.Places.SPEECH_STOPPED
        case VoiceSignal.INTERRUPTED:
            return BI.Places.INTERRUPTED
        case VoiceSignal.TURN_COMPLETE:
            return None  # no place in this fixture


def seed_for(signals: Sequence[VoiceSignal]) -> dict[str, list[None]]:
    initial: dict[str, list[None]] = {}
    for s in signals:
        p = place_for(s)
        if p is not None:
            initial.setdefault(p.name, []).append(None)
    return initial


def run(net: lp.BuiltNet, initial: Mapping[str, Sequence[Any]]) -> lp.MarkingView:
    return lp.run_sync(net, initial=dict(initial), event_store=lp.InMemoryEventStore())


VAD_BARGE_IN = NetSpec.compose("vad+bargein", VAD.DEF, BI.DEF)


def vad_barge_in_actions() -> dict[str, Any]:
    return merge(VAD.action_bindings(), BI.action_bindings())


def test_decoded_speech_start_opens_the_vad_window() -> None:
    signals = GenaiLiveConnection.voice_signals(vad_edge(VA.ACTIVITY_START))
    assert signals == [VoiceSignal.SPEECH_STARTED]

    net = NetSpec.compose("vad", VAD.DEF).build(VAD.action_bindings())
    m = run(net, seed_for(signals))

    assert m.count(VAD.VOICE_ACTIVITY_OPEN.name) == 1


def test_decoded_interrupt_while_window_open_routes_to_barge_in() -> None:
    net = VAD_BARGE_IN.build(vad_barge_in_actions())

    # Phase 1: a decoded ACTIVITY_START opens the window via VadSubnet.
    after_speech = run(
        net, seed_for(GenaiLiveConnection.voice_signals(vad_edge(VA.ACTIVITY_START)))
    )
    assert after_speech.count(VAD.VOICE_ACTIVITY_OPEN.name) == 1

    # Phase 2: carry the open window forward, then a decoded interrupt.
    phase2 = {
        VAD.VOICE_ACTIVITY_OPEN.name: list(after_speech.tokens(VAD.VOICE_ACTIVITY_OPEN.name)),
        **seed_for(GenaiLiveConnection.voice_signals(server_content(interrupted=True))),
    }
    after_interrupt = run(net, phase2)
    assert after_interrupt.count(BI.Places.BARGE_IN_SENT.name) == 1
    assert after_interrupt.count(BI.Places.INTERRUPT_DISCARDED.name) == 0


# ============================================================
#  Wire path (Python only): a fake session in place of the websocket
# ============================================================


def test_connection_satisfies_the_live_connection_protocol() -> None:
    assert isinstance(GenaiLiveConnection(FakeSession()), LiveConnection)


async def test_open_enters_connect_and_close_exits_it() -> None:
    session = FakeSession()
    client = FakeClient(session)
    config = types.LiveConnectConfig(response_modalities=[types.Modality.AUDIO])

    conn = await GenaiLiveConnection.open(client, "gemini-live", config)
    assert client.live.connects == [("gemini-live", config)]
    assert not client.live.exited

    await conn.close()
    await conn.close()  # idempotent
    assert client.live.exited
    assert session.closed
    assert conn.closed


async def test_sends_map_onto_the_session_with_explicit_turn_control() -> None:
    session = FakeSession()
    conn = GenaiLiveConnection(session)
    blob = types.Blob(data=b"\x01\x02", mime_type="audio/pcm;rate=16000")
    history = [user("earlier"), types.Content(role="model", parts=[types.Part(text="ok")])]

    await conn.send_content(user("hi"))
    await conn.send_client_content(user("partial"), False)
    await conn.send_history(history)
    await conn.send_realtime(blob)

    assert session.client_content == [
        {"turns": [user("hi")], "turn_complete": True},
        {"turns": [user("partial")], "turn_complete": False},
        {"turns": history, "turn_complete": False},
    ]
    assert session.realtime == [{"media": blob}]


async def test_raw_receive_spans_turns_until_close() -> None:
    # genai's receive() ends at each turn_complete; the raw stream does not.
    session = FakeSession()
    conn = GenaiLiveConnection(session)
    got: list[types.LiveServerMessage] = []

    async def consume() -> None:
        async for msg in conn.raw_receive():
            got.append(msg)

    task = asyncio.ensure_future(consume())
    turn_one = [model_text("one"), server_content(turn_complete=True)]
    turn_two = [vad_edge(VA.ACTIVITY_START), model_text("two")]
    for m in [*turn_one, *turn_two]:
        session.push(m)
    await until(lambda: len(got) == 4)
    assert got == [*turn_one, *turn_two]
    assert session.receive_calls == 2

    await conn.close()
    # The socket error our own close causes ends the stream cleanly.
    await asyncio.wait_for(task, 2)


async def test_a_transport_error_before_close_fails_the_raw_stream() -> None:
    session = FakeSession()
    conn = GenaiLiveConnection(session)
    boom = errors.APIError(1006, {"message": "abnormal closure"})
    session.push(boom)

    with pytest.raises(errors.APIError) as raised:
        async for _ in conn.raw_receive():
            pass
    assert raised.value is boom


async def test_receive_is_adk_shaped_and_keeps_voice_activity() -> None:
    session = FakeSession()
    conn = GenaiLiveConnection(session)
    session.push(types.LiveServerMessage(setup_complete=types.LiveServerSetupComplete()))
    session.push(vad_edge(VA.ACTIVITY_START))
    session.push(model_text("hello"))
    session.push(server_content(interrupted=True, turn_complete=True))

    responses = []
    async for r in conn.receive():
        responses.append(r)
        if len(responses) == 3:
            break
    await conn.close()

    # setup_complete carries nothing ADK's LlmResponse models: dropped.
    vad, text, end = responses
    assert vad.voice_activity is not None
    assert vad.voice_activity.voice_activity_type == VA.ACTIVITY_START
    assert vad.content is None
    assert text.content is not None and text.content.parts[0].text == "hello"
    assert end.interrupted is True and end.turn_complete is True


async def test_bridge_drives_a_vad_barge_in_net_from_genai_frames(orch) -> None:
    """The whole seam: genai frames -> decode -> env places -> structural barge-in."""

    def decode(msg: types.LiveServerMessage, r: PetriRunner) -> None:
        for s in GenaiLiveConnection.voice_signals(msg):
            p = place_for(s)
            if p is not None:
                r.signal(p)

    runner = await (
        PetriRunner.builder(VAD_BARGE_IN, vad_barge_in_actions())
        .environment_places(
            VAD.Places.SPEECH_STARTED, VAD.Places.SPEECH_STOPPED, BI.Places.INTERRUPTED
        )
        .orchestrator(orch)
        .astart()
    )
    session = FakeSession()
    conn = GenaiLiveConnection(session)
    queue = LiveRequestQueue()
    out = bridge(queue, conn, runner, decode)
    pump = asyncio.ensure_future(out.__anext__())
    try:
        # The user speaks, then the server reports a barge-in.
        session.push(vad_edge(VA.ACTIVITY_START))
        await until_marked(runner, VAD.VOICE_ACTIVITY_OPEN)
        session.push(server_content(interrupted=True))
        await until_marked(runner, BI.Places.BARGE_IN_SENT)

        # The user stops; a late interrupt is now discarded, structurally.
        session.push(vad_edge(VA.ACTIVITY_END))
        await until_marked(runner, VAD.Places.UTTERANCE_ENDED)
        session.push(server_content(interrupted=True))
        await until_marked(runner, BI.Places.INTERRUPT_DISCARDED)

        # The input pump forwards audio to the session.
        blob = types.Blob(data=b"\x09", mime_type="audio/pcm")
        queue.send_realtime(blob)
        await until(lambda: session.realtime == [{"media": blob}])
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await pump
        await out.aclose()
        await runner.aclose()

    # Disposing the egress closes the connection.
    assert session.closed
    final = await runner.wait_closed()
    assert final.count(BI.Places.BARGE_IN_SENT.name) == 1
    assert final.count(BI.Places.INTERRUPT_DISCARDED.name) == 1
