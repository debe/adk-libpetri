"""Port of Java ``AgentStateCheckpointStoreTest``, plus the store under a registry.

The store's methods block on the orchestrator loop, so the unit tests are
plain sync tests; the registry test is async and the registry calls the store
from its teardown thread, as in production.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.runner import PetriRunner, SessionExecutorRegistry, SessionKey
from adk_libpetri.subnet import bind
from demos.agent_state_checkpoint_store import MARKING_KEY, AgentStateCheckpointStore

AT = 1_790_000_000_000  # 2026-09-21 in epoch ms; libpetri's created_at unit


class IdentityCodec:
    """Strings stay strings; unit tokens are ``None`` either way."""

    def encode(self, place: str, value: Any) -> Any:
        return value

    def decode(self, place: str, encoded: Any) -> Any:
        return encoded


CODEC = IdentityCodec()


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("checkpoint-store-demo")
    yield loop
    loop.close()


def token(value: Any, at: int = AT) -> dict[str, Any]:
    return {"value": value, "created_at": at}


def new_session(orch: OrchestratorLoop) -> tuple[InMemorySessionService, SessionKey]:
    sessions = InMemorySessionService()
    session = orch.call(
        sessions.create_session(app_name="app", user_id="user", session_id="s1"), timeout=5
    )
    return sessions, SessionKey.of(session)


def events(orch: OrchestratorLoop, sessions: InMemorySessionService) -> list[Any]:
    session = orch.call(
        sessions.get_session(app_name="app", user_id="user", session_id="s1"), timeout=5
    )
    assert session is not None
    return list(session.events)


def test_the_marking_round_trips_through_an_adk_session_event(orch: OrchestratorLoop) -> None:
    sessions, key = new_session(orch)
    store = AgentStateCheckpointStore(sessions, "agent", CODEC, orch)

    assert store.load(key) is None

    marking = {
        "notes": [token("first"), token("second", AT + 1000)],
        "budget": [token(None), token(None)],
    }
    store.save(key, marking)

    assert store.load(key) == marking
    # It is an ordinary ADK event, carried in agent_state.
    stored = events(orch, sessions)[-1]
    assert stored.author == "agent"
    assert stored.actions.agent_state is not None
    assert MARKING_KEY in stored.actions.agent_state


def test_the_latest_checkpoint_wins(orch: OrchestratorLoop) -> None:
    sessions, key = new_session(orch)
    store = AgentStateCheckpointStore(sessions, "agent", CODEC, orch)

    store.save(key, {"notes": [token("old")]})
    store.save(key, {"notes": [token("new")]})

    loaded = store.load(key)
    assert loaded is not None
    assert loaded["notes"][0]["value"] == "new"


def test_remove_appends_a_tombstone_that_hides_earlier_markings(orch: OrchestratorLoop) -> None:
    sessions, key = new_session(orch)
    store = AgentStateCheckpointStore(sessions, "agent", CODEC, orch)

    store.remove(key)  # nothing saved yet: no event appended
    assert len(events(orch, sessions)) == 0

    store.save(key, {"notes": [token("old")]})
    store.remove(key)
    assert store.load(key) is None
    assert len(events(orch, sessions)) == 2
    store.remove(key)  # already removed: no second tombstone
    assert len(events(orch, sessions)) == 2

    store.save(key, {"notes": [token("new")]})
    loaded = store.load(key)
    assert loaded is not None
    assert loaded["notes"][0]["value"] == "new"


def test_a_session_the_service_does_not_have_holds_no_checkpoint(orch: OrchestratorLoop) -> None:
    sessions = InMemorySessionService()
    store = AgentStateCheckpointStore(sessions, "agent", CODEC, orch)
    key = SessionKey("app", "user", "gone")

    assert store.load(key) is None
    store.save(key, {"notes": [token("lost")]})
    store.remove(key)
    assert store.load(key) is None


# ============================================================
#  Python-only: the store behind a registry, saved at close, resumed after
# ============================================================

TICK: Place[None] = Place("tick")
COUNT: Place[int] = Place("count", int)
T_COUNT = "Counter_Count"
SPEC = NetSpec("counter", (TransitionSpec(T_COUNT, (one(TICK), one(COUNT)), out(COUNT)),))


def count(ctx: Ctx) -> None:
    ctx.input(TICK)
    ctx.output(COUNT, ctx.input(COUNT) + 1)


ACTIONS = bind(SPEC, {T_COUNT: count})


async def await_count(runner: PetriRunner, n: int, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = await runner.snapshot()
        if snap.is_restore_point and list(snap.marking.tokens(COUNT.name)) == [n]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"count never reached {n}")


async def test_a_registry_checkpoints_into_the_adk_session_and_resumes_from_it(
    orch: OrchestratorLoop,
) -> None:
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="app", user_id="user", session_id="s1")
    key = SessionKey.of(session)
    store = AgentStateCheckpointStore(sessions, "agent", CODEC, orch)
    registry = SessionExecutorRegistry.strong_owned(store)

    def factory(k: SessionKey) -> Any:
        return (
            PetriRunner.builder(SPEC, ACTIONS)
            .environment_place(TICK)
            .initial_marking({COUNT: [0]})
            .resume_from(store, k)
            .orchestrator(orch)
            .astart()
        )

    try:
        # The factory's resume_from reads the store synchronously, here on the
        # test's loop: anywhere but the orchestrator thread.
        runner = await registry.aget_or_create(key, factory)
        assert all(runner.signal(TICK) for _ in range(3))
        await await_count(runner, 3)

        assert await registry.aclose(key) is True
        stored = await sessions.get_session(app_name="app", user_id="user", session_id="s1")
        assert stored is not None
        last = stored.events[-1]
        assert last.actions.agent_state is not None
        saved = last.actions.agent_state[MARKING_KEY]
        assert [t["value"] for t in saved[COUNT.name]] == [3]

        # The next runner for the session starts from the ADK-held marking,
        # not from the initial 0.
        resumed = await registry.aget_or_create(key, factory)
        assert resumed is not runner
        await await_count(resumed, 3)
        assert resumed.signal(TICK)
        await await_count(resumed, 4)
    finally:
        await registry.aclose_all()
