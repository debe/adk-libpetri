"""Marking traces beyond plain firings: timeouts, failures, resets, eviction, workflow seeds."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from adk_libpetri.bridge import MarkingTraces
from adk_libpetri.runner import SessionKey
from adk_libpetri.workflow import PetriWorkflow

from .conftest import LOOP_CONFIG

KEY = SessionKey("a", "u", "s")


class _Ev:
    def __init__(self, type: str, place: str | None = None, transition: str | None = None) -> None:
        self.type = type
        self.place_name = place
        self.transition_name = transition


def _steps(traces: MarkingTraces, session: str = "s") -> list[dict[str, Any]]:
    t = traces.trace("a", "u", session)
    assert t is not None
    return t[""]["steps"]


def test_a_timeout_branch_is_a_step_of_its_own() -> None:
    """A timeout branch's tokens follow ``ActionTimedOut``; no ``TransitionCompleted`` follows."""
    traces = MarkingTraces()
    link = traces.for_session(KEY, {"a": 1, "b": 1})
    for e in (
        _Ev("TokenRemoved", place="a"),
        _Ev("ActionTimedOut", transition="To_Slow"),
        _Ev("TokenAdded", place="eventOut"),
        _Ev("TokenRemoved", place="b"),
        _Ev("TokenAdded", place="other"),
        _Ev("TransitionCompleted", transition="To_Medium"),
    ):
        link.append(e)
    timed_out, medium = _steps(traces)
    assert (timed_out["kind"], timed_out["transition"]) == ("timed_out", "To_Slow")
    assert timed_out["marking"] == {"b": 1, "eventOut": 1}
    assert (medium["kind"], medium["transition"]) == ("fired", "To_Medium")
    assert medium["marking"] == {"eventOut": 1, "other": 1}


def test_a_failure_takes_its_inputs_and_adds_nothing() -> None:
    traces = MarkingTraces()
    link = traces.for_session(KEY, {"a": 2})
    link.append(_Ev("TokenRemoved", place="a"))
    link.append(_Ev("TransitionFailed", transition="T_Boom"))
    [failed] = _steps(traces)
    assert (failed["kind"], failed["marking"]) == ("failed", {"a": 1})


def test_a_reset_removes_every_token() -> None:
    """A reset arc emits one ``TokenRemoved`` per token; read and inhibitor arcs emit nothing."""
    traces = MarkingTraces()
    link = traces.for_session(KEY, {"a": 1, "won": 3})
    for _ in range(3):
        link.append(_Ev("TokenRemoved", place="won"))
    link.append(_Ev("TransitionCompleted", transition="T_Reset"))
    [reset] = _steps(traces)
    assert reset["marking"] == {"a": 1}


def test_wrong_seeds_are_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    traces = MarkingTraces()
    link = traces.for_session(KEY, {})
    with caplog.at_level(logging.WARNING, logger="adk_libpetri.bridge.marking_trace"):
        link.append(_Ev("TokenRemoved", place="permit"))
        link.append(_Ev("TokenRemoved", place="permit"))
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "permit" in caplog.records[0].getMessage()


def test_the_session_recorded_longest_ago_is_evicted() -> None:
    traces = MarkingTraces(max_sessions=2)
    old, busy = SessionKey("a", "u", "old"), SessionKey("a", "u", "busy")
    links = {k: traces.for_session(k) for k in (busy, old)}
    links[busy].append(_Ev("TransitionCompleted", transition="T"))
    traces.for_session(SessionKey("a", "u", "new"))
    assert traces.sessions() == [busy, SessionKey("a", "u", "new")]


def test_a_compiled_workflow_is_seeded_with_its_turn_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner seeds ``turnPermit``; the trace and the drawing must start from it too."""
    monkeypatch.syspath_prepend(str(LOOP_CONFIG.parent))
    wf = PetriWorkflow.from_config(str(LOOP_CONFIG / "petri_root_agent.yaml"))
    assert wf._initial_counts()["turnPermit"] == 1
    assert {p.name: p.seed for p in wf.graph.places}["turnPermit"] == 1
