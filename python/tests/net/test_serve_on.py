"""``serve_on``: where a net node's session runners start, live and record (no runs)."""

from __future__ import annotations

from typing import Any

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.bridge import PerSessionEventStore
from adk_libpetri.net import PetriNet
from adk_libpetri.runner import SessionExecutorRegistry, SessionKey

from .conftest import BLUEPRINTS

TURNS = BLUEPRINTS / "bp_turns"


class _Store:
    def append(self, event: Any) -> None:
        pass


class _PerSession:
    def __init__(self) -> None:
        self.keys: list[SessionKey] = []

    def for_session(self, key: SessionKey, initial: Any = None) -> _Store:
        self.keys.append(key)
        return _Store()


def test_from_config_serves_through_serve_on(orchestrator: OrchestratorLoop) -> None:
    registry = SessionExecutorRegistry.strong_owned()
    store = _Store()
    node = PetriNet.from_config(
        str(TURNS / "inner.yaml"), orchestrator=orchestrator, registry=registry, event_store=store
    )
    assert node.orchestrator is orchestrator
    assert node.registry is registry
    assert node.event_store is store


def test_serve_on_without_a_registry_keeps_the_nodes_own(orchestrator: OrchestratorLoop) -> None:
    node = PetriNet.from_config(str(TURNS / "inner.yaml"))
    own = node.registry
    assert node.serve_on(orchestrator) is node
    assert node.registry is own
    caller = SessionExecutorRegistry.strong_owned()
    node.serve_on(orchestrator, caller)
    assert node.registry is caller
    # Back to a private registry: a new one, closed with the node.
    node.serve_on(orchestrator)
    assert node.registry is not caller


def test_set_event_store_reaches_each_new_session(orchestrator: OrchestratorLoop) -> None:
    node = PetriNet.from_config(str(TURNS / "inner.yaml")).serve_on(orchestrator)
    per_session = _PerSession()
    assert isinstance(per_session, PerSessionEventStore)
    node.set_event_store(per_session)
    assert node.event_store is per_session
    key = SessionKey("app", "u", "s")
    assert isinstance(node._session_event_store(key), _Store)
    assert per_session.keys == [key]
