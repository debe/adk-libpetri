"""Port of Java ``LlmStreamingStepSubnetTest``.

True incremental streaming: partials arrive while the LLM call is still in
flight, injected through the ``CHUNK`` env place. Java drives a bare
``BitmapNetExecutor`` with an ``executorRef``; here the net runs in a
:class:`PetriRunner` on the module's orchestrator loop (the stream hops to the
captured loop, and the runner owns the egress), with a :class:`HandleRef` in
place of the executor ref.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import libpetri as lp
import pytest
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import NetSpec, Place, one
from adk_libpetri.runner import HandleRef, PetriRunner
from adk_libpetri.subnet import llm_streaming_step as LS
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import assert_each_proven, requires_z3

T = LS.Transitions


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("streaming-step-orchestrator")
    yield loop
    loop.close()


# ============================================================
#  Fixture: drive a long-running runner with env-place injection
# ============================================================


@dataclass
class Fixture:
    events: list[Event]
    merged_responses: list[LlmResponse]
    final_marking: lp.MarkingView
    net_events: list[lp.NetEvent]


async def run_streaming(
    orch: OrchestratorLoop, llm: Any, author: str, *requests: LlmRequest, seed: bool = False
) -> Fixture:
    """Drive ``requests`` through one long-lived runner.

    By default the requests are injected on an ``LLM_REQUEST`` env place once
    the runner is up. Java seeds them in the initial marking instead, which is
    safe there because it sets the executor ref before ``run()``; ``seed=True``
    does the same here. It is not the default because a seeded request streams
    before the test can subscribe to the hot egress.
    """
    ref = HandleRef()
    config = LS.Config(author, ref)
    store = lp.InMemoryEventStore()
    builder = (
        PetriRunner.builder(LS.DEF, LS.action_bindings(llm, config))
        .environment_place(LS.Places.CHUNK)
        .event_store(store)
        .handle_ref(ref)
        .orchestrator(orch)
    )
    if seed:
        builder.initial_marking({C.LLM_REQUEST: list(requests)})
    else:
        builder.environment_place(C.LLM_REQUEST)
    runner = await builder.astart()
    # Subscribe before any request is in: the partials arrive on a hot stream.
    events: list[Event] = []
    sub = runner.adk_events().subscribe()

    async def collect() -> None:
        async for e in sub:
            events.append(e)

    collector = asyncio.ensure_future(collect())
    if not seed:
        assert runner.inject_many(C.LLM_REQUEST, requests)

    # Drain only once the net has really finished: drain refuses new injects,
    # so draining while a stream still injects chunks would lose them.
    await await_settled(runner, 5.0)
    runner.drain()
    final = await asyncio.wait_for(runner.wait_closed(), 5)
    await asyncio.wait_for(collector, 5)
    return Fixture(
        events,
        list(final.tokens(C.LLM_RESPONSE.name)),
        final,
        list(store.events()),
    )


async def await_settled(runner: PetriRunner, timeout: float) -> None:
    """No action in flight, no inject pending, no request or chunk waiting."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        snap = await runner.snapshot()
        marking = snap.marking
        if (
            snap.is_restore_point
            and not marking.get(C.LLM_REQUEST.name)
            and not marking.get(LS.Places.CHUNK.name)
        ):
            return
        await asyncio.sleep(0.002)
    raise AssertionError(f"streaming net did not settle within {timeout}s")


def simple_request(t: str) -> LlmRequest:
    return LlmRequest(
        model="fake", contents=[types.Content(role="user", parts=[types.Part(text=t)])]
    )


def text_of(x: Event | LlmResponse) -> str:
    assert x.content is not None and x.content.parts
    return "".join(p.text or "" for p in x.content.parts)


def streaming_llm(chunks: list[LlmResponse]) -> ScriptedLlm:
    return ScriptedLlm.of().stream(0, chunks)


# ============================================================
#  True incremental streaming
# ============================================================


async def test_three_chunks_each_injected_via_env_place_emit_three_partial_events(orch) -> None:
    llm = streaming_llm([text("Hello"), text(", "), text("world!")])

    fixture = await run_streaming(orch, llm, "streamer", simple_request("hi"))

    # Three partial Events, each from a separately injected chunk token.
    assert [text_of(e) for e in fixture.events] == ["Hello", ", ", "world!"]
    assert all(e.partial for e in fixture.events)
    assert {e.author for e in fixture.events} == {"streamer"}

    # One merged LlmResponse for downstream Router consumption.
    assert len(fixture.merged_responses) == 1
    merged = fixture.merged_responses[0]
    assert merged.turn_complete is True
    assert merged.content is not None and merged.content.parts is not None
    assert len(merged.content.parts) == 3


async def test_large_batch_of_chunks_each_emitted_in_order(orch) -> None:
    chunks = [text(f"chunk-{i}") for i in range(20)]

    fixture = await run_streaming(orch, streaming_llm(chunks), "burst", simple_request("burst me"))

    assert [text_of(e) for e in fixture.events] == [f"chunk-{i}" for i in range(20)]
    assert len(fixture.merged_responses) == 1


async def test_each_request_on_one_executor_streams_its_own_chunks(orch) -> None:
    # Two requests through the SAME long-lived executor. Java's executor never
    # starts LlmCallStream again while a stream is in flight, so its partials
    # come out in request order. libpetri-py may overlap the two async firings
    # (see the module docs of llm_streaming_step), so here each request's
    # partials are in order and the merged responses are one per request.
    llm = ScriptedLlm.of().stream(0, [text("a"), text("b")]).stream(1, [text("c"), text("d")])

    fixture = await run_streaming(orch, llm, "multi", simple_request("one"), simple_request("two"))

    emitted = [text_of(e) for e in fixture.events]
    assert sorted(emitted) == ["a", "b", "c", "d"]
    assert emitted.index("a") < emitted.index("b")
    assert emitted.index("c") < emitted.index("d")
    assert sorted(text_of(r) for r in fixture.merged_responses) == ["ab", "cd"]


# ============================================================
#  Structural verification
# ============================================================


def _verify_opts() -> dict[str, Any]:
    # CHUNK is an internal env place, not a port, and a stream may carry any
    # number of chunks: bounded(1) (one resident chunk, refilled forever) is the
    # model. Left out, nothing would reach CHUNK and the proof says nothing.
    return {
        "initial_marking": {C.LLM_REQUEST.name: 2},
        "environment_places": [LS.Places.CHUNK.name],
        "environment_mode": lp.bounded(1),
        "sink_places": [C.EVENT_OUT.name, C.LLM_RESPONSE.name],
    }


def _with_emit_gated_on_unseeded_place(spec: NetSpec) -> NetSpec:
    """``spec`` with ``EmitChunk`` also consuming from a place nothing produces
    into: the shape of an emit gated on a permit pool that has run dry."""
    gate: Place[None] = Place("unseededGate")
    transitions = tuple(
        dataclasses.replace(t, inputs=(*t.inputs, one(gate))) if t.name == T.EMIT_CHUNK else t
        for t in spec.transitions
    )
    return NetSpec(f"{spec.name}-starved", transitions, spec.extra_places, spec.ports)


@requires_z3
def test_streaming_step_never_strands_a_request_or_a_chunk() -> None:
    # Bind the real actions so the property is about the net that runs; they
    # are never invoked here, only the structure is encoded.
    config = LS.Config("verify", HandleRef())
    actions = LS.action_bindings(streaming_llm([]), config)
    net = LS.DEF.build(actions)

    assert_each_proven(net, {"deadlockFree": lp.deadlock_free()}, **_verify_opts())

    # Keep the proof honest: a starved emit must strand a chunk.
    starved = lp.verify(
        _with_emit_gated_on_unseeded_place(LS.DEF).build(actions),
        lp.deadlock_free(),
        **_verify_opts(),
    )
    assert starved.is_violated(), f"a starved emit must strand a chunk:\n{starved.report}"


# ============================================================
#  Edge cases
# ============================================================


async def test_empty_stream_fails_the_llm_call_transition(orch) -> None:
    fixture = await run_streaming(orch, streaming_llm([]), "e", simple_request("hi"))

    assert fixture.events == []
    failed = [e for e in fixture.net_events if e.type == "TransitionFailed"]
    assert len(failed) == 1
    assert failed[0].transition_name == T.LLM_CALL_STREAM


async def test_a_request_seeded_in_the_initial_marking_streams(orch) -> None:
    llm = streaming_llm([text("a"), text("b")])

    fixture = await run_streaming(orch, llm, "seeded", simple_request("hi"), seed=True)

    # A seeded request fires as soon as the runner starts, before the test can
    # subscribe to the hot egress, so the partials may already be gone (Java's
    # PublishProcessor behaves the same). What this test pins is that the
    # HandleRef is set before the seeded firing injects its chunks: every chunk
    # was emitted, none was rejected, and the merged response is in the marking.
    assert not [e for e in fixture.net_events if e.type == "TransitionFailed"]
    emitted = [
        e
        for e in fixture.net_events
        if e.type == "TransitionCompleted" and e.transition_name == T.EMIT_CHUNK
    ]
    assert len(emitted) == 3  # two partials plus the terminal
    assert [text_of(r) for r in fixture.merged_responses] == ["ab"]
    # Whatever did reach the subscriber is the tail of the stream, in order.
    assert [text_of(e) for e in fixture.events] in (["a", "b"], ["b"], [])


def test_interface_exposes_one_input_and_two_outputs() -> None:
    assert sorted(p.name for p in LS.DEF.ports) == ["eventOut", "llmRequest", "llmResponse"]


def test_def_declares_call_and_emit_transitions() -> None:
    assert sorted(LS.DEF.transition_names) == sorted([T.EMIT_CHUNK, T.LLM_CALL_STREAM])
