"""``MarkingTraces``: each session's firings and markings, recorded beside the net's store."""

from __future__ import annotations

from typing import Any

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.bridge import MarkingTraces
from adk_libpetri.net.report import load_net
from adk_libpetri.runner import SessionExecutorRegistry, SessionKey
from net._harness import session

from .conftest import HERO

SEEDED = HERO.parents[1] / "net" / "blueprints" / "bp_turns" / "seeded.yaml"


class Recording:
    """A store of the node's own, to see the traces hand every event on."""

    captures_tokens = True

    def __init__(self) -> None:
        self.types: list[str] = []

    def is_enabled(self) -> bool:
        return True

    def append(self, event: Any) -> None:
        self.types.append(event.type)

    def events(self, **filters: Any) -> list[Any]:
        return []


async def test_a_turn_is_traced_firing_by_firing() -> None:
    orchestrator = OrchestratorLoop("trace-test")
    registry = SessionExecutorRegistry.strong_owned()
    own = Recording()
    traces = MarkingTraces(own)
    try:
        node = load_net(str(HERO / "race.yaml")).serve_on(
            orchestrator, registry=registry, event_store=traces
        )
        s = await session(node, app_name="race")
        turn = await s.say("go")
        assert turn.error is None
        key = SessionKey("race", "u", s.session_id, node.session_scope())
        assert key in traces.sessions()
        t = traces.trace("race", "u", s.session_id)
        assert t is not None
        steps = t[node.session_scope()]["steps"]
        assert steps[0]["kind"] == "turn"
        assert steps[0]["marking"] == {"userIn": 1}
        fired = [st["transition"] for st in steps if st["kind"] == "fired"]
        assert fired[0] == "Race_Start"
        assert fired.count("Race_Commit") == 1
        after_commit = next(st for st in steps if st["transition"] == "Race_Commit")
        assert after_commit["marking"]["eventOut"] == 1
        # The node's own store saw every event too.
        assert "TransitionCompleted" in own.types
    finally:
        registry.close_all()
        orchestrator.close()


class _Ev:
    def __init__(self, type: str, place: str | None = None, transition: str | None = None) -> None:
        self.type = type
        self.place_name = place
        self.transition_name = transition


def test_a_trace_starts_from_the_seeds(monkeypatch: Any) -> None:
    """The executor reports no TokenAdded for a seed: a node hands its seeds to the trace."""
    monkeypatch.syspath_prepend(str(SEEDED.parents[1]))
    node = load_net(str(SEEDED))
    assert node._initial_counts() == {"warm": 1}
    traces = MarkingTraces()
    node._event_store = traces
    link = node._session_event_store(SessionKey("seeded", "u", "s"))
    link.append(_Ev("TokenAdded", place="userIn"))
    link.append(_Ev("TokenRemoved", place="warm"))
    link.append(_Ev("TokenAdded", place="ready"))
    link.append(_Ev("TransitionCompleted", transition="Seeded_Warm"))
    t = traces.trace("seeded", "u", "s")
    assert t is not None
    turn, warmed = t[""]["steps"]
    assert turn["marking"] == {"userIn": 1, "warm": 1}
    assert warmed["marking"] == {"ready": 1, "userIn": 1}


def test_traces_are_bounded() -> None:
    traces = MarkingTraces(max_steps=3, max_sessions=2)
    keys = [SessionKey("a", "u", str(i)) for i in range(3)]
    links = [traces.for_session(k) for k in keys]
    assert traces.sessions() == keys[1:]

    for _ in range(5):
        links[2].append(_Ev("TransitionCompleted", transition="T"))
    t = traces.trace("a", "u", "2")
    assert t is not None
    assert len(t[""]["steps"]) == 3
    assert t[""]["dropped"] == 2
