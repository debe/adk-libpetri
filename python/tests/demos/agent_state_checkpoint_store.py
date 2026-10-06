"""Exemplar: checkpoint a session's marking into ADK's own session history.

Port of Java ``AgentStateCheckpointStore``. The marking is stored as the
``agent_state`` of an ordinary ADK event (``EventActions.agent_state``, ADK's
resumability field), so the resume data lives where ADK keeps the rest of
the session and goes wherever its ``BaseSessionService`` persists to.

Thin user code, not library: what a token value turns into is the caller's
decision, made by the :class:`Codec`. ``agent_state`` is a JSON map, so a
durable session service needs JSON-friendly encodings.

Like any :class:`~adk_libpetri.runner.SessionCheckpointStore`, it is written
at session end and read before a runner starts, never during execution, so
ADK's session stays a write-only legacy bridge as far as a running net is
concerned (design commitment 2).

The history is append-only, so :meth:`remove` appends a tombstone: an event
whose ``agent_state`` maps :data:`MARKING_KEY` to :data:`REMOVED`.
:meth:`load` reads the newest marking event and finds none past a tombstone.
A session the service no longer has holds no checkpoint: :meth:`load` finds
none, and :meth:`save` and :meth:`remove` do nothing, since there is no
history left to write to.

Python difference: the checkpoint protocol is synchronous (the registry calls
it from its teardown thread) while ADK Python's session services are async.
Java blocks on the RxJava ``Single``; here each call runs the service's
coroutine on the caller-owned :class:`OrchestratorLoop` and blocks for it.
So the store must not be called from the orchestrator thread itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any, Protocol, TypeVar

from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.session import Session

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.runner import Checkpoint, SessionKey

T = TypeVar("T")

MARKING_KEY = "adk-libpetri.marking"
"""The ``agent_state`` key the marking is stored under."""

REMOVED = "removed"
"""The :data:`MARKING_KEY` value of a tombstone: the checkpoint was removed."""


class Codec(Protocol):
    """Turns one place's token values into JSON-friendly values and back."""

    def encode(self, place: str, value: Any) -> Any: ...

    def decode(self, place: str, encoded: Any) -> Any: ...


class AgentStateCheckpointStore:
    """A ``SessionCheckpointStore`` over a ``BaseSessionService``'s event history."""

    def __init__(
        self,
        sessions: BaseSessionService,
        author: str,
        codec: Codec,
        loop: OrchestratorLoop,
        timeout: timedelta = timedelta(seconds=10),
    ) -> None:
        if sessions is None or author is None or codec is None or loop is None:
            raise TypeError("sessions, author, codec and loop are required")
        self._sessions = sessions
        self._author = author
        self._codec = codec
        self._loop = loop
        self._timeout = timeout.total_seconds()

    # -- SessionCheckpointStore ---------------------------------------------

    def save(self, key: SessionKey, marking: Checkpoint) -> None:
        session = self._session(key)
        if session is None:
            return
        encoded = {
            place: [
                {
                    "value": self._codec.encode(place, token["value"]),
                    "created_at": token["created_at"],
                }
                for token in tokens
            ]
            for place, tokens in marking.items()
        }
        self._append(session, encoded)

    def remove(self, key: SessionKey) -> None:
        # Nothing to retract unless a marking is the latest word.
        if self.load(key) is not None:
            session = self._session(key)
            if session is not None:
                self._append(session, REMOVED)

    def load(self, key: SessionKey) -> Checkpoint | None:
        session = self._session(key)
        if session is None:
            return None
        for event in reversed(session.events):
            state = (event.actions.agent_state or {}).get(MARKING_KEY)
            if state is None:
                continue
            if state == REMOVED:
                return None
            return {
                place: [
                    {
                        "value": self._codec.decode(place, entry["value"]),
                        "created_at": entry["created_at"],
                    }
                    for entry in entries
                ]
                for place, entries in state.items()
            }
        return None

    # -- plumbing -------------------------------------------------------------

    def _append(self, session: Session, marking_state: Any) -> None:
        event = Event(
            id=str(uuid.uuid4()),
            invocation_id=f"checkpoint-{uuid.uuid4()}",
            author=self._author,
            actions=EventActions(agent_state={MARKING_KEY: marking_state}),
        )
        self._call(self._sessions.append_event(session, event))

    def _session(self, key: SessionKey) -> Session | None:
        """The session, or ``None`` when the service has none."""
        return self._call(
            self._sessions.get_session(
                app_name=key.app_name, user_id=key.user_id, session_id=key.session_id
            )
        )

    def _call(self, coro: Coroutine[Any, Any, T]) -> T:
        if self._loop.on_thread:
            coro.close()
            raise RuntimeError(
                "AgentStateCheckpointStore blocks on the orchestrator loop; "
                "call it from another thread"
            )
        return self._loop.call(coro, timeout=self._timeout)
