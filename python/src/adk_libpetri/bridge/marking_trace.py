"""``EventStore`` decorator recording each session's firings and markings (``@experimental``).

A :class:`MarkingTraces` records, per session, which transition fired, in what
order, and the marking (token count per place) after each firing; give one to
``PetriAgentLoader(traces=...)`` to trace every net ``adk-libpetri web`` loads. A net node asks
the store for its session's link with :meth:`MarkingTraces.for_session`
(``NetNodeBase`` does, for any store that has that method), so every session
gets a trace of its own though the node holds one store.

Observability only (design commitment 5): the net never reads it back. It
keeps counts, not token values, and a bounded number of steps and sessions.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .._experimental import experimental

if TYPE_CHECKING:
    from ..runner.session_key import SessionKey


@dataclass(frozen=True)
class TraceStep:
    """One firing, or the start of a turn (``kind == "turn"``)."""

    seq: int
    kind: str
    """``fired``, ``failed``, ``timed_out`` (a ``timeout`` output branch fired) or ``turn``."""
    transition: str | None
    marking: dict[str, int]
    """Token count per place after the step (places with no tokens left out)."""
    at_ms: int


@dataclass
class _Trace:
    marking: dict[str, int] = field(default_factory=dict)
    steps: deque[TraceStep] = field(default_factory=deque)
    seq: int = 0
    dropped: int = 0
    timed_out: str | None = None
    drifted: bool = False


class _SessionLink:
    """The store one session's runner appends to."""

    def __init__(self, owner: MarkingTraces, key: SessionKey, delegate: Any) -> None:
        self._owner = owner
        self._key = key
        self._delegate = delegate

    @property
    def captures_tokens(self) -> bool:
        return bool(getattr(self._delegate, "captures_tokens", False))

    def is_enabled(self) -> bool:
        return True

    def append(self, event: Any) -> None:
        self._owner._record(self._key, event)
        if self._delegate is not None:
            self._delegate.append(event)

    def events(self, **filters: Any) -> list[Any]:
        return self._delegate.events(**filters) if self._delegate is not None else []


_log = logging.getLogger(__name__)

_RECORDED = frozenset(
    {"TokenAdded", "TokenRemoved", "TransitionCompleted", "TransitionFailed", "ActionTimedOut"}
)


@experimental
class MarkingTraces:
    """Per-session firing and marking traces, wrapping an optional ``delegate`` store."""

    def __init__(
        self,
        delegate: Any = None,
        *,
        turn_place: str = "userIn",
        max_steps: int = 2000,
        max_sessions: int = 256,
    ) -> None:
        self._delegate = delegate
        self._turn_place = turn_place
        self._max_steps = max_steps
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._traces: OrderedDict[SessionKey, _Trace] = OrderedDict()

    # -- the EventStore side ----------------------------------------------------

    def for_session(
        self, key: SessionKey, initial: Mapping[str, int] | None = None, *, delegate: Any = None
    ) -> _SessionLink:
        """The link ``key``'s runner appends to; ``initial`` is its seed count per place
        (the executor reports no ``TokenAdded`` for a seed). ``delegate`` overrides
        this store's own for that session."""
        if delegate is None:
            per_session = getattr(self._delegate, "for_session", None)
            delegate = per_session(key, initial) if callable(per_session) else self._delegate
        with self._lock:
            self._traces.pop(key, None)  # a new runner for the session: a new trace
            self._traces[key] = _Trace(marking={p: n for p, n in (initial or {}).items() if n})
            while len(self._traces) > self._max_sessions:
                self._traces.popitem(last=False)
        return _SessionLink(self, key, delegate)

    def _record(self, key: SessionKey, event: Any) -> None:
        t = event.type
        if t not in _RECORDED:
            return
        with self._lock:
            trace = self._traces.get(key)
            if trace is None:
                return
            self._traces.move_to_end(key)  # evict the session recorded longest ago
            m = trace.marking
            if t != "TokenAdded" and trace.timed_out is not None:
                # A timeout branch's tokens follow its ActionTimedOut; no
                # TransitionCompleted follows them.
                self._step(trace, "timed_out", trace.timed_out)
                trace.timed_out = None
            if t == "TokenAdded":
                place = str(event.place_name)
                m[place] = m.get(place, 0) + 1
                if place == self._turn_place:
                    self._step(trace, "turn", None)
            elif t == "TokenRemoved":
                place = str(event.place_name)
                n = m.get(place, 0) - 1
                if n < 0 and not trace.drifted:
                    trace.drifted = True
                    _log.warning(
                        "marking trace of %s removed a token %s did not hold: its seed "
                        "counts are wrong; later markings of this trace may be off",
                        key,
                        place,
                    )
                if n > 0:
                    m[place] = n
                else:
                    m.pop(place, None)
            elif t == "ActionTimedOut":
                trace.timed_out = str(event.transition_name)
            else:
                kind = "fired" if t == "TransitionCompleted" else "failed"
                self._step(trace, kind, str(event.transition_name))

    def _step(self, trace: _Trace, kind: str, transition: str | None) -> None:
        trace.seq += 1
        trace.steps.append(
            TraceStep(
                trace.seq,
                kind,
                transition,
                dict(sorted(trace.marking.items())),
                int(time.time() * 1000),
            )
        )
        if len(trace.steps) > self._max_steps:
            trace.steps.popleft()
            trace.dropped += 1

    # -- reading ----------------------------------------------------------------

    def sessions(self) -> list[SessionKey]:
        with self._lock:
            return list(self._traces)

    def trace(self, app_name: str, user_id: str, session_id: str) -> dict[str, Any] | None:
        """The session's trace (every scope's, merged by scope), or ``None``."""
        with self._lock:
            found = {
                k.scope: (list(t.steps), dict(sorted(t.marking.items())), t.dropped)
                for k, t in self._traces.items()
                if (k.app_name, k.user_id, k.session_id) == (app_name, user_id, session_id)
            }
        if not found:
            return None
        return {
            scope: {
                "steps": [asdict(s) for s in steps],
                "marking": marking,
                "dropped": dropped,
            }
            for scope, (steps, marking, dropped) in found.items()
        }


__all__ = ["MarkingTraces", "TraceStep"]
