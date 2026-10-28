"""Event stores a net node hands to each session's runner (``event_store=``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, TypeAlias, runtime_checkable

from libpetri import EventStoreProtocol

from .._experimental import experimental

if TYPE_CHECKING:
    from ..runner.session_key import SessionKey


@experimental
@runtime_checkable
class PerSessionEventStore(Protocol):
    """A store that gives each session's runner a link of its own, as
    :class:`~adk_libpetri.bridge.MarkingTraces` does.

    ``initial`` is the new net's seed count per place: the executor reports no
    ``TokenAdded`` for a seed.
    """

    def for_session(
        self, key: SessionKey, initial: Mapping[str, int] | None = None, /
    ) -> EventStoreProtocol: ...


NodeEventStore: TypeAlias = EventStoreProtocol | PerSessionEventStore
"""What a ``PetriNet`` or ``PetriWorkflow`` records into: one libpetri event
store for every session, or a :class:`PerSessionEventStore`."""


def for_session(
    store: NodeEventStore | None, key: SessionKey, initial: Mapping[str, int] | None = None
) -> EventStoreProtocol | None:
    """``store``'s link for ``key`` if it keeps one per session, else ``store``."""
    if isinstance(store, PerSessionEventStore):
        return store.for_session(key, initial)
    return store


__all__ = ["NodeEventStore", "PerSessionEventStore", "for_session"]
