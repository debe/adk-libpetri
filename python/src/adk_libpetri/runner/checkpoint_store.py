"""Where a registry saves a session's marking on teardown (``@experimental``).

A checkpoint is the marking a drained run came to rest in, in libpetri's
snapshot form (``{place: [{"value": v, "created_at": ms}, ...]}``), minus the
places a runner excludes (``EVENT_OUT`` always). ``PetriRunner.builder(...)
.resume_from(store, key)`` starts the next runner of that session from it.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable

from .._experimental import experimental
from .session_key import SessionKey

Checkpoint = dict[str, list[dict[str, Any]]]


@runtime_checkable
class SessionCheckpointStore(Protocol):
    def save(self, key: SessionKey, marking: Checkpoint) -> None: ...
    def load(self, key: SessionKey) -> Checkpoint | None: ...
    def remove(self, key: SessionKey) -> None: ...


@experimental
class InMemoryCheckpointStore:
    """A process-local store; a real deployment persists checkpoints elsewhere."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[SessionKey, Checkpoint] = {}

    def save(self, key: SessionKey, marking: Checkpoint) -> None:
        with self._lock:
            self._data[key] = {p: list(ts) for p, ts in marking.items()}

    def load(self, key: SessionKey) -> Checkpoint | None:
        with self._lock:
            m = self._data.get(key)
            return None if m is None else {p: list(ts) for p, ts in m.items()}

    def remove(self, key: SessionKey) -> None:
        with self._lock:
            self._data.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
