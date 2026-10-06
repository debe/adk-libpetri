"""Port of Java ``VoiceSessionDemoTest``: a BIDI/Live voice session as one long-lived net.

Composes the streaming subnet, barge-in and silence recovery into one per-user
net, driven through the **stock ADK ``Runner``** via ``PetriAgent``.

What the demo shows:

1. **Multi-env-place injection through the ADK adapter.** The runner declares
   six typed env places: ``USER_IN`` for ADK ingress plus five voice signals.
   Unit signals go through ``runner.signal(place)`` from any thread; typed side
   channels would use ``runner.inject(place, value)``.
2. **Per-chunk env-place injection.** ``LlmStreamingStep`` injects each partial
   through its own net's ``CHUNK`` place, so egress subscribers see partial
   events as they arrive, not at the end.
3. **Barge-in via the read/inhibitor pair** on one shared voice window.
4. **Two-stage silence recovery:** Nudge after 80 ms of silence, Reconnect 80 ms
   after that.
5. **``VoiceDemo_StartStream``**, the recipe for turning an ADK turn
   (``USER_IN``) into a request token for a downstream subnet.

Python differences:

* Java's ``deferredExecutorRef`` is ``handle_ref``; ``runner.executor().marking()``
  is ``(await runner.snapshot()).marking``.
* The silence-recovery steps run on ``ManualClock`` in Java and on libpetri's
  ``SteppedClock`` here.
* Java's BIDI test chains an ``OtelEventStore`` through the runner and checks
  the bridging and the session root span in one test; here they are two tests.
* Java's live connection pump runs its inject inside ``onNext``, synchronously
  with the frame. A Python async pump injects on its next turn of the loop, so
  the test waits for the pump's injects before signalling the turn edge: that
  keeps the race the inhibitor is there to win (the chunks are injected,
  fire-and-forget, but maybe not yet processed).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import libpetri as lp
import pytest
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop, on_loop
from adk_libpetri._spec import Action, Ctx, NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.bridge import OtelEventStore
from adk_libpetri.bridge.otel_event_store import context_with_span
from adk_libpetri.runner import (
    HandleRef,
    PetriAgent,
    PetriRunner,
    SessionExecutorRegistry,
    SessionKey,
)
from adk_libpetri.subnet import llm_streaming_step as LS
from adk_libpetri.subnet import merge, router
from demos.voice import barge_in_subnet as BI
from demos.voice import live_api_recovery_subnet as R
from demos.voice._support import (
    advance_and_settle,
    marking,
    settle,
    stepped_clock,
    until,
    until_marked,
    until_settled,
)
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import assert_each_proven, requires_z3

FAST_RECOVERY = R.Config(timedelta(milliseconds=80), timedelta(milliseconds=80))
NUDGE_MS = 80
RECONNECT_MS = 80

T_START_STREAM = "VoiceDemo_StartStream"


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("voice-demo-orchestrator")
    yield loop
    loop.close()


# ============================================================
#  Fixtures
# ============================================================


def user_message(t: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=t)])


def model_message(t: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=t)])


def text_of(c: types.Content | Event | LlmRequest | None) -> str:
    content = c.content if isinstance(c, Event) else c
    if not isinstance(content, types.Content) or not content.parts:
        return ""
    return "".join(p.text or "" for p in content.parts)


def request_for(user_content: types.Content) -> LlmRequest:
    return LlmRequest(model="fake", contents=[user_content])


def streaming_llm(chunks: list[LlmResponse]) -> ScriptedLlm:
    return ScriptedLlm.of().stream(0, chunks)


START_STREAM = TransitionSpec(T_START_STREAM, (one(C.USER_IN),), out(C.LLM_REQUEST))


def start_stream(ctx: Ctx) -> None:
    ctx.output(C.LLM_REQUEST, request_for(ctx.input(C.USER_IN)))


@dataclass
class Egress:
    """Every event a runner's egress publishes, collected from subscription on."""

    events: list[Event] = field(default_factory=list)
    task: asyncio.Task[None] | None = None

    @classmethod
    def of(cls, runner: PetriRunner) -> Egress:
        egress = cls()
        sub = runner.adk_events().subscribe()

        async def collect() -> None:
            async for e in sub:
                egress.events.append(e)

        egress.task = asyncio.ensure_future(collect())
        return egress

    def partials(self) -> list[Event]:
        return [e for e in self.events if e.partial]

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


async def run_adk_turn(adk: InMemoryRunner, session: Any, message: str) -> list[Event]:
    async def collect() -> list[Event]:
        return [
            e
            async for e in adk.run_async(
                user_id=session.user_id, session_id=session.id, new_message=user_message(message)
            )
        ]

    return await asyncio.wait_for(collect(), 5)


# ============================================================
#  1. ADK-driven voice session: partials, barge-in, silence recovery
# ============================================================


def voice_session_spec() -> NetSpec:
    return NetSpec.compose(
        "voice-session",
        LS.DEF,
        router.DEF,
        BI.DEF,
        R.def_(FAST_RECOVERY),
        START_STREAM,
    )


VOICE_ENV = (
    C.USER_IN,
    LS.Places.CHUNK,
    BI.Places.INTERRUPTED,
    BI.Places.VOICE_ACTIVITY_OPEN,
    R.Places.RESPONSE_AWAITED,
    R.Places.MODEL_ACTIVE,
)


@dataclass
class VoiceSession:
    registry: SessionExecutorRegistry
    adk: InMemoryRunner
    session: Any
    runner: PetriRunner
    egress: Egress

    async def close(self) -> None:
        await self.egress.stop()
        await self.registry.aclose_all()


async def open_voice_session(orch: OrchestratorLoop, clock: Any = None) -> VoiceSession:
    # 1. Compose the BIDI net plus StartStream (USER_IN -> LLM_REQUEST).
    spec = voice_session_spec()
    ref = HandleRef()
    llm = streaming_llm([text("Sure, "), text("the answer "), text("is 42.")])
    actions = merge(
        LS.action_bindings(llm, LS.Config("voice_agent", ref)),
        router.action_bindings(router.Config("voice_agent")),
        BI.action_bindings(),
        R.action_bindings(FAST_RECOVERY),
        {T_START_STREAM: start_stream},
    )

    # 2. The ADK-integrated runner with six typed env places: one for the ADK
    #    utterance, five for voice signals.
    registry = SessionExecutorRegistry.strong_owned()

    def factory(key: SessionKey) -> Any:
        b = (
            PetriRunner.builder(spec, actions)
            .environment_places(*VOICE_ENV)
            .handle_ref(ref)
            .orchestrator(orch)
        )
        if clock is not None:
            b = b.clock(clock).deadline_tolerance(timedelta(0))
        return b.astart()

    agent = (
        PetriAgent.builder("voice_agent", registry, factory)
        .description("BIDI voice agent: streaming, barge-in, silence recovery")
        .build()
    )
    adk = InMemoryRunner(agent=agent, app_name="voice-demo")
    session = await adk.session_service.create_session(
        app_name=adk.app_name, user_id="user-1", session_id="session-1"
    )

    # 3. Create the per-session runner BEFORE any side-channel inject, the
    #    realistic pattern (websocket open creates the runner first). It also
    #    fills the handle ref the streaming subnet injects chunks through.
    runner = await registry.aget_or_create(SessionKey.of(session), factory)

    # 4. Subscribe to ALL partials before driving the turn: the ADK turn
    #    returns only the terminal event, the partials land on egress.
    return VoiceSession(registry, adk, session, runner, Egress.of(runner))


async def assert_turn_streamed(s: VoiceSession) -> None:
    # 6. Drive an ADK turn. PetriAgent injects USER_IN; StartStream turns it
    #    into LLM_REQUEST; the streaming subnet streams; Router turns the
    #    merged response into the terminal event of the non-SSE turn.
    events = await run_adk_turn(s.adk, s.session, "what's 6*7")
    terminal = next(e for e in events if e.author == "voice_agent")
    assert not terminal.partial
    assert text_of(terminal) == "Sure, the answer is 42."


async def assert_voice_outcomes(s: VoiceSession) -> None:
    # 8. Partials seen on the ADK egress, marking-level facts on the runner.
    await until(lambda: len(s.egress.partials()) >= 3)
    assert len(s.egress.partials()) == 3

    m = await marking(s.runner)
    # Barge-in routed to BARGE_IN_SENT: the voice window was open.
    assert m.count(BI.Places.BARGE_IN_SENT.name) == 1
    assert m.count(BI.Places.INTERRUPT_DISCARDED.name) == 0
    # Silence recovery fired both stages.
    assert m.count(R.Places.NUDGE_NEEDED.name) >= 1
    assert m.count(R.Places.RECONNECT_NEEDED.name) >= 1


async def test_adk_driven_voice_session_streams_partials_handles_barge_in_and_recovers_silence(
    orch,
) -> None:
    clock = stepped_clock()
    s = await open_voice_session(orch, clock)
    try:
        # 5. The voice window opens before the utterance arrives, as an audio
        #    frontend signals when it detects voice activity.
        await settle(clock, lambda: s.runner.signal(BI.Places.VOICE_ACTIVITY_OPEN))
        await assert_turn_streamed(s)

        # 7. Mid/post-stream signals: a barge-in while the window is open, and
        #    a response-awaited the recovery subnet times out into Nudge, then
        #    Reconnect. Each step settles before time moves, so the timers
        #    start exactly where the scenario says.
        await settle(clock, lambda: s.runner.signal(BI.Places.INTERRUPTED))
        await settle(clock, lambda: s.runner.signal(R.Places.RESPONSE_AWAITED))
        await advance_and_settle(clock, NUDGE_MS)
        await advance_and_settle(clock, RECONNECT_MS)

        await assert_voice_outcomes(s)
    finally:
        await s.close()


# ============================================================
#  2. BIDI: a live connection bridged into the net by env-place injection.
#     The connection's send side runs from a transition action; its receive
#     stream is pumped frame by frame into LLM_RESPONSE by the application
#     layer. The connection and its pump are application code.
# ============================================================

BIDI_TURN_COMPLETE: Place[None] = Place("bidiDemo_turnComplete")
"""Turn edge the application signals from the transport's turn-complete frame.
The net, not the bridge, turns it into the terminal event."""


class MockLiveConnection:
    """Records sends; the test pushes frames as if the model were talking back."""

    def __init__(self) -> None:
        self.sent_contents: list[types.Content] = []
        self.sent_realtime: list[types.Blob] = []
        self.sent_history: list[types.Content] = []
        self._frames: asyncio.Queue[LlmResponse | None] = asyncio.Queue()

    async def send_history(self, history: list[types.Content]) -> None:
        self.sent_history.extend(history)

    async def send_content(self, content: types.Content) -> None:
        self.sent_contents.append(content)

    async def send_realtime(self, blob: types.Blob) -> None:
        self.sent_realtime.append(blob)

    async def receive(self) -> AsyncIterator[LlmResponse]:
        while (frame := await self._frames.get()) is not None:
            yield frame

    def push_response(self, response: LlmResponse) -> None:
        self._frames.put_nowait(response)

    async def close(self) -> None:
        self._frames.put_nowait(None)


def bidi_spec() -> NetSpec:
    # LLM_REQUEST  --Bidi_SendToConnection (action: connection.send_content)
    # LLM_RESPONSE --Bidi_RouteResponse--> EVENT_OUT   partial event
    # TURN_COMPLETE --Bidi_EmitTurnEnd--> EVENT_OUT    terminal event
    #                  o---[LLM_RESPONSE]  no terminal while chunks are queued
    #
    # The turn shape is the NET's: partial and turn_complete are set by the
    # transition that fired, not by the transport bridge. The inhibitor makes
    # egress order an arc, not a rule the application must remember.
    return NetSpec.compose(
        "bidi-bridge",
        BI.DEF,
        TransitionSpec("Bidi_SendToConnection", (one(C.LLM_REQUEST),)),
        TransitionSpec("Bidi_RouteResponse", (one(C.LLM_RESPONSE),), out(C.EVENT_OUT)),
        TransitionSpec(
            "Bidi_EmitTurnEnd",
            (one(BIDI_TURN_COMPLETE),),
            out(C.EVENT_OUT),
            inhibitors=(C.LLM_RESPONSE,),
        ),
    )


def bidi_actions(connection: MockLiveConnection) -> dict[str, Action]:
    async def send_to_connection(ctx: Ctx) -> None:
        request = ctx.input(C.LLM_REQUEST)
        await on_loop(connection.send_content(request.contents[0]))

    # Sync, so each chunk's event leaves within its firing, in arrival order.
    def route_response(ctx: Ctx) -> None:
        resp = ctx.input(C.LLM_RESPONSE)
        ctx.output(
            C.EVENT_OUT,
            Event(
                invocation_id="bidi-1",
                author="bidi_agent",
                content=resp.content or types.Content(),
                partial=True,
            ),
        )

    def emit_turn_end(ctx: Ctx) -> None:
        ctx.input(BIDI_TURN_COMPLETE)
        ctx.output(
            C.EVENT_OUT,
            Event(invocation_id="bidi-1", author="bidi_agent", partial=False, turn_complete=True),
        )

    return merge(
        BI.action_bindings(),
        {
            "Bidi_SendToConnection": send_to_connection,
            "Bidi_RouteResponse": route_response,
            "Bidi_EmitTurnEnd": emit_turn_end,
        },
    )


BIDI_ENV = (
    C.LLM_REQUEST,
    C.LLM_RESPONSE,
    BI.Places.INTERRUPTED,
    BI.Places.VOICE_ACTIVITY_OPEN,
    BIDI_TURN_COMPLETE,
)


async def run_bidi_session(orch: OrchestratorLoop, event_store: Any) -> None:
    connection = MockLiveConnection()
    spec = bidi_spec()
    actions = bidi_actions(connection)

    # 3. Runner and agent. The connection lives outside the net; the test
    #    wires connection.receive() -> runner.inject(LLM_RESPONSE) itself, as
    #    a real deployment's websocket handler does once per session.
    registry = SessionExecutorRegistry.strong_owned()

    def factory(key: SessionKey) -> Any:
        return (
            PetriRunner.builder(spec, actions)
            .environment_places(*BIDI_ENV)
            .event_store(event_store)
            .orchestrator(orch)
            .astart()
        )

    agent = (
        PetriAgent.builder("bidi_agent", registry, factory)
        .description("BIDI bridge demo via a custom live connection")
        .build()
    )
    pump: asyncio.Task[None] | None = None
    egress: Egress | None = None
    try:
        adk = InMemoryRunner(agent=agent, app_name="bidi-demo")
        session = await adk.session_service.create_session(
            app_name=adk.app_name, user_id="u", session_id="s"
        )
        runner = await registry.aget_or_create(SessionKey.of(session), factory)

        # The application-layer bridge from connection.receive() into the net.
        pumped = 0

        async def receive_pump() -> None:
            nonlocal pumped
            async for frame in connection.receive():
                runner.inject(C.LLM_RESPONSE, frame)
                pumped += 1

        pump = asyncio.ensure_future(receive_pump())
        # Observe egress as the BIDI client would (PetriAgent's run_live
        # without a LiveConfig returns runner.adk_events()).
        egress = Egress.of(runner)

        # 4. Drive the loop: user content to the model through the net, then
        #    two response frames from "Gemini" on the connection's stream.
        assert runner.inject(
            C.LLM_REQUEST,
            LlmRequest(model="gemini-live", contents=[user_message("what's the weather")]),
        )
        # Open the voice window, to exercise barge-in.
        assert runner.signal(BI.Places.VOICE_ACTIVITY_OPEN)

        connection.push_response(LlmResponse(content=model_message("Sunny, ")))
        connection.push_response(LlmResponse(content=model_message("with light wind.")))
        # Signalled as soon as the pump has injected both chunks, which it does
        # fire-and-forget: both may still sit in LLM_RESPONSE. EmitTurnEnd's
        # inhibitor on LLM_RESPONSE keeps the terminal behind them.
        await until(lambda: pumped == 2)
        runner.signal(BIDI_TURN_COMPLETE)
        runner.signal(BI.Places.INTERRUPTED)

        # Wait for what is asserted, not for quiescence.
        await until(lambda: len(egress.events) >= 3)
        await until_marked(runner, BI.Places.BARGE_IN_SENT)

        # 5. The send went to the connection; two partials, then one
        #    net-authored terminal event; the barge-in routed to BARGE_IN_SENT.
        assert len(connection.sent_contents) == 1
        assert text_of(connection.sent_contents[0]) == "what's the weather"

        agent_events = [e for e in egress.events if e.author == "bidi_agent"]
        assert len(agent_events) == 3
        assert text_of(agent_events[0]) == "Sunny, "
        assert agent_events[0].partial is True
        assert text_of(agent_events[1]) == "with light wind."
        assert agent_events[1].partial is True
        # The turn boundary is the net's: EmitTurnEnd authored it, so ADK's
        # run_live consumers can tell partials from finals.
        assert agent_events[2].partial is False
        assert agent_events[2].turn_complete is True

        assert (await marking(runner)).count(BI.Places.BARGE_IN_SENT.name) == 1
    finally:
        await connection.close()
        if pump is not None:
            await asyncio.wait_for(pump, 2)
        if egress is not None:
            await egress.stop()
        await registry.aclose_all()


async def test_bidi_voice_via_live_connection_bridges_frames_through_net(orch) -> None:
    await run_bidi_session(orch, lp.InMemoryEventStore())


async def test_bidi_session_transition_spans_are_children_of_the_session_root_span(
    orch,
) -> None:
    # 0. One session-long root span. Unlike PetriAgent's per-invocation span,
    #    a BIDI session is long-lived and has side-channel transitions
    #    (BargeIn) firing between turns; they need a parent too. The
    #    application binds ONE root span to OtelEventStore for the runner's
    #    lifetime, and every transition span attaches to it.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("voice-bidi-demo")
    root = tracer.start_span("VoiceSession", attributes={"session.kind": "bidi"})
    store = OtelEventStore(tracer, lp.InMemoryEventStore(), subnet_of=bidi_spec().subnet_of)

    with store.bind_invocation_context(context_with_span(root)):
        await run_bidi_session(orch, store)
    root.end()
    provider.force_flush()

    # 6. Every transition span, including BargeIn's (fired by a background
    #    env-place injection with no ADK invocation), is a child of the root.
    spans = exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "VoiceSession"]
    assert len(roots) == 1
    root_id = roots[0].context.span_id
    transition_spans = [s for s in spans if s.name != "VoiceSession"]
    assert transition_spans
    assert BI.Transitions.SEND_BARGE_IN in {s.name for s in transition_spans}
    for s in transition_spans:
        assert s.parent is not None and s.parent.span_id == root_id, s.name
    provider.shutdown()


# ============================================================
#  3. Barge-in chunk drop: what net-authored egress buys. When the bridge
#     maps frames straight to events, model content never enters the
#     marking and a barge-in can only stop *future* chunks. With content
#     in a place, the queued backlog is a reset arc away.
# ============================================================

BIDI_CHUNKS_DROPPED: Place[None] = Place("bidiDemo_chunksDropped")
"""Observed: a barge-in wiped the queued turn."""


def barge_in_drop_spec() -> NetSpec:
    """Stock barge-in plus ``Bidi_EmitChunk`` and ``Bidi_DropQueuedTurn``, unbound::

    LLM_RESPONSE --Bidi_EmitChunk--> EVENT_OUT
                     o---[BARGE_IN_SENT]   stop emitting once barge-in is decided
    BARGE_IN_SENT --Bidi_DropQueuedTurn--> CHUNKS_DROPPED
                     reset(LLM_RESPONSE)   wipe the backlog
    """
    return NetSpec.compose(
        "barge-in-drop",
        BI.DEF,
        TransitionSpec(
            "Bidi_EmitChunk",
            (one(C.LLM_RESPONSE),),
            out(C.EVENT_OUT),
            inhibitors=(BI.Places.BARGE_IN_SENT,),
        ),
        TransitionSpec(
            "Bidi_DropQueuedTurn",
            (one(BI.Places.BARGE_IN_SENT),),
            out(BIDI_CHUNKS_DROPPED),
            resets=(C.LLM_RESPONSE,),
        ),
        extra_places=(BIDI_CHUNKS_DROPPED,),
    )


async def test_barge_in_structurally_drops_the_queued_model_chunks(orch) -> None:
    def emit_chunk(ctx: Ctx) -> None:
        resp = ctx.input(C.LLM_RESPONSE)
        # Emission costs something real (a socket write), which is why a
        # backlog builds up in LLM_RESPONSE faster than it drains.
        time.sleep(0.005)
        ctx.output(
            C.EVENT_OUT,
            Event(
                invocation_id="barge-1",
                author="bidi_agent",
                content=resp.content or types.Content(),
                partial=True,
            ),
        )

    def drop_queued_turn(ctx: Ctx) -> None:
        ctx.input(BI.Places.BARGE_IN_SENT)
        ctx.signal(BIDI_CHUNKS_DROPPED)

    actions = merge(
        BI.action_bindings(),
        {"Bidi_EmitChunk": emit_chunk, "Bidi_DropQueuedTurn": drop_queued_turn},
    )
    runner = await (
        PetriRunner.builder(barge_in_drop_spec(), actions)
        .environment_places(C.LLM_RESPONSE, BI.Places.INTERRUPTED, BI.Places.VOICE_ACTIVITY_OPEN)
        .orchestrator(orch)
        .astart()
    )
    egress = Egress.of(runner)
    try:
        # The user is speaking, so the interrupt is a genuine barge-in.
        runner.signal(BI.Places.VOICE_ACTIVITY_OPEN)

        # A model turn streams in faster than it can be emitted, then the user
        # cuts in. Everything still queued must never reach the client.
        pushed = 8
        for i in range(pushed):
            runner.inject(C.LLM_RESPONSE, LlmResponse(content=model_message(f"chunk {i}")))
        runner.signal(BI.Places.INTERRUPTED)

        # Wait on the property, not on quiescence: a quiescence poll right
        # after an inject can read the pre-injection state.
        await until_marked(runner, BIDI_CHUNKS_DROPPED, timeout=5)
        m = await until_settled(runner, timeout=5)

        assert m.count(BIDI_CHUNKS_DROPPED.name) == 1
        # The backlog is gone from the marking, not merely unsubscribed downstream.
        assert m.count(C.LLM_RESPONSE.name) == 0
        # And it never became an Event, which a bridge-authored egress could
        # not guarantee at any marking: its chunks were Events on arrival.
        assert len(egress.events) < pushed
    finally:
        await egress.stop()
        await runner.aclose()


# ============================================================
#  4. Reset-arc demo: each new utterance wipes the in-net intent place and
#     seeds the new one, through the ADK adapter and a dedicated env place.
# ============================================================

UTTERANCE_IN: Place[types.Content] = Place("voiceDemo_utteranceIn", types.Content)
"""Dedicated env place for utterance signals, separate from ADK's USER_IN."""

CURRENT_INTENT: Place[str] = Place("voiceDemo_currentIntent", str)
"""In-net derived intent: at most one token, wiped by the reset arc."""

T_ON_NEW_UTTERANCE = "VoiceDemo_OnNewUtterance"
T_ECHO_INTENT = "VoiceDemo_EchoIntent"


async def test_new_utterance_resets_in_net_intent_state_through_adk_egress(orch) -> None:
    # OnNewUtterance consumes UTTERANCE_IN, resets CURRENT_INTENT (wipes any
    # prior intent) and seeds the new one. EchoIntent consumes USER_IN, reads
    # CURRENT_INTENT and emits the response on EVENT_OUT.
    spec = NetSpec(
        "reset-arc-demo",
        (
            TransitionSpec(
                T_ON_NEW_UTTERANCE,
                (one(UTTERANCE_IN),),
                out(CURRENT_INTENT),
                resets=(CURRENT_INTENT,),
            ),
            TransitionSpec(
                T_ECHO_INTENT, (one(C.USER_IN),), out(C.EVENT_OUT), reads=(CURRENT_INTENT,)
            ),
        ),
        extra_places=(C.USER_IN, C.EVENT_OUT, UTTERANCE_IN, CURRENT_INTENT),
    )

    def on_new_utterance(ctx: Ctx) -> None:
        ctx.output(CURRENT_INTENT, "intent-from:" + text_of(ctx.input(UTTERANCE_IN)))

    def echo_intent(ctx: Ctx) -> None:
        ctx.input(C.USER_IN)
        ctx.output(
            C.EVENT_OUT,
            Event(
                invocation_id="reset-arc-demo",
                author="reset_arc_agent",
                content=model_message(ctx.read(CURRENT_INTENT)),
            ),
        )

    actions = {T_ON_NEW_UTTERANCE: on_new_utterance, T_ECHO_INTENT: echo_intent}
    registry = SessionExecutorRegistry.strong_owned()

    def factory(key: SessionKey) -> Any:
        return (
            PetriRunner.builder(spec, actions)
            .environment_places(C.USER_IN, UTTERANCE_IN)
            .orchestrator(orch)
            .astart()
        )

    agent = (
        PetriAgent.builder("reset_arc_agent", registry, factory)
        .description("Demonstrates reset-arc wipe of in-net intent state per utterance")
        .build()
    )
    try:
        adk = InMemoryRunner(agent=agent, app_name="reset-arc-demo")
        session = await adk.session_service.create_session(
            app_name=adk.app_name, user_id="u", session_id="s"
        )
        runner = await registry.aget_or_create(SessionKey.of(session), factory)

        # Side channel: two consecutive utterances. The second triggers the
        # reset arc, wiping the intent from the first.
        assert runner.inject(UTTERANCE_IN, user_message("what time is it"))
        assert runner.inject(UTTERANCE_IN, user_message("tell me a joke"))

        async def intent_settled() -> bool:
            m = await until_settled(runner)
            return m.count(UTTERANCE_IN.name) == 0 and m.tokens(CURRENT_INTENT.name) == (
                "intent-from:tell me a joke",
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        while not await intent_settled():
            assert loop.time() < deadline, "the second utterance never replaced the first"
            await asyncio.sleep(0.005)

        # The ADK turn: EchoIntent reads the surviving CURRENT_INTENT.
        events = await run_adk_turn(adk, session, "respond now")
        agent_event = next(e for e in events if e.author == "reset_arc_agent")
        assert text_of(agent_event) == "intent-from:tell me a joke"

        # Exactly ONE intent survives: the second utterance reset the first,
        # then deposited the new one. No stale leftover.
        m = await marking(runner)
        assert list(m.tokens(CURRENT_INTENT.name)) == ["intent-from:tell me a joke"]
    finally:
        await registry.aclose_all()


# ============================================================
#  5. The composed voice net is SMT-proven deadlock-free
# ============================================================


@requires_z3
def test_composed_voice_demo_net_is_smt_proven_deadlock_free() -> None:
    # Bind every composed subnet plus the local StartStream so the proof is
    # about the net that runs; the actions are never invoked, only the
    # structure is encoded.
    spec = NetSpec.compose("voice-dlf-check", LS.DEF, BI.DEF, R.def_(FAST_RECOVERY), START_STREAM)
    net = spec.build(
        merge(
            LS.action_bindings(streaming_llm([]), LS.Config("voice-verify", HandleRef())),
            BI.action_bindings(),
            R.action_bindings(FAST_RECOVERY),
            {T_START_STREAM: start_stream},
        )
    )
    env = [
        C.USER_IN,
        LS.Places.CHUNK,
        BI.Places.INTERRUPTED,
        BI.Places.VOICE_ACTIVITY_OPEN,
        R.Places.RESPONSE_AWAITED,
        R.Places.MODEL_ACTIVE,
        R.Places.MODEL_QUIET,
    ]
    sinks = [
        C.EVENT_OUT,
        C.LLM_RESPONSE,
        BI.Places.BARGE_IN_SENT,
        BI.Places.INTERRUPT_DISCARDED,
        R.Places.NUDGE_NEEDED,
        R.Places.RECONNECT_NEEDED,
        R.Places.QUIET_IGNORED,
    ]
    # Env places modelled with bounded(1): at most one resident token each,
    # refilled forever. Without an environment mode the verifier would answer
    # unknown, because a proof that ignores env places would be vacuous.
    assert_each_proven(
        net,
        {"deadlockFree": lp.deadlock_free()},
        initial_marking={C.USER_IN.name: 1},
        environment_places=[p.name for p in env],
        environment_mode=lp.bounded(1),
        sink_places=[p.name for p in sinks],
    )
