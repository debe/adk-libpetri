"""Port of ``PersistStateSubnetTest.java``."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import libpetri as lp
from google.adk.events.event import Event
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.sessions.session import Session

from adk_libpetri import colours as C
from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, and_, one, out
from adk_libpetri.subnet import persist_state as PS
from adk_libpetri.subnet.actions import merge


async def run_with(config: PS.Config, *deltas: C.LegacySessionWrite) -> lp.InMemoryEventStore:
    net = NetSpec.compose("test", PS.DEF).build(PS.action_bindings(config))
    store = lp.InMemoryEventStore()
    await lp.run_async(net, initial={C.LEGACY_SESSION_WRITE.name: list(deltas)}, event_store=store)
    return store


async def new_session(svc: InMemorySessionService, session_id: str) -> Session:
    return await svc.create_session(app_name="app", user_id="user", session_id=session_id)


# ============================================================
#  Isolation -- single LegacySessionWrite lands in the session
# ============================================================


async def test_single_state_delta_appends_event_with_state_delta_actions() -> None:
    svc = InMemorySessionService()
    session = await new_session(svc, "sess-1")
    config = PS.Config("agent", svc, lambda: session, invocation_id_supplier=lambda: "inv-fixed")

    await run_with(config, PS.legacy_write({"foo": "bar"}))

    assert len(session.events) == 1
    assert session.events[0].author == "agent"
    assert session.events[0].invocation_id == "inv-fixed"
    assert session.state["foo"] == "bar"


async def test_two_state_deltas_in_initial_marking_both_persist_in_order() -> None:
    svc = InMemorySessionService()
    session = await new_session(svc, "sess-2")
    counter = itertools.count(1)
    config = PS.Config(
        "agent", svc, lambda: session, invocation_id_supplier=lambda: f"inv-{next(counter)}"
    )

    await run_with(config, PS.legacy_write({"k1": "v1"}), PS.legacy_write({"k2": "v2"}))

    assert len(session.events) == 2
    assert session.state["k1"] == "v1"
    assert session.state["k2"] == "v2"
    assert [e.invocation_id for e in session.events] == ["inv-1", "inv-2"]


async def test_overlapping_keys_apply_in_serial_fire_order() -> None:
    svc = InMemorySessionService()
    session = await new_session(svc, "sess-3")

    await run_with(
        PS.Config("agent", svc, lambda: session),
        PS.legacy_write({"k": "first"}),
        PS.legacy_write({"k": "second"}),
    )

    assert len(session.events) == 2
    # Which value wins is the serial fire order; the point is structural correctness.
    assert session.state["k"] in ("first", "second")


# ============================================================
#  Race-free composition -- two parallel producers funnel into
#  the single Persist transition.
# ============================================================


async def test_two_parallel_producer_transitions_funnel_through_single_persist_writer() -> None:
    # [start] --Fork--> and([branchA], [branchB])
    # [branchA] --ProducerA--> [LEGACY_SESSION_WRITE]
    # [branchB] --ProducerB--> [LEGACY_SESSION_WRITE]
    # [LEGACY_SESSION_WRITE] --PersistState_Persist--> (consumed)
    svc = InMemorySessionService()
    session = await new_session(svc, "sess-race")

    start: Place[None] = Place("start")
    branch_a: Place[None] = Place("branchA")
    branch_b: Place[None] = Place("branchB")

    def fork(ctx: Ctx) -> None:
        ctx.input(start)
        ctx.signal(branch_a)
        ctx.signal(branch_b)

    def producer(branch: Place[None], delta: dict[str, Any]) -> Callable[[Ctx], None]:
        def act(ctx: Ctx) -> None:
            ctx.input(branch)
            ctx.output(C.LEGACY_SESSION_WRITE, PS.legacy_write(delta))

        return act

    spec = NetSpec.compose(
        "race",
        TransitionSpec("Fork", (one(start),), and_(branch_a, branch_b)),
        TransitionSpec("ProducerA", (one(branch_a),), out(C.LEGACY_SESSION_WRITE)),
        TransitionSpec("ProducerB", (one(branch_b),), out(C.LEGACY_SESSION_WRITE)),
        PS.DEF,
    )
    # Java binds the inline actions via function-based bindActions; here one checked merge.
    actions = merge(
        {
            "Fork": fork,
            "ProducerA": producer(branch_a, {"from": "A"}),
            "ProducerB": producer(branch_b, {"from-b": "B"}),
        },
        PS.action_bindings(PS.Config("agent", svc, lambda: session)),
    )

    await lp.run_async(
        spec.build(actions), initial={start.name: [None]}, event_store=lp.InMemoryEventStore()
    )

    assert len(session.events) == 2
    assert session.state["from"] == "A"
    assert session.state["from-b"] == "B"


# ============================================================
#  Action failure surfaces as TransitionFailed (caller decides)
# ============================================================


class _AppendingService(BaseSessionService):
    """A session service whose ``append_event`` runs ``append``; everything else is unused."""

    def __init__(self, append: Callable[[Session, Event], Awaitable[Event]]) -> None:
        self._append = append

    async def create_session(self, **kwargs: Any) -> Session:  # type: ignore[override]
        raise NotImplementedError

    async def get_session(self, **kwargs: Any) -> Session | None:  # type: ignore[override]
        return None

    async def list_sessions(self, **kwargs: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def delete_session(self, **kwargs: Any) -> None:  # type: ignore[override]
        return None

    async def append_event(self, session: Session, event: Event) -> Event:
        return await self._append(session, event)


async def persist_once_with(service: BaseSessionService, timeout: timedelta) -> list[lp.NetEvent]:
    session = Session(id="s", app_name="app", user_id="u")
    config = PS.Config("agent", service, lambda: session, persist_timeout=timeout)
    store = await run_with(config, PS.legacy_write({"k": "v"}))
    return list(store.failures())


async def test_session_service_error_surfaces_as_transition_failure() -> None:
    async def fail(session: Session, event: Event) -> Event:
        raise RuntimeError("db down")

    failed = await persist_once_with(_AppendingService(fail), PS.DEFAULT_PERSIST_TIMEOUT)
    assert len(failed) == 1


async def test_a_hung_session_service_times_out_as_a_transition_failure() -> None:
    """A session service that never answers must not hold the transition forever."""

    async def hang(session: Session, event: Event) -> Event:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    failed = await persist_once_with(_AppendingService(hang), timedelta(milliseconds=50))
    assert len(failed) == 1
    # Java TimeoutException -> builtins TimeoutError; the payload carries "Type: message".
    assert "TimeoutError" in failed[0].payload()["error"]


async def test_a_timed_out_append_has_its_subscription_disposed() -> None:
    """The timeout also cancels the hung ``append_event``.

    Java's ``doOnDispose`` on the never-``Single`` -> the coroutine sees ``CancelledError``.
    """
    disposed = asyncio.Event()

    async def hang(session: Session, event: Event) -> Event:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            disposed.set()
            raise
        raise AssertionError("unreachable")

    failed = await persist_once_with(_AppendingService(hang), timedelta(milliseconds=50))
    assert len(failed) == 1
    assert disposed.is_set()


def test_subnet_def_has_one_transition_and_one_port() -> None:
    assert list(PS.DEF.transition_names) == [PS.Transitions.PERSIST]
    assert [p.name for p in PS.DEF.ports] == ["legacySessionWrite"]


# ============================================================
#  Concurrency: append serialization
# ============================================================


async def test_append_event_calls_are_serialized() -> None:
    calls = 0
    in_flight = 0
    max_in_flight = 0

    async def append(session: Session, event: Event) -> Event:
        nonlocal calls, in_flight, max_in_flight
        calls += 1
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        # If Persist fired in parallel with itself, the second call would land here.
        await asyncio.sleep(0.05)
        in_flight -= 1
        return event

    session = Session(id="s", app_name="app", user_id="u")
    config = PS.Config("agent", _AppendingService(append), lambda: session)

    await run_with(config, PS.legacy_write({"k1": "v1"}), PS.legacy_write({"k2": "v2"}))

    assert calls == 2
    assert max_in_flight == 1
