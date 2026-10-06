"""Fakes mimicking libpetri-py's ``NetEvent`` and a recording delegate store.

The decorator tests feed events to the stores directly, as the Java tests do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FakeNetEvent:
    type: str
    transition_name: str | None = None
    place_name: str | None = None
    timestamp: int = 0
    token: Any = None
    data: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return dict(self.data)


def token_added(place: str, token: Any, at: int = 0) -> FakeNetEvent:
    return FakeNetEvent("TokenAdded", place_name=place, token=token, timestamp=at)


def token_removed(place: str, token: Any, at: int = 0) -> FakeNetEvent:
    return FakeNetEvent("TokenRemoved", place_name=place, token=token, timestamp=at)


def started(name: str, at: int = 0) -> FakeNetEvent:
    return FakeNetEvent("TransitionStarted", transition_name=name, timestamp=at)


def completed(name: str, at: int = 0) -> FakeNetEvent:
    return FakeNetEvent("TransitionCompleted", transition_name=name, timestamp=at)


def failed(name: str, error: str, at: int = 0) -> FakeNetEvent:
    return FakeNetEvent(
        "TransitionFailed", transition_name=name, timestamp=at, data={"error": error}
    )


def timed_out(name: str, at: int = 0) -> FakeNetEvent:
    return FakeNetEvent("TransitionTimedOut", transition_name=name, timestamp=at)


def execution_started(at: int = 0) -> FakeNetEvent:
    return FakeNetEvent("ExecutionStarted", timestamp=at)


def execution_completed(at: int = 0) -> FakeNetEvent:
    return FakeNetEvent("ExecutionCompleted", timestamp=at)


class RecordingStore:
    """List-recording delegate (Java's ``EventStore.inMemory()``)."""

    captures_tokens = True

    def __init__(self) -> None:
        self.recorded: list[Any] = []

    def is_enabled(self) -> bool:
        return True

    def append(self, event: Any) -> None:
        self.recorded.append(event)

    def events(self, **filters: Any) -> list[Any]:
        return [e for e in self.recorded if all(getattr(e, k) == v for k, v in filters.items())]


class LoggingStore:
    """Pass-through that logs, standing in for Java's ``EventStore.logging(delegate)``."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.lines: list[str] = []

    def append(self, event: Any) -> None:
        self.lines.append(f"{event.type} {event.transition_name or event.place_name}")
        self.delegate.append(event)

    def events(self, **filters: Any) -> list[Any]:
        return self.delegate.events(**filters)
