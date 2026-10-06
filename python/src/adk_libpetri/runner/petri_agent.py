"""``PetriAgent``: a thin ``BaseAgent`` that runs a libpetri net under stock ADK.

No ADK source changes (commitment 4). On each invocation it:

1. derives a :class:`SessionKey` from ``ctx.session``;
2. gets the session's :class:`PetriRunner` from the registry, creating it on
   first call (one net per user, alive across invocations);
3. subscribes to the runner's egress, stamping every event with the ADK
   invocation id, and to its failure signal, so a transition that fails
   mid-turn fails the turn instead of stalling it. For a runner that declares
   ``TURN_ABORT`` every failure is also signalled there, so the net lets go
   of the failed turn and serves the next one;
4. injects the user's message onto ``USER_IN``;
5. returns the turn: the first non-partial event, or, under
   ``StreamingMode.SSE``, every partial through the first non-partial one.

**Two roles in ADK 2.** As the ``Runner``'s root (``Runner(agent=...)``), the
net replaces ADK's graph ``Workflow`` outright. As a node inside a
``Workflow``, the node's input becomes the user content (a ``Content``, a
``str``, or anything ``input_mapper`` maps), and the turn's final event
becomes the node's output: its text, as ADK's ``LlmAgent`` reports, or what
``output_mapper`` returns; ``route_mapper`` picks the outgoing route.

**Session lifetime** follows the registry's mode; see
:mod:`~adk_libpetri.runner.session_registry`. Under ``finalizer_owned`` an
``owner_extractor`` is required and load-bearing: never return
``ctx.session`` from ``InMemorySessionService`` (it hands out copies).

``sub_agents`` is empty by design: the net *is* the topology.

``_run_live_impl`` (BIDI/Live). Without a :class:`LiveConfig` this is the
egress half of the bridge: it returns the runner's ``adk_events``. With one,
:func:`~adk_libpetri.runner.bidi_petri_agent.bridge` pumps the
``LiveRequestQueue`` into the provider connection and decodes server messages
into env-place injections.
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.run_config import StreamingMode  # pyright: ignore[reportPrivateImportUsage]
from google.adk.events.event import Event
from google.genai import types
from opentelemetry.trace import Span, Tracer
from pydantic import PrivateAttr

from .. import colours as C
from .._experimental import experimental
from ..bridge import OtelEventStore
from ..bridge.otel_event_store import context_with_span
from .petri_runner import PetriRunner
from .session_key import SessionKey
from .session_registry import NO_OWNER, SessionExecutorRegistry
from .turn import abort_turns_on_failure, run_turn

RunnerFactory = Callable[[SessionKey], "PetriRunner | Awaitable[PetriRunner]"]
OwnerExtractor = Callable[[InvocationContext], Any]

_node_content: contextvars.ContextVar[types.Content | None] = contextvars.ContextVar(
    "adk_libpetri_node_content", default=None
)


@experimental
@dataclass(frozen=True)
class LiveConfig:
    """The BIDI/live bridge: a connection per invocation, and the decoder that
    turns raw provider server messages into env-place injections."""

    connection_factory: Callable[[InvocationContext], Any]
    on_server_message: Callable[[Any, PetriRunner], None]


def _text_of(event: Event) -> str:
    content = event.content
    if content is None or not content.parts:
        return ""
    return "".join(p.text for p in content.parts if p.text and not p.thought)


def content_of(value: Any) -> types.Content | None:
    """Default node-input mapping: ``Content`` as is, ``str`` as a user turn."""
    if value is None or isinstance(value, types.Content):
        return value
    if isinstance(value, str):
        return types.Content(role="user", parts=[types.Part(text=value)])
    return types.Content(role="user", parts=[types.Part(text=str(value))])


class PetriAgent(BaseAgent):
    """Build with :meth:`builder`; see the module docs."""

    _registry: SessionExecutorRegistry = PrivateAttr()
    _factory: RunnerFactory = PrivateAttr()
    _owner_extractor: OwnerExtractor | None = PrivateAttr(default=None)
    _tracer: Tracer | None = PrivateAttr(default=None)
    _otel: OtelEventStore | None = PrivateAttr(default=None)
    _live: LiveConfig | None = PrivateAttr(default=None)
    _input_mapper: Callable[[Any], types.Content | None] = PrivateAttr(default=content_of)
    _output_mapper: Callable[[Event], Any] | None = PrivateAttr(default=None)
    _route_mapper: Callable[[Event], Any] | None = PrivateAttr(default=None)
    _spans: dict[SessionKey, Span] = PrivateAttr(default_factory=dict)

    @staticmethod
    def builder(
        name: str, registry: SessionExecutorRegistry, runner_factory: RunnerFactory
    ) -> PetriAgentBuilder:
        return PetriAgentBuilder(name, registry, runner_factory)

    @classmethod
    def of(
        cls,
        name: str,
        description: str,
        registry: SessionExecutorRegistry,
        runner_factory: RunnerFactory,
        owner_extractor: OwnerExtractor,
    ) -> PetriAgent:
        return (
            cls.builder(name, registry, runner_factory)
            .description(description)
            .owner_extractor(owner_extractor)
            .build()
        )

    # -- runner resolution -------------------------------------------------

    def _key(self, ctx: InvocationContext) -> SessionKey:
        return SessionKey.of(ctx.session)

    async def _runner_for(self, ctx: InvocationContext, key: SessionKey) -> PetriRunner:
        owner = NO_OWNER
        if self._owner_extractor is not None:
            owner = self._owner_extractor(ctx)
            if owner is None:
                raise ValueError(
                    "owner_extractor returned None; every invocation must yield an owner"
                )
        runner = await self._registry.aget_or_create(key, self._factory, owner)
        abort_turns_on_failure(runner)
        return runner

    # -- tracing -----------------------------------------------------------

    def _open_invocation_span(self, ctx: InvocationContext, key: SessionKey) -> None:
        """Opens ``petri.invocation.<agent>`` and binds it as the parent of
        transition spans. It stays open until the session's next invocation
        supersedes it, so late transition spans of this turn still attach."""
        if self._tracer is None or self._otel is None:
            return
        span = self._tracer.start_span(
            f"petri.invocation.{self.name}",
            attributes={
                "petri.agent.name": self.name,
                "adk.invocation.id": ctx.invocation_id,
                "adk.session.id": ctx.session.id,
            },
        )
        self._otel.set_invocation_context(context_with_span(span))
        previous = self._spans.pop(key, None)
        self._spans[key] = span
        if previous is not None:
            previous.end()

    def end_all_open_invocation_spans(self) -> None:
        """End every still-open invocation span (at shutdown, or before reading spans in tests)."""
        for span in self._spans.values():
            span.end()
        self._spans.clear()

    # -- turn-based --------------------------------------------------------

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        key = self._key(ctx)
        runner = await self._runner_for(ctx, key)
        content = _node_content.get() or ctx.user_content
        if content is None:
            return
        self._open_invocation_span(ctx, key)
        sse = ctx.run_config is not None and ctx.run_config.streaming_mode == StreamingMode.SSE
        async for event in run_turn(
            ctx.invocation_id,
            runner,
            lambda: runner.inject(C.USER_IN, content),
            sse=sse,
            abort_signal=getattr(ctx, "_abort_signal", None),
            finish=self._as_node_output,
        ):
            yield event

    def _as_node_output(self, event: Event) -> Event:
        """Mark the turn's terminal event as the node's output (ADK 2 node contract)."""
        if self._output_mapper is not None:
            event.output = self._output_mapper(event)
        elif event.content is not None and event.output is None:
            event.output = _text_of(event)
            event.node_info.message_as_output = True
        if self._route_mapper is not None:
            route = self._route_mapper(event)
            if route is not None:
                event.actions.route = route
        return event

    async def _run_impl(self, *, ctx: Any, node_input: Any) -> AsyncGenerator[Any, None]:
        """As a ``Workflow`` node: the node's input is this turn's user content."""
        ic = ctx.get_invocation_context()
        mapped = None if node_input is ic.user_content else self._input_mapper(node_input)
        token = _node_content.set(mapped)
        try:
            async for event in super()._run_impl(ctx=ctx, node_input=node_input):
                yield event
        finally:
            _node_content.reset(token)

    # -- live --------------------------------------------------------------

    async def _run_live_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        key = self._key(ctx)
        runner = await self._runner_for(ctx, key)
        if self._live is None:
            async for event in runner.adk_events().subscribe():
                yield event
            return
        from .bidi_petri_agent import bridge

        queue = ctx.live_request_queue
        if queue is None:
            raise RuntimeError("run_live requires a LiveRequestQueue")
        connection = self._live.connection_factory(ctx)
        async for event in bridge(queue, connection, runner, self._live.on_server_message):
            yield event


class PetriAgentBuilder:
    def __init__(
        self, name: str, registry: SessionExecutorRegistry, factory: RunnerFactory
    ) -> None:
        self._name = name
        self._registry = registry
        self._factory = factory
        self._description = ""
        self._owner: OwnerExtractor | None = None
        self._tracer: Tracer | None = None
        self._otel: OtelEventStore | None = None
        self._live: LiveConfig | None = None
        self._input_mapper: Callable[[Any], types.Content | None] = content_of
        self._output_mapper: Callable[[Event], Any] | None = None
        self._route_mapper: Callable[[Event], Any] | None = None

    def description(self, description: str) -> PetriAgentBuilder:
        self._description = description
        return self

    def owner_extractor(self, extractor: OwnerExtractor) -> PetriAgentBuilder:
        """Per-invocation owner: required for ``finalizer_owned``, optional otherwise."""
        self._owner = extractor
        return self

    def tracing(
        self, tracer: Tracer | None, otel_event_store: OtelEventStore | None
    ) -> PetriAgentBuilder:
        """Root-span observability; pass the same ``OtelEventStore`` the runner chains."""
        self._tracer = tracer
        self._otel = otel_event_store
        return self

    @experimental
    def live(self, config: LiveConfig) -> PetriAgentBuilder:
        self._live = config
        return self

    def input_mapper(self, fn: Callable[[Any], types.Content | None]) -> PetriAgentBuilder:
        """As a Workflow node: map the node input to the turn's user content."""
        self._input_mapper = fn
        return self

    def output_mapper(self, fn: Callable[[Event], Any]) -> PetriAgentBuilder:
        """As a Workflow node: map the terminal event to the node's output."""
        self._output_mapper = fn
        return self

    def route_mapper(self, fn: Callable[[Event], Any]) -> PetriAgentBuilder:
        """As a Workflow node: pick the outgoing route from the terminal event."""
        self._route_mapper = fn
        return self

    def build(self) -> PetriAgent:
        if self._owner is None and self._registry.is_finalizer_owned:
            raise ValueError(
                "A finalizer_owned() registry needs an owner_extractor: the owner is what "
                "tears each session's runner down. Supply one, or use strong_owned()."
            )
        if (self._tracer is None) != (self._otel is None):
            raise ValueError("tracer and otel_event_store must be both provided or both None")
        agent = PetriAgent(name=self._name, description=self._description)
        agent._registry = self._registry
        agent._factory = self._factory
        agent._owner_extractor = self._owner
        agent._tracer = self._tracer
        agent._otel = self._otel
        agent._live = self._live
        agent._input_mapper = self._input_mapper
        agent._output_mapper = self._output_mapper
        agent._route_mapper = self._route_mapper
        return agent


__all__ = ["LiveConfig", "PetriAgent", "PetriAgentBuilder", "content_of"]
