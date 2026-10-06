"""Port of Java's ``EventStoreToFlowableBridgeTest`` (RxJava ``Flowable`` -> ``HotStream``)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from google.adk.events.event import Event

from adk_libpetri.bridge import EventStoreToStreamBridge, Kind, TransitionFailure
from adk_libpetri.colours import EVENT_OUT

from ._fakes import (
    RecordingStore,
    execution_completed,
    failed,
    started,
    timed_out,
    token_added,
    token_removed,
)

_NOTHING: Any = object()


def _event(inv: str = "inv-1", id: str | None = None) -> Event:
    return Event(invocation_id=inv, author="agent", **({"id": id} if id else {}))


async def _next(sub: AsyncIterator[Any], timeout: float = 0.05) -> Any:
    """The next item, ``StopAsyncIteration`` on complete, or ``_NOTHING`` if none arrives."""
    try:
        return await asyncio.wait_for(anext(sub), timeout)
    except TimeoutError:
        return _NOTHING


async def _values(sub: AsyncIterator[Any]) -> list[Any]:
    """Drain what has been delivered so far (Java's ``TestSubscriber.values()``)."""
    out: list[Any] = []
    while (item := await _next(sub)) is not _NOTHING:
        out.append(item)
    return out


async def _assert_completed(sub: AsyncIterator[Any]) -> None:
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(sub), 1)


def _bridge(delegate: Any = None) -> EventStoreToStreamBridge:
    return EventStoreToStreamBridge(EVENT_OUT, RecordingStore() if delegate is None else delegate)


async def test_forwards_token_added_on_event_out_place_as_stream_event() -> None:
    bridge = _bridge()
    sub = bridge.stream().subscribe()
    adk_event = _event()

    bridge.append(token_added(EVENT_OUT.name, adk_event))

    assert await _values(sub) == [adk_event]


async def test_ignores_token_added_on_other_places() -> None:
    bridge = _bridge()
    sub = bridge.stream().subscribe()

    bridge.append(token_added("someOtherPlace", "not-an-event"))

    assert await _values(sub) == []
    assert not bridge.stream().terminated


async def test_ignores_token_added_on_event_out_when_value_is_not_an_event() -> None:
    bridge = _bridge()
    sub = bridge.stream().subscribe()

    # A misconfigured transition producing a wrong-typed token is ignored, not a crash.
    bridge.append(token_added(EVENT_OUT.name, "string-not-event"))

    assert await _values(sub) == []
    assert not bridge.stream().terminated


async def test_token_added_without_a_captured_token_is_ignored() -> None:
    # Python-specific: without token capture on the chain, ``token`` is absent.
    bridge = _bridge()
    sub = bridge.stream().subscribe()

    bridge.append(token_added(EVENT_OUT.name, None))

    assert await _values(sub) == []


async def test_execution_completed_completes_the_stream() -> None:
    bridge = _bridge()
    sub = bridge.stream().subscribe()

    bridge.append(execution_completed())

    await _assert_completed(sub)


async def test_execution_completed_also_completes_the_failure_signal() -> None:
    # Python-specific: the failure signal completes with the net (see failure_signal docs).
    bridge = _bridge()
    failures = bridge.failure_signal().subscribe()

    bridge.append(execution_completed())

    await _assert_completed(failures)


async def test_transition_failed_is_published_on_the_failure_signal() -> None:
    bridge = _bridge()
    failures = bridge.failure_signal().subscribe()

    # libpetri-py reports the error as "<Type>: <message>" (Java has separate fields).
    bridge.append(failed("T_llm_call", "OSError: model returned 500", at=1_700_000_000_000))

    values = await _values(failures)
    assert len(values) == 1
    # Identity intact, not flattened into a message a consumer has to regex.
    f = values[0]
    assert isinstance(f, TransitionFailure)
    assert f.transition_name == "T_llm_call"
    assert f.kind is Kind.ACTION_THREW
    assert f.exception_type == "OSError"
    assert f.error_message == "model returned 500"
    assert f.occurred_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert str(f) == "Transition T_llm_call failed: model returned 500 (OSError)"
    # Non-terminal: the signal stays open for the next failure.
    assert not bridge.failure_signal().terminated


async def test_a_deadline_timeout_is_also_published_on_the_failure_signal() -> None:
    """A blown deadline is a failure the caller must hear about.

    Otherwise a turn whose only terminal event was going to come from the
    timed-out transition waits forever (PersistState ships a deadline).
    """
    bridge = _bridge()
    failures = bridge.failure_signal().subscribe()

    bridge.append(timed_out("PersistState_Persist"))

    values = await _values(failures)
    assert len(values) == 1
    f = values[0]
    assert f.transition_name == "PersistState_Persist"
    assert f.kind is Kind.DEADLINE_EXCEEDED
    # Nothing was thrown, so there is no exception type to report.
    # (libpetri-py's TransitionTimedOut carries no deadline/actual durations; Java's does.)
    assert f.exception_type is None
    assert f.error_message is None


async def test_a_transition_failure_does_not_kill_the_event_stream() -> None:
    """The regression that motivated the failure signal.

    Erroring the per-session stream on a failure would end egress for every
    later turn, though libpetri contained the failure and the net still runs.
    """
    bridge = _bridge()
    sub = bridge.stream().subscribe()

    bridge.append(failed("T_llm_call", "OSError: model returned 500"))

    assert await _values(sub) == []
    assert not bridge.stream().terminated

    # ... and an Event produced after the failure still reaches subscribers.
    later = _event("inv-2")
    bridge.append(token_added(EVENT_OUT.name, later))
    assert await _values(sub) == [later]


async def test_a_late_subscriber_after_a_failure_still_receives_events() -> None:
    bridge = _bridge()

    # Failure happens with nobody attached, as between ADK turns.
    bridge.append(failed("T_llm_call", "OSError: model returned 500"))

    late = bridge.stream().subscribe()
    ev = _event("inv-3")
    bridge.append(token_added(EVENT_OUT.name, ev))

    assert await _values(late) == [ev]


async def test_multiple_events_arrive_in_order() -> None:
    bridge = _bridge()
    sub = bridge.stream().subscribe()

    for i in ("e1", "e2", "e3"):
        bridge.append(token_added(EVENT_OUT.name, _event(id=i)))

    assert [e.id for e in await _values(sub)] == ["e1", "e2", "e3"]


def test_delegate_receives_every_event() -> None:
    captured = RecordingStore()
    bridge = _bridge(captured)

    n1 = token_added(EVENT_OUT.name, _event("inv"))
    n2 = token_added("other", "x")
    n3 = execution_completed()
    n4 = failed("Bad", "RuntimeError: err")

    for n in (n1, n2, n3, n4):
        bridge.append(n)

    assert captured.recorded == [n1, n2, n3, n4]


def test_events_delegates_to_the_downstream_store() -> None:
    captured = RecordingStore()
    bridge = _bridge(captured)
    bridge.append(started("T_x"))
    bridge.append(token_added("p", "x"))

    assert bridge.events() == captured.recorded
    assert bridge.events(type="TransitionStarted") == [captured.recorded[0]]
    assert bridge.delegate is captured


def test_a_delegate_is_required() -> None:
    # Java's constructor null-checks it; the bridge never silently drops events.
    with pytest.raises(TypeError):
        EventStoreToStreamBridge(EVENT_OUT, None)


def test_bridge_requests_token_capture() -> None:
    assert EventStoreToStreamBridge.captures_tokens is True
    assert _bridge().is_enabled()


async def test_stream_supports_multiple_subscribers() -> None:
    bridge = _bridge()
    s1 = bridge.stream().subscribe()
    s2 = bridge.stream().subscribe()

    adk_event = _event()
    bridge.append(token_added(EVENT_OUT.name, adk_event))

    assert await _values(s1) == [adk_event]
    assert await _values(s2) == [adk_event]


async def test_events_emitted_before_subscription_are_not_replayed() -> None:
    bridge = _bridge()
    bridge.append(token_added(EVENT_OUT.name, _event("inv", "early")))

    sub = bridge.stream().subscribe()
    # Hot, like Java's PublishProcessor: late subscribers miss what already passed.
    assert await _values(sub) == []

    live = _event("inv", "live")
    bridge.append(token_added(EVENT_OUT.name, live))
    assert await _values(sub) == [live]


async def test_unrelated_net_events_are_silently_passed_through() -> None:
    captured = RecordingStore()
    bridge = _bridge(captured)
    sub = bridge.stream().subscribe()

    bridge.append(started("T_x"))
    bridge.append(token_removed("p", "x"))

    assert await _values(sub) == []
    assert not bridge.stream().terminated
    assert len(captured.recorded) == 2
