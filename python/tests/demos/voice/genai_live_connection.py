"""A :class:`~adk_libpetri.runner.live_connection.LiveConnection` over genai's Live session.

Port of Java ``SyncGeminiLiveConnection`` (Java: ``client.async.live``; Python:
``client.aio.live.connect(...)``, an async context manager yielding an
``AsyncSession``). An exemplar to copy and adapt, not library code: binding to
the Live API is tied to a genai SDK version, so the consumer owns it
(commitment 7). No ``google.adk`` class is overridden or patched (commitment 4).

**What it carries.**

* :meth:`raw_receive` exposes the unabstracted ``LiveServerMessage`` stream,
  and :meth:`voice_signals` decodes the edges a voice net models: speech
  start/stop, barge-in interrupt, turn complete.
* :meth:`send_client_content` keeps explicit ``turn_complete`` control.
  ``send_content`` (the protocol method) completes the turn, as ADK's does.

**Python differs from Java on why.** In Java, ADK's ``GeminiLlmConnection``
drops the VAD edges, which is the reason this class exists. ADK Python 2.11
forwards them (``LlmResponse.voice_activity``, then ``Event.voice_activity``;
see ``test_voice_vad_edge_adk_foil``), so here the class is the *full-control*
route: one long-lived raw stream across turns, with no LlmResponse shaping in
between. That matters for the bridge, which needs the frames themselves.

**One stream across turns.** genai's ``AsyncSession.receive()`` ends after each
completed interaction (``turn_complete``, or ``IDLE`` status). The bridge wants
the whole session, so :meth:`raw_receive` re-enters ``receive()`` until the
connection closes, and a transport error raised *because we closed* ends it
cleanly instead of failing the stream.

**How decoded edges reach the net.** Through env places only (commitment 1),
from the bridge's ``on_server_message`` callback; model content too, so a net
transition authors every outbound ``Event``::

    conn = await GenaiLiveConnection.open(client, "gemini-live-2.5-flash", config)
    events = bridge(queue, conn, runner, decode)

    def decode(msg: types.LiveServerMessage, r: PetriRunner) -> None:
        sc = msg.server_content
        if sc is not None and sc.model_turn is not None:
            r.inject(MODEL_CHUNK, sc.model_turn)       # a transition authors the Event
        for s in GenaiLiveConnection.voice_signals(msg):
            match s:
                case VoiceSignal.SPEECH_STARTED: r.signal(vad_subnet.Places.SPEECH_STARTED)
                case VoiceSignal.SPEECH_STOPPED: r.signal(vad_subnet.Places.SPEECH_STOPPED)
                case VoiceSignal.INTERRUPTED: r.signal(barge_in_subnet.Places.INTERRUPTED)
                case VoiceSignal.TURN_COMPLETE: r.signal(TURN_COMPLETE)

The decode is fire-and-forget. Ordering a turn's partials ahead of its terminal
is the net's job: inhibit the terminal transition on the chunk place.
"""

from __future__ import annotations

import contextlib
import enum
from collections.abc import AsyncIterator, Sequence
from typing import Any

from google.adk.models.llm_response import LlmResponse
from google.genai import types


class VoiceSignal(enum.Enum):
    """The voice edges a net cares about, decoded from a ``LiveServerMessage``."""

    SPEECH_STARTED = "speech_started"
    SPEECH_STOPPED = "speech_stopped"
    INTERRUPTED = "interrupted"
    TURN_COMPLETE = "turn_complete"


class GenaiLiveConnection:
    """Satisfies the ``LiveConnection`` protocol; build with :meth:`open`.

    The constructor takes an already-open session (what tests and callers
    that manage ``connect`` themselves use). ``exit_stack``, when given, owns
    the ``connect`` context and is closed by :meth:`close`.

    One consumer of :meth:`raw_receive` at a time: the bridge's output pump.
    """

    def __init__(self, session: Any, exit_stack: contextlib.AsyncExitStack | None = None) -> None:
        if session is None:
            raise TypeError("session is required")
        self._session = session
        self._stack = exit_stack
        self._closed = False

    @classmethod
    async def open(
        cls, client: Any, model: str, config: types.LiveConnectConfigOrDict | None = None
    ) -> GenaiLiveConnection:
        """Enter ``client.aio.live.connect(model=..., config=...)`` and wrap the session."""
        stack = contextlib.AsyncExitStack()
        try:
            session = await stack.enter_async_context(
                client.aio.live.connect(model=model, config=config)
            )
        except BaseException:
            await stack.aclose()
            raise
        return cls(session, stack)

    @property
    def closed(self) -> bool:
        return self._closed

    # ---- decode: the edges the net models -----------------------------------

    @staticmethod
    def voice_signals(msg: types.LiveServerMessage) -> list[VoiceSignal]:
        """Pure decode of one message. A message may carry several edges (an
        interrupt alongside a turn complete); empty when it carries none the net
        models (setup complete, usage metadata, plain audio)."""
        if msg is None:
            raise TypeError("msg is required")
        signals: list[VoiceSignal] = []
        va = msg.voice_activity
        if va is not None:
            if va.voice_activity_type == types.VoiceActivityType.ACTIVITY_START:
                signals.append(VoiceSignal.SPEECH_STARTED)
            elif va.voice_activity_type == types.VoiceActivityType.ACTIVITY_END:
                signals.append(VoiceSignal.SPEECH_STOPPED)
        sc = msg.server_content
        if sc is not None:
            if sc.interrupted:
                signals.append(VoiceSignal.INTERRUPTED)
            if sc.turn_complete:
                signals.append(VoiceSignal.TURN_COMPLETE)
        return signals

    # ---- LiveConnection -----------------------------------------------------

    async def raw_receive(self) -> AsyncIterator[types.LiveServerMessage]:
        """Every server message of the session, across turns, until :meth:`close`."""
        try:
            while not self._closed:
                async for msg in self._session.receive():
                    if self._closed:
                        return
                    yield msg
        except Exception:
            if self._closed:
                return  # the socket error our own close caused
            raise

    async def send_client_content(self, content: types.Content, turn_complete: bool) -> None:
        """Send client content with explicit ``turn_complete`` control."""
        if content is None:
            raise TypeError("content is required")
        await self._session.send_client_content(turns=[content], turn_complete=turn_complete)

    async def send_content(self, content: types.Content) -> None:
        await self.send_client_content(content, True)

    async def send_history(self, history: Sequence[types.Content]) -> None:
        """Replay history without completing a turn (Java leaves ``turnComplete`` unset)."""
        await self._session.send_client_content(turns=list(history), turn_complete=False)

    async def send_realtime(self, blob: types.Blob) -> None:
        if blob is None:
            raise TypeError("blob is required")
        await self._session.send_realtime_input(media=blob)

    async def close(self) -> None:
        """Idempotent. Ends :meth:`raw_receive`, then closes the session."""
        if self._closed:
            return
        self._closed = True
        if self._stack is not None:
            await self._stack.aclose()
        else:
            await self._session.close()

    # ---- ADK-shaped egress --------------------------------------------------

    async def receive(self) -> AsyncIterator[LlmResponse]:
        """``raw_receive`` mapped to ``LlmResponse``: content, ``turn_complete``,
        ``interrupted`` and (unlike Java, whose ``LlmResponse`` has no such field)
        ``voice_activity``. Messages carrying none of these are dropped."""
        async for msg in self.raw_receive():
            response = to_llm_response(msg)
            if response is not None:
                yield response


def to_llm_response(msg: types.LiveServerMessage) -> LlmResponse | None:
    sc = msg.server_content
    content = sc.model_turn if sc is not None else None
    turn_complete = sc.turn_complete if sc is not None else None
    interrupted = sc.interrupted if sc is not None else None
    if (
        content is None
        and turn_complete is None
        and interrupted is None
        and msg.voice_activity is None
    ):
        return None
    return LlmResponse(
        content=content,
        turn_complete=turn_complete,
        interrupted=interrupted,
        voice_activity=msg.voice_activity,
    )
