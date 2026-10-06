"""Event-store decorators: the observability chain (commitment 5) and the egress bridge."""

from .event_store_bridge import EventStoreToStreamBridge
from .otel_event_store import SUBNET_ATTRIBUTE, TRANSITION_ATTRIBUTE, OtelEventStore
from .transition_failure import Kind, TransitionFailure

__all__ = [
    "SUBNET_ATTRIBUTE",
    "TRANSITION_ATTRIBUTE",
    "EventStoreToStreamBridge",
    "Kind",
    "OtelEventStore",
    "TransitionFailure",
]
