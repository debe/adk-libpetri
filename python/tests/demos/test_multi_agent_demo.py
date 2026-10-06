"""Port of Java ``MultiAgentDemoTest``.

Composes the stock subnets into a customer-service routing pattern and
drives it through the **stock ADK ``Runner``** (``InMemoryRunner``), with
structural verification before execution.

What the demo shows that libpetri makes possible:

1. **Multi-subnet composition by typed-place fusion.** A planner
   ``LlmAgent`` subnet is composed alongside a ``TransferRouter`` subnet
   configured with the compile-time-known set of specialist names. The
   structural ``xor`` over per-target places makes a hallucinated agent name a
   typed error event, not a runtime lookup failure.
2. **Observability via event-store decoration.** An ``OtelEventStore`` wraps
   the executor's event store and emits one span per transition fire, each a
   child of ``PetriAgent``'s ``petri.invocation.<name>`` span.
3. **Structural verification at build time.** ``single_legacy_session_writer``
   (vacuous here: no ``PersistState`` is composed) and
   ``transfer_demux_has_unknown_fallback`` hold on the composed net.
4. **Stock ADK ``Runner`` compatibility.** The net is wrapped in a
   ``PetriAgent`` and driven by ``InMemoryRunner`` with no ADK changes.
5. **Long-lived per-session executor** from ``SessionExecutorRegistry``.

Python differences:

* Java checks the invariants on the composed ``PetriNet``; here they walk the
  composed :class:`NetSpec`, from which the runnable net is built.
* The OTel half of Java's first test is its own test here.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import libpetri as lp
import pytest
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import NetSpec
from adk_libpetri.bridge import OtelEventStore
from adk_libpetri.runner import PetriAgent, PetriRunner, SessionExecutorRegistry
from adk_libpetri.subnet import bind_composed, llm_agent, llm_step, merge, router, transfer_router
from adk_libpetri.verify import (
    event_out_bounded,
    single_legacy_session_writer,
    transfer_demux_has_unknown_fallback,
)
from support.fake_llm import ScriptedLlm, text, transfer
from support.smt_proofs import assert_each_proven, requires_z3

KNOWN_SPECIALISTS = ("billing", "tech_support")
ANSWER = "Here's the answer to your question."
_counter = itertools.count()


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("multi-agent-demo")
    yield loop
    loop.close()


def compose(name: str) -> NetSpec:
    """Planner LlmAgent + TransferRouter over the known specialists, fused by place."""
    return NetSpec.compose(name, llm_agent.DEF, transfer_router.def_(KNOWN_SPECIALISTS))


def planner_actions(llm: Any, *, system_instruction: str | None = None) -> list[dict[str, Any]]:
    config = llm_agent.Config(
        name="planner",
        model="fake-model",
        system_instruction=system_instruction,
        reask_budget=2,
    )
    router_config = transfer_router.Config("planner", lambda: f"inv-{next(_counter)}")
    return [
        llm_agent.action_bindings(llm, config),
        transfer_router.action_bindings(KNOWN_SPECIALISTS, router_config),
    ]


def user_message(t: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=t)])


def event_text(e: Event) -> str:
    if e.content is None or not e.content.parts:
        return ""
    return "".join(p.text or "" for p in e.content.parts)


async def run_once(agent: PetriAgent, message: str) -> list[Event]:
    runner = InMemoryRunner(agent=agent, app_name="multi_agent_app")
    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id="user-1", session_id="sess-1"
    )
    return [
        e
        async for e in runner.run_async(
            user_id=session.user_id, session_id=session.id, new_message=user_message(message)
        )
    ]


# ============================================================
#  The planner's text answer through the stock ADK Runner
# ============================================================


async def test_planner_composed_with_transfer_router_passes_invariants_and_emits_text_event(
    orch: OrchestratorLoop,
) -> None:
    # 1. Compose: the router knows exactly two specialists at build time. The
    #    planner answers with text here, so the transfer topology is present
    #    but does not fire; it is exercised by the hallucination test below.
    spec = compose("multi-agent-app")

    # 2. Structural verification, BEFORE anything runs.
    assert single_legacy_session_writer(spec) == []
    assert transfer_demux_has_unknown_fallback(spec) == []

    # 3. One checked binding for every subnet (bind_composed rejects a missing,
    #    unknown or doubly bound transition).
    llm = ScriptedLlm.of(text(ANSWER))
    actions = planner_actions(llm, system_instruction="Route to the right specialist.")
    bind_composed(spec, *actions)
    merged = merge(*actions)

    # 4. Stock ADK Runner through the PetriAgent adapter.
    registry = SessionExecutorRegistry.strong_owned()
    agent = (
        PetriAgent.builder(
            "multi_agent",
            registry,
            lambda key: (
                PetriRunner.builder(spec, merged)
                .environment_place(C.USER_IN)
                .orchestrator(orch)
                .astart()
            ),
        )
        .description("Planner that routes to specialists")
        .build()
    )
    try:
        events = await run_once(agent, "Help me with billing")
    finally:
        await registry.aclose_all()

    # The agent's actual reply, not merely "some event": InMemoryRunner records
    # the user message regardless of what the net does.
    planner_texts = [t for e in events if e.author == "planner" and (t := event_text(e).strip())]
    assert planner_texts, f"no planner event among {[(e.author, event_text(e)) for e in events]}"
    assert planner_texts[-1] == ANSWER


async def test_planner_transition_spans_are_children_of_the_invocation_span(
    orch: OrchestratorLoop,
) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    spec = compose("multi-agent-app")
    llm = ScriptedLlm.of(text(ANSWER))
    actions = planner_actions(llm, system_instruction="Route to the right specialist.")
    merged = merge(*actions)

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("multi-agent-demo")
    chain = OtelEventStore(tracer, lp.InMemoryEventStore(), subnet_of=spec.subnet_of)

    registry = SessionExecutorRegistry.strong_owned()
    agent = (
        PetriAgent.builder(
            "multi_agent",
            registry,
            lambda key: (
                PetriRunner.builder(spec, merged)
                .environment_place(C.USER_IN)
                .event_store(chain)
                .orchestrator(orch)
                .astart()
            ),
        )
        .description("Planner that routes to specialists")
        .tracing(tracer, chain)
        .build()
    )
    try:
        events = await run_once(agent, "Help me with billing")
    finally:
        await registry.aclose_all()
    assert any(event_text(e) == ANSWER for e in events)

    # The invocation span stays open past the turn so late transition spans
    # still attach; end it before reading.
    agent.end_all_open_invocation_spans()
    provider.force_flush()
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    # The text path: BuildPrompt, LlmCall, Route. ReAsk and the router's Demux
    # do not fire without tool calls or a transfer.
    assert llm_agent.Transitions.BUILD_PROMPT in names
    assert llm_step.Transitions.LLM_CALL in names
    assert router.Transitions.ROUTE in names

    invocation = [s for s in spans if s.name == "petri.invocation.multi_agent"]
    assert len(invocation) == 1
    root = invocation[0].context
    assert root is not None
    root_id = root.span_id
    transition_spans = [s for s in spans if s.name != "petri.invocation.multi_agent"]
    assert transition_spans
    for s in transition_spans:
        assert s.parent is not None and s.parent.span_id == root_id, s.name
    provider.shutdown()


# ============================================================
#  A hallucinated transfer target becomes a typed error event
# ============================================================


async def test_hallucinated_agent_name_surfaces_as_typed_error_event_not_npe(
    orch: OrchestratorLoop,
) -> None:
    """The planner transfers to an unknown name. ADK's ``find_agent`` returns
    ``None`` and its scheduler raises an untyped ``ValueError`` (see
    ``test_transfer_unknown_target_adk_foil``); here ``TransferRouter_Demux``
    routes the name to ``UNKNOWN_TARGET`` and ``EmitUnknownError`` puts a typed
    error ``Event`` on ``EVENT_OUT``, which ends the turn."""
    spec = compose("hallucination-app")
    llm = ScriptedLlm.of(transfer("hallucinated_typo"))
    actions = planner_actions(llm)
    bind_composed(spec, *actions)
    merged = merge(*actions)

    registry = SessionExecutorRegistry.strong_owned()
    agent = (
        PetriAgent.builder(
            "halluc_agent",
            registry,
            lambda key: (
                PetriRunner.builder(spec, merged)
                .environment_place(C.USER_IN)
                .orchestrator(orch)
                .astart()
            ),
        )
        .description("Demonstrates structural elimination of hallucinated-transfer failures")
        .build()
    )
    try:
        events = await run_once(agent, "help")
    finally:
        await registry.aclose_all()

    # Not just "the runner returned events" (the user message alone does that):
    # the hallucinated name surfaced as the typed error event.
    errors = [event_text(e) for e in events if "hallucinated_typo" in event_text(e)]
    assert errors == ["Cannot transfer to unknown agent: 'hallucinated_typo'"]


# ============================================================
#  Z3: the composed net is deadlock-free, one egress event per turn
# ============================================================


@requires_z3
def test_multi_agent_net_is_smt_proven_deadlock_free() -> None:
    # Bind the actions the demo runs with, so the proof is about the net that
    # executes. They are never invoked; only structure is encoded.
    spec = compose("dlf-check")
    net = bind_composed(
        spec,
        *planner_actions(
            ScriptedLlm.of(text("verification stub")),
            system_instruction="Route to the right specialist.",
        ),
    )
    sinks = [
        # Terminal places: tokens here are a finished turn, not a deadlock.
        C.EVENT_OUT,
        C.LEGACY_SESSION_WRITE,
        transfer_router.UNKNOWN_TARGET,
        *(transfer_router.target_place(n) for n in KNOWN_SPECIALISTS),
        # The planner at rest holds its permit and nothing else; a stalled
        # turn holds no permit, so excusing it hides nothing.
        C.TURN_PERMIT,
    ]
    # One verify() per property, each in the strong form (is_proven): libpetri
    # downgrades a verdict it cannot back to unknown.
    assert_each_proven(
        net,
        {
            "deadlock_free": lp.deadlock_free(),
            # One user turn yields at most one egress event: the router's
            # answer, the reask-exhausted fallback or the unknown-target
            # error, never two.
            "one egress event per turn: event_out_bounded(1)": event_out_bounded(1),
        },
        # The planner's turn permit is what PetriRunner seeds.
        initial_marking={C.USER_IN.name: 1, C.TURN_PERMIT.name: 1},
        sink_places=[p.name for p in sinks],
    )
