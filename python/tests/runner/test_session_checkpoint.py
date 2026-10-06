"""Port of Java ``SessionCheckpointTest``.

Checkpoints are libpetri's snapshot form, ``{place: [{"value": v,
"created_at": ms}, ...]}``. The ``Counter_Work`` action blocks on a
``threading.Event`` (Java: a ``CountDownLatch``) through
``libpetri.action_to_thread``, since actions have no running asyncio loop.
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import timedelta
from typing import Any

import libpetri as lp
import pytest
from google.adk.events.event import Event
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.runner import (
    Builder,
    Checkpoint,
    InMemoryCheckpointStore,
    PetriRunner,
    SessionCheckpointStore,
    SessionExecutorRegistry,
    SessionKey,
)
from adk_libpetri.subnet import llm_agent as LA
from support.fake_llm import ScriptedLlm, text

KEY = SessionKey("app", "user", "session")

COUNTER: Place[int] = Place("counter", int)
BUMP: Place[str] = Place("bump", str)
WORK: Place[str] = Place("work", str)
DONE: Place[str] = Place("done", str)


class Owner:
    """A lifetime owner for the finalizer-owned case."""


@pytest.fixture(scope="module")
def orchestrator() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop()
    yield loop
    loop.close()


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(4) as p:
        yield p


async def test_a_session_closed_at_rest_resumes_with_its_marking(
    orchestrator: OrchestratorLoop,
) -> None:
    # A session closed at rest is saved, and a new runner for the key resumes
    # from it: the agent's turn permit is back, as it was, and the resumed
    # session answers its next turn. Nothing else of the finished turn rests
    # to be saved, and EVENT_OUT (egress) is never checkpointed.
    checkpoints = InMemoryCheckpointStore()
    factory = agent_factory(orchestrator, checkpoints)
    registry = SessionExecutorRegistry.strong_owned(checkpoints)
    try:
        runner = await registry.aget_or_create(KEY, factory)
        egress = runner.adk_events().subscribe()
        assert runner.inject(C.USER_IN, user_message("remember me"))
        await take_one(egress)
        before = dict((await runner.snapshot()).marking.snapshot())
        assert C.EVENT_OUT.name in before

        assert await registry.aclose(KEY) is True
        saved = checkpoints.load(KEY)
        assert saved is not None
        before.pop(C.EVENT_OUT.name, None)
        assert saved == before
        assert len(saved[C.TURN_PERMIT.name]) == 1
        assert LA.CONVERSATION.name not in saved
        assert LA.REASK_BUDGET.name not in saved

        resumed = await registry.aget_or_create(KEY, factory)
        # Restored, not seeded again on top: still exactly one permit.
        assert (await resumed.snapshot()).marking.count(C.TURN_PERMIT.name) == 1
        resumed_egress = resumed.adk_events().subscribe()
        assert resumed.inject(C.USER_IN, user_message("still there?"))
        event = await take_one(resumed_egress)
        assert event.content is not None and event.content.parts
        assert event.content.parts[0].text == "ok"
    finally:
        await registry.aclose_all()


async def test_delivered_events_do_not_accumulate_across_resumes(
    orchestrator: OrchestratorLoop,
) -> None:
    # Delivered events are never consumed in the net, so a checkpoint that
    # kept them would hand every resume all earlier sessions' events again.
    checkpoints = InMemoryCheckpointStore()
    factory = agent_factory(orchestrator, checkpoints)
    registry = SessionExecutorRegistry.strong_owned(checkpoints)
    try:
        for i in range(3):
            runner = await registry.aget_or_create(KEY, factory)
            egress = runner.adk_events().subscribe()
            assert runner.inject(C.USER_IN, user_message(f"turn {i}"))
            await take_one(egress)
            assert await registry.aclose(KEY) is True

            saved = checkpoints.load(KEY)
            assert saved is not None
            assert C.EVENT_OUT.name not in saved
    finally:
        await registry.aclose_all()


def test_a_finalizer_owned_session_is_saved_when_its_owner_is_collected(
    orchestrator: OrchestratorLoop,
) -> None:
    # The finalizer path checkpoints too: once a finalizer_owned(store)
    # runner's owner is collected, its final marking is saved off-thread, and
    # the next runner for the key resumes from it.
    checkpoints = InMemoryCheckpointStore()
    net = counter_net(released())

    def factory(key: SessionKey) -> PetriRunner:
        return counter_runner(orchestrator, net, checkpoints, key).start()

    with SessionExecutorRegistry.finalizer_owned(checkpoints) as registry:
        bump_once(registry, factory)

        deadline = time.monotonic() + 10
        while checkpoints.load(KEY) is None:
            if time.monotonic() > deadline:
                raise AssertionError("owner was not collected, or its runner not saved, in 10s")
            gc.collect()
            time.sleep(0.05)
        assert counter_of(must_load(checkpoints)) == 1

        owner = Owner()
        resumed = registry.get_or_create(KEY, factory, owner)
        assert counter_of(snapshot(orchestrator, resumed)) == 1
        del owner  # its finalizer tears the resumed runner down


def bump_once(
    registry: SessionExecutorRegistry, factory: Callable[[SessionKey], PetriRunner]
) -> None:
    """Own frame, so the owner is unreachable once it returns.

    Held in a local until the inject: CPython collects an owner passed only
    as a call argument the moment the call returns.
    """
    owner = Owner()
    runner = registry.get_or_create(KEY, factory, owner)
    assert runner.inject(BUMP, "x")


def test_a_runner_names_further_places_to_leave_out(orchestrator: OrchestratorLoop) -> None:
    # Further egress places can be left out alongside EVENT_OUT.
    checkpoints = InMemoryCheckpointStore()
    net = counter_net(released())
    with SessionExecutorRegistry.strong_owned(checkpoints) as registry:
        runner = registry.get_or_create(
            KEY,
            lambda key: (
                counter_runner(orchestrator, net, checkpoints, key)
                .exclude_from_checkpoint(DONE)
                .start()
            ),
        )
        assert runner.inject(BUMP, "x")
        assert runner.inject(WORK, "job")
        registry.close(KEY)

        saved = must_load(checkpoints)
        assert DONE.name not in saved
        assert counter_of(saved) == 1


def test_a_checkpoint_takes_precedence_over_the_initial_marking(
    orchestrator: OrchestratorLoop,
) -> None:
    # One factory serves the first start and every resume: the initial
    # marking seeds a session without a checkpoint, and a checkpoint found by
    # resume_from takes its place rather than clash with it.
    checkpoints = InMemoryCheckpointStore()
    net = counter_net(released())

    def factory(key: SessionKey) -> PetriRunner:
        return counter_runner(orchestrator, net, checkpoints, key).start()

    with SessionExecutorRegistry.strong_owned(checkpoints) as registry:
        first = registry.get_or_create(KEY, factory)
        assert first.inject(BUMP, "x")
        registry.close(KEY)
        assert counter_of(must_load(checkpoints)) == 1

        resumed = registry.get_or_create(KEY, factory)
        assert counter_of(snapshot(orchestrator, resumed)) == 1
        assert resumed.inject(BUMP, "y")
        registry.close(KEY)
        assert counter_of(must_load(checkpoints)) == 2


def test_an_explicit_restore_with_an_initial_marking_is_rejected(
    orchestrator: OrchestratorLoop,
) -> None:
    # An explicit restore next to an explicit seed is still a contradiction.
    spec, actions = counter_net(released())
    builder = (
        PetriRunner.builder(spec, actions)
        .initial_marking({COUNTER: [0]})
        .restore(checkpoint(5))
        .orchestrator(orchestrator)
    )
    with pytest.raises(ValueError, match="exclusive"):
        builder.start()


def test_an_action_in_flight_completes_and_is_checkpointed(
    orchestrator: OrchestratorLoop, pool: ThreadPoolExecutor
) -> None:
    # Teardown drains before it saves: the runner refuses injects from the
    # moment close begins, the action in flight finishes, and its output is
    # in the checkpoint.
    checkpoints = InMemoryCheckpointStore()
    release = threading.Event()
    net = counter_net(release)
    with SessionExecutorRegistry.strong_owned(checkpoints) as registry:
        runner = registry.get_or_create(
            KEY, lambda key: counter_runner(orchestrator, net, checkpoints, key).start()
        )
        assert runner.inject(BUMP, "x")
        assert runner.inject(WORK, "job")

        closing = pool.submit(registry.close, KEY)
        try:
            await_draining(orchestrator, runner)
            assert registry.get(KEY) is None
            assert runner.inject(BUMP, "late") is False
            assert not closing.done()
        finally:
            release.set()
        assert closing.result(5) is True

        saved = must_load(checkpoints)
        assert saved[DONE.name][0]["value"] == "job"
        assert counter_of(saved) == 1


def test_a_session_that_does_not_drain_in_time_loses_its_stale_checkpoint(
    orchestrator: OrchestratorLoop, pool: ThreadPoolExecutor
) -> None:
    # A runner that does not drain in time leaves no checkpoint behind: an
    # older one would be restored as if it were this session's last word.
    checkpoints = InMemoryCheckpointStore()
    release = threading.Event()
    net = counter_net(release)
    checkpoints.save(KEY, checkpoint(41))
    registry = SessionExecutorRegistry.strong_owned(
        checkpoints, checkpoint_timeout=timedelta(milliseconds=100)
    )
    with registry:
        runner = registry.get_or_create(
            KEY, lambda key: counter_runner(orchestrator, net, checkpoints, key).start()
        )
        assert runner.inject(WORK, "job")

        closing = pool.submit(registry.close, KEY)
        try:
            # The checkpoint goes once the timeout passes, while the action still runs.
            deadline = time.monotonic() + 5
            while (checkpoints.load(KEY) is not None or registry.size() > 0) and (
                time.monotonic() < deadline
            ):
                time.sleep(0.005)
            assert checkpoints.load(KEY) is None
            assert registry.size() == 0
            assert not closing.done()
        finally:
            release.set()
        assert closing.result(5) is True
        assert checkpoints.load(KEY) is None


def test_get_or_create_during_close_waits_and_resumes_from_the_final_marking(
    orchestrator: OrchestratorLoop, pool: ThreadPoolExecutor
) -> None:
    # The race the closing slot exists for. While the old runner drains, a
    # get_or_create for its key waits instead of resuming from the checkpoint
    # the old runner has yet to write; once the save lands, the new runner
    # resumes from exactly that marking. Never two serving runners for a key.
    checkpoints = InMemoryCheckpointStore()
    release = threading.Event()
    net = counter_net(release)
    # An older checkpoint, so resuming early would be visibly wrong.
    checkpoints.save(KEY, checkpoint(7))
    with SessionExecutorRegistry.strong_owned(checkpoints) as registry:
        old = registry.get_or_create(
            KEY, lambda key: counter_runner(orchestrator, net, checkpoints, key).start()
        )
        assert counter_of(snapshot(orchestrator, old)) == 7
        assert old.inject(BUMP, "x")
        assert old.inject(WORK, "job")

        closing = pool.submit(registry.close, KEY)
        factory_calls = 0
        old_terminated_at_create: list[bool] = []

        def replace(key: SessionKey) -> PetriRunner:
            nonlocal factory_calls
            factory_calls += 1
            old_terminated_at_create.append(old.await_termination(0))
            return counter_runner(orchestrator, net, checkpoints, key).start()

        replacing: Future[PetriRunner] | None = None
        try:
            await_draining(orchestrator, old)
            assert registry.get(KEY) is None

            replacing = pool.submit(registry.get_or_create, KEY, replace)
            with pytest.raises(FutureTimeout):
                replacing.result(0.2)
            assert factory_calls == 0
        finally:
            release.set()
        assert closing.result(5) is True
        fresh = replacing.result(5)

        assert fresh is not old
        assert factory_calls == 1
        assert old_terminated_at_create == [True]
        marking = snapshot(orchestrator, fresh)
        assert counter_of(marking) == 8
        assert marking[DONE.name][0]["value"] == "job"
        assert registry.get(KEY) is fresh


class StoreBroke(BaseException):
    """Java's ``Error``: not an ``Exception``, so nothing on the way swallows it."""


class BrokenStore:
    """Saves always fail; loads and removes go to ``inner``."""

    def __init__(self, inner: InMemoryCheckpointStore) -> None:
        self.inner = inner

    def save(self, key: SessionKey, marking: Checkpoint) -> None:
        raise StoreBroke

    def load(self, key: SessionKey) -> Checkpoint | None:
        return self.inner.load(key)

    def remove(self, key: SessionKey) -> None:
        self.inner.remove(key)


def test_an_error_from_the_store_still_tears_the_runner_down(
    orchestrator: OrchestratorLoop,
) -> None:
    # An error from the store reaches the caller, but only after the runner
    # is torn down and the key freed: a broken store never orphans a
    # session's runner. The checkpoint it could not replace is gone too.
    inner = InMemoryCheckpointStore()
    inner.save(KEY, checkpoint(41))
    broken = BrokenStore(inner)
    assert isinstance(broken, SessionCheckpointStore)
    net = counter_net(released())

    def factory(key: SessionKey) -> PetriRunner:
        return counter_runner(orchestrator, net, broken, key).start()

    registry = SessionExecutorRegistry.strong_owned(broken)
    try:
        runner = registry.get_or_create(KEY, factory)
        assert runner.inject(BUMP, "x")

        with pytest.raises(StoreBroke):
            registry.close(KEY)

        assert runner.await_termination(0) is True
        assert registry.size() == 0
        assert inner.load(KEY) is None
        nxt = registry.get_or_create(KEY, factory)
        assert counter_of(snapshot(orchestrator, nxt)) == 0
    finally:
        registry.discard(KEY)  # the registry's own close would hit the broken save again


def test_remove_forgets_a_checkpoint() -> None:
    checkpoints = InMemoryCheckpointStore()
    checkpoints.save(KEY, checkpoint(1))
    checkpoints.remove(KEY)
    assert checkpoints.load(KEY) is None
    checkpoints.remove(KEY)  # no-op
    assert checkpoints.load(KEY) is None


def test_discard_ends_a_session_without_a_checkpoint(orchestrator: OrchestratorLoop) -> None:
    # discard ends a session without saving it and drops what was saved
    # before, so the next runner for the key starts from its seed.
    checkpoints = InMemoryCheckpointStore()
    net = counter_net(released())

    def factory(key: SessionKey) -> PetriRunner:
        return counter_runner(orchestrator, net, checkpoints, key).start()

    with SessionExecutorRegistry.strong_owned(checkpoints) as registry:
        runner = registry.get_or_create(KEY, factory)
        assert runner.inject(BUMP, "x")
        registry.close(KEY)
        assert checkpoints.load(KEY) is not None

        resumed = registry.get_or_create(KEY, factory)
        assert resumed.inject(BUMP, "y")
        assert registry.discard(KEY) is True
        assert resumed.await_termination(0) is True
        assert checkpoints.load(KEY) is None
        assert counter_of(snapshot(orchestrator, registry.get_or_create(KEY, factory))) == 0

        # With no runner registered, discard still forgets the checkpoint.
        registry.close(KEY)
        assert checkpoints.load(KEY) is not None
        assert registry.discard(KEY) is False
        assert checkpoints.load(KEY) is None


# ============================================================
#  Fixtures
# ============================================================

CounterNet = tuple[NetSpec, dict[str, Any]]


def counter_net(release: threading.Event) -> CounterNet:
    """``BUMP`` increments ``COUNTER``; ``WORK`` waits for ``release``, then fills ``DONE``."""

    def bump(ctx: Ctx) -> None:
        ctx.input(BUMP)
        ctx.output(COUNTER, ctx.input(COUNTER) + 1)

    async def work(ctx: Ctx) -> None:
        job = ctx.input(WORK)
        await lp.action_to_thread(release.wait)
        ctx.output(DONE, job)

    spec = NetSpec(
        "counter",
        (
            TransitionSpec("Counter_Bump", (one(BUMP), one(COUNTER)), out(COUNTER)),
            TransitionSpec("Counter_Work", (one(WORK),), out(DONE)),
        ),
    )
    return spec, {"Counter_Bump": bump, "Counter_Work": work}


def released() -> threading.Event:
    e = threading.Event()
    e.set()
    return e


def counter_runner(
    orchestrator: OrchestratorLoop,
    net: CounterNet,
    store: SessionCheckpointStore,
    key: SessionKey,
) -> Builder:
    """Seeded with a zero counter, resumed from ``store`` when it has a checkpoint."""
    spec, actions = net
    return (
        PetriRunner.builder(spec, actions)
        .environment_places(BUMP, WORK)
        .initial_marking({COUNTER: [0]})
        .resume_from(store, key)
        .orchestrator(orchestrator)
    )


def checkpoint(counter: int) -> Checkpoint:
    return {COUNTER.name: [{"value": counter, "created_at": 0}]}


def counter_of(marking: Checkpoint) -> int:
    tokens = marking[COUNTER.name]
    assert len(tokens) == 1
    return tokens[0]["value"]


def must_load(store: SessionCheckpointStore) -> Checkpoint:
    saved = store.load(KEY)
    assert saved is not None
    return saved


def snapshot(orchestrator: OrchestratorLoop, runner: PetriRunner) -> Checkpoint:
    """The running net's marking in checkpoint form (from a non-orchestrator thread)."""
    return orchestrator.call(runner.snapshot(), timeout=5).marking.snapshot()


def await_draining(orchestrator: OrchestratorLoop, runner: PetriRunner) -> None:
    """Waits until teardown has drained ``runner``: a drained executor refuses snapshots."""
    deadline = time.monotonic() + 5
    while True:
        try:
            orchestrator.call(runner.snapshot(), timeout=5)
        except RuntimeError:
            return
        if time.monotonic() > deadline:
            raise AssertionError("close never began")
        time.sleep(0.001)


def agent_factory(
    orchestrator: OrchestratorLoop, store: SessionCheckpointStore
) -> Callable[[SessionKey], Any]:
    llm = ScriptedLlm.of(*(text("ok") for _ in range(8)))
    actions = LA.action_bindings(llm, LA.Config(name="agent", model="fake-model", reask_budget=3))

    async def start(key: SessionKey) -> PetriRunner:
        return await (
            PetriRunner.builder(LA.DEF, actions)
            .environment_place(C.USER_IN)
            .resume_from(store, key)
            .orchestrator(orchestrator)
            .astart()
        )

    return start


async def take_one(sub: Any, timeout: float = 2) -> Event:
    return await asyncio.wait_for(anext(sub), timeout)


def user_message(t: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=t)])
