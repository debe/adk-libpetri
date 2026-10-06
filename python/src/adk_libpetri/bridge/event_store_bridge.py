"""``EventStore`` decorator exposing the egress place's tokens as a hot stream.

Watches every ``TokenAdded`` on the configured output place and forwards the
token's value (needs token capture on the store chain) to a :class:`HotStream`
that subscribers read with ``async for``. ``ExecutionCompleted`` completes the
stream.

Failures are **not** terminal on the event stream. One bridge serves a whole
long-lived session; libpetri contains an action failure to its own
transition and keeps running (EXEC-031), so ending egress on the first
failure would undo that containment one layer up. Failures go to a separate
non-terminating :meth:`failure_signal` instead, and the consumer decides what
a failure means (``PetriAgent`` fails the turn in flight).

Purely additive: every event still reaches ``delegate``, so it chains with
other decorators (OTel spans, logging) without interference.
"""

from __future__ import annotations

from typing import Any

from google.adk.events.event import Event

from .._aio import HotStream
from .._spec import Place
from .transition_failure import TransitionFailure


class EventStoreToStreamBridge:
    captures_tokens = True

    def __init__(self, event_out_place: Place[Event], delegate: Any) -> None:
        if delegate is None:
            raise TypeError("delegate is required; pass libpetri.InMemoryEventStore() for none")
        self._place = event_out_place.name
        self._delegate = delegate
        self._events: HotStream[Event] = HotStream()
        self._failures: HotStream[TransitionFailure] = HotStream()

    def is_enabled(self) -> bool:
        return True

    def append(self, event: Any) -> None:
        t = event.type
        if t == "TokenAdded" and event.place_name == self._place:
            value = getattr(event, "token", None)
            if isinstance(value, Event):
                self._events.publish(value)
        elif t == "ExecutionCompleted":
            self._events.complete()
            self._failures.complete()
        elif t in ("TransitionFailed", "TransitionTimedOut"):
            failure = TransitionFailure.from_event(event)
            if failure is not None:
                self._failures.publish(failure)
        self._delegate.append(event)

    def events(self, **filters: Any) -> list[Any]:
        return self._delegate.events(**filters)

    @property
    def delegate(self) -> Any:
        return self._delegate

    def stream(self) -> HotStream[Event]:
        """Every ``Event`` token produced into the egress place, hot."""
        return self._events

    def failure_signal(self) -> HotStream[TransitionFailure]:
        """Transition failures, hot and non-terminating (completes with the net).

        A control signal for the caller's unit of work, not observability:
        the decorator chain sees every failure whether anyone subscribes.
        """
        return self._failures
