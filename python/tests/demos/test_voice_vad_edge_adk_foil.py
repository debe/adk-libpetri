"""Port of Java ``VoiceVadEdgeAdkFoilTest``, verdict flipped: ADK Python keeps the VAD edges.

**Java's foil.** ADK Java's ``GeminiLlmConnection.convertToServerResponse`` drops
the server-side VAD speech-activity edges: a VAD-only ``LiveServerMessage``
surfaces as an "Unknown server message" error, never a speech start. That is
why the Java exemplars read genai's Live session directly
(``SyncGeminiLiveConnection``) or tap ADK's live transport (``VadTapGemini``).
Java green-locks the drop so that a fix in ADK turns its test red.

**What ADK Python 2.11 does.** The opposite. ``GeminiLlmConnection.receive``
yields ``LlmResponse(voice_activity=...)`` for a VAD frame (no error), and the
live flow copies it onto ``Event.voice_activity`` and yields it to the
``Runner.run_live`` caller. So in Python a barge-in frontend built on stock ADK
*does* see speech start and stop. These tests green-lock that, running ADK's
real ``GeminiLlmConnection`` over a fake genai session (no network): if a
future ADK Python release drops the edges, they turn red, and the Python port
needs the direct-session route again.

**Consequences for the port.** No ``VadTapGemini`` counterpart: it exists in
Java only to recover what ADK Java drops, and ADK Python drops nothing. The
``GenaiLiveConnection`` exemplar stays, as the full-control route the BIDI
bridge needs (raw frames, one stream across turns), not as a workaround.

The narrow structural fact is the same in both languages: the edge rides
``voice_activity``, a sibling of ``server_content``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.gemini_llm_connection import GeminiLlmConnection
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import PrivateAttr

from demos.voice.genai_live_connection import GenaiLiveConnection, VoiceSignal

VA = types.VoiceActivityType


class FakeGenaiSession:
    """The ``AsyncSession`` surface ``GeminiLlmConnection`` reads: replays
    ``messages``, then stays open like a live socket."""

    session_id = "fake-live-session"

    def __init__(self, *messages: types.LiveServerMessage) -> None:
        self._messages = messages

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        for m in self._messages:
            yield m
        await asyncio.Event().wait()

    async def send(self, **_: Any) -> None:
        pass

    async def send_client_content(self, **_: Any) -> None:
        pass

    async def send_realtime_input(self, **_: Any) -> None:
        pass

    async def close(self) -> None:
        pass


def vad_edge(t: types.VoiceActivityType) -> types.LiveServerMessage:
    return types.LiveServerMessage(voice_activity=types.VoiceActivity(voice_activity_type=t))


async def adk_receive(msg: types.LiveServerMessage) -> LlmResponse:
    """ADK's real ``LiveServerMessage -> LlmResponse`` mapping, first response."""
    conn = GeminiLlmConnection(FakeGenaiSession(msg))
    agen = conn.receive()
    try:
        return await asyncio.wait_for(agen.__anext__(), 2)
    finally:
        await agen.aclose()


def assert_edge_kept(r: LlmResponse, t: types.VoiceActivityType) -> None:
    """The flip of Java's ``assertEdgeLost``: the edge arrives, and as no error."""
    assert r.voice_activity is not None
    assert r.voice_activity.voice_activity_type == t
    assert r.error_code is None
    assert r.error_message is None
    assert r.content is None
    assert not r.interrupted
    assert not r.turn_complete


async def test_adk_python_receive_keeps_the_vad_speech_start_edge_foil() -> None:
    speech_start = vad_edge(VA.ACTIVITY_START)

    # Petri path: the edge is decoded into a typed signal.
    assert GenaiLiveConnection.voice_signals(speech_start) == [VoiceSignal.SPEECH_STARTED]
    # ADK path: unlike ADK Java, its own conversion carries the edge.
    assert_edge_kept(await adk_receive(speech_start), VA.ACTIVITY_START)


async def test_adk_python_receive_keeps_the_vad_speech_stop_edge_foil() -> None:
    speech_stop = vad_edge(VA.ACTIVITY_END)

    assert GenaiLiveConnection.voice_signals(speech_stop) == [VoiceSignal.SPEECH_STOPPED]
    assert_edge_kept(await adk_receive(speech_stop), VA.ACTIVITY_END)


def test_the_vad_edge_lives_on_voice_activity_a_sibling_of_server_content_foil() -> None:
    speech_start = vad_edge(VA.ACTIVITY_START)
    assert speech_start.voice_activity is not None
    assert speech_start.server_content is None
    # And ADK Python's LlmResponse (hence Event) has a field for it; Java's has not.
    assert "voice_activity" in LlmResponse.model_fields


class LiveOverFakeSession(BaseLlm):
    """A ``BaseLlm`` whose live connection is ADK's own ``GeminiLlmConnection``."""

    model: str = "gemini-live-2.5-flash"
    _session: Any = PrivateAttr(default=None)

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        raise NotImplementedError
        yield  # pragma: no cover

    @contextlib.asynccontextmanager
    async def connect(self, llm_request: Any) -> AsyncIterator[GeminiLlmConnection]:
        yield GeminiLlmConnection(self._session)


async def test_stock_adk_run_live_surfaces_the_vad_edge_to_the_caller_foil() -> None:
    # The whole stock path: Runner.run_live -> live flow -> GeminiLlmConnection.
    llm = LiveOverFakeSession()
    llm._session = FakeGenaiSession(vad_edge(VA.ACTIVITY_START), vad_edge(VA.ACTIVITY_END))
    runner = InMemoryRunner(agent=LlmAgent(name="voice", model=llm), app_name="foil")
    session = await runner.session_service.create_session(app_name="foil", user_id="u")
    queue = LiveRequestQueue()

    async def edges() -> list[types.VoiceActivityType | None]:
        seen: list[types.VoiceActivityType | None] = []
        agen = runner.run_live(
            user_id="u", session_id=session.id, live_request_queue=queue, run_config=RunConfig()
        )
        try:
            async for event in agen:
                assert event.error_code is None, event.error_message
                if event.voice_activity is not None:
                    seen.append(event.voice_activity.voice_activity_type)
                    if len(seen) == 2:
                        return seen
        finally:
            queue.close()
            await agen.aclose()
        return seen

    assert await asyncio.wait_for(edges(), 5) == [VA.ACTIVITY_START, VA.ACTIVITY_END]
