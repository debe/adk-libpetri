"""A transition failure carried on ``PetriRunner.failure_signal()`` with its identity intact.

The fields stay fields, so a consumer branches on :attr:`transition_name`
(and :attr:`instance_prefix` for a composed instance) instead of parsing a
message. ``ActionTimedOut`` is deliberately not a failure: the net routed the
tokens down the declared timeout branch, a modelled outcome.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any


class Kind(enum.Enum):
    ACTION_THREW = "action_threw"
    """The action raised. Consumed tokens are lost (libpetri EXEC-031)."""
    DEADLINE_EXCEEDED = "deadline_exceeded"
    """The transition exceeded the deadline of its timing."""


class TransitionFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        transition_name: str,
        kind: Kind,
        occurred_at: datetime,
        exception_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.transition_name = transition_name
        self.kind = kind
        self.occurred_at = occurred_at
        self.exception_type = exception_type
        self.error_message = error_message

    @property
    def instance_prefix(self) -> str | None:
        """The composed instance prefix (``a/b`` of ``a/b/T``), or ``None`` (MOD-041)."""
        head, sep, _ = self.transition_name.rpartition("/")
        return head if sep else None

    @classmethod
    def from_event(cls, event: Any) -> TransitionFailure | None:
        """Build from a libpetri ``NetEvent``; ``None`` if it is not a failure."""
        name = event.transition_name or ""
        at = datetime.fromtimestamp(event.timestamp / 1000, tz=UTC)
        if event.type == "TransitionFailed":
            error = str(event.payload().get("error", ""))
            exc_type, msg = split_error(error)
            return cls(
                f"Transition {name} failed: {msg} ({exc_type})",
                transition_name=name,
                kind=Kind.ACTION_THREW,
                occurred_at=at,
                exception_type=exc_type,
                error_message=msg,
            )
        if event.type == "TransitionTimedOut":
            return cls(
                f"Transition {name} exceeded its deadline",
                transition_name=name,
                kind=Kind.DEADLINE_EXCEEDED,
                occurred_at=at,
            )
        return None


def split_error(error: str) -> tuple[str, str]:
    """libpetri-py reports ``"<Type>: <message>"``; split it (no type: ``"Error"``)."""
    head, sep, tail = error.partition(": ")
    if sep and head and " " not in head and not head.startswith("["):
        return head, tail
    return "Error", error
