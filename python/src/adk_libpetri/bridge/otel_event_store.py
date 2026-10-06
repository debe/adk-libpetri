"""``EventStore`` decorator emitting one OpenTelemetry span per transition fire.

Span start comes from the matching ``TransitionStarted`` (libpetri-py's
``TransitionCompleted`` carries no duration), paired FIFO per transition
name; two concurrent fires of one transition that complete out of order may
swap starts. ``TransitionFailed`` and ``TransitionTimedOut`` give ERROR spans
with an ``exception`` span event, as OpenTelemetry models failures.

``append`` runs on a libpetri thread, so ``context.get_current()`` there does
not see the caller's span. :meth:`bind_invocation_context` threads a parent
across that boundary: per invocation (``PetriAgent`` with tracing) or for a
whole session (a BIDI root span). Bindings are sequential by design.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer

from .transition_failure import split_error

TRANSITION_ATTRIBUTE = "libpetri.transition"
SUBNET_ATTRIBUTE = "libpetri.subnet"

_MS = 1_000_000


class OtelEventStore:
    def __init__(
        self,
        tracer: Tracer,
        delegate: Any,
        subnet_of: Callable[[str], str | None] | None = None,
    ) -> None:
        """``subnet_of`` names a transition's subnet (``NetSpec.subnet_of``)."""
        if delegate is None:
            raise TypeError("delegate is required")
        self._tracer = tracer
        self._delegate = delegate
        self._subnet_of = subnet_of
        self._parent: otel_context.Context = otel_context.Context()
        self._starts: dict[str, deque[int]] = defaultdict(deque)
        self._lock = threading.Lock()

    @property
    def captures_tokens(self) -> bool:
        return bool(getattr(self._delegate, "captures_tokens", False))

    def is_enabled(self) -> bool:
        return True

    @contextmanager
    def bind_invocation_context(self, ctx: otel_context.Context) -> Iterator[None]:
        """Parent every transition span on ``ctx`` until the block exits (LIFO restore)."""
        with self._lock:
            previous, self._parent = self._parent, ctx
        try:
            yield
        finally:
            with self._lock:
                self._parent = previous

    def set_invocation_context(self, ctx: otel_context.Context) -> otel_context.Context:
        """Bind ``ctx`` until the next call (``PetriAgent``'s sticky per-turn binding).

        Returns the previous binding.
        """
        with self._lock:
            previous, self._parent = self._parent, ctx
        return previous

    def append(self, event: Any) -> None:
        t = event.type
        name = event.transition_name
        if t == "TransitionStarted" and name:
            with self._lock:
                self._starts[name].append(event.timestamp)
        elif t == "TransitionCompleted" and name:
            self._emit(name, self._start_of(name, event.timestamp), event.timestamp, None, None)
        elif t == "TransitionFailed" and name:
            exc_type, msg = split_error(str(event.payload().get("error", "")))
            self._emit(name, self._start_of(name, event.timestamp), event.timestamp, msg, exc_type)
        elif t == "TransitionTimedOut" and name:
            self._emit(
                name,
                self._start_of(name, event.timestamp),
                event.timestamp,
                "deadline exceeded",
                "TransitionTimedOut",
            )
        self._delegate.append(event)

    def events(self, **filters: Any) -> list[Any]:
        return self._delegate.events(**filters)

    def _start_of(self, name: str, end: int) -> int:
        with self._lock:
            q = self._starts.get(name)
            return q.popleft() if q else end

    def _emit(
        self, name: str, start_ms: int, end_ms: int, error: str | None, exc_type: str | None
    ) -> None:
        with self._lock:
            parent = self._parent
        span = self._tracer.start_span(name, context=parent, start_time=start_ms * _MS)
        try:
            span.set_attribute(TRANSITION_ATTRIBUTE, name)
            if self._subnet_of is not None:
                subnet = self._subnet_of(name)
                if subnet:
                    span.set_attribute(SUBNET_ATTRIBUTE, subnet)
            if error is None:
                span.set_status(Status(StatusCode.OK))
            else:
                span.set_status(Status(StatusCode.ERROR, error))
                span.add_event(
                    "exception",
                    {"exception.type": exc_type or "Error", "exception.message": error},
                    timestamp=end_ms * _MS,
                )
        finally:
            span.end(end_time=end_ms * _MS)


def context_with_span(span: trace.Span) -> otel_context.Context:
    return trace.set_span_in_context(span, otel_context.Context())
