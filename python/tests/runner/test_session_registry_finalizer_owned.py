"""Port of Java ``SessionExecutorRegistryTest`` (``cleanerOwned`` -> ``finalizer_owned``).

Java's ``Cleaner`` is ``weakref.finalize`` here. CPython collects an owner as
soon as its last reference goes, so the GC waits below rarely need to loop;
they still poll with ``gc.collect()`` and fail rather than time out silently.
"""

from __future__ import annotations

import gc
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.runner import PetriRunner, SessionExecutorRegistry, SessionKey
from adk_libpetri.subnet import llm_agent as LA
from support.fake_llm import ScriptedLlm

K1 = SessionKey("app", "u1", "s1")
K2 = SessionKey("app", "u1", "s2")
K3 = SessionKey("app", "u2", "s1")


class Owner:
    """A lifetime owner. ``object()`` itself cannot be weakly referenced."""


@pytest.fixture(scope="module")
def orchestrator() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop()
    yield loop
    loop.close()


@pytest.fixture
def new_runner(orchestrator: OrchestratorLoop) -> Callable[[SessionKey], PetriRunner]:
    def make(_: SessionKey) -> PetriRunner:
        # A minimal long-lived runner: not driven, only its identity matters.
        actions = LA.action_bindings(ScriptedLlm.of(), LA.Config(name="test-agent", model="m"))
        return (
            PetriRunner.builder(LA.DEF, actions)
            .environment_place(C.USER_IN)
            .orchestrator(orchestrator)
            .start()
        )

    return make


def test_get_or_create_lazily_builds_runner_on_first_call_only(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    calls = 0
    owner = Owner()

    def factory(k: SessionKey) -> PetriRunner:
        nonlocal calls
        calls += 1
        return new_runner(k)

    with SessionExecutorRegistry.finalizer_owned() as registry:
        r1 = registry.get_or_create(K1, factory, owner)
        r2 = registry.get_or_create(K1, factory, owner)
        r3 = registry.get_or_create(K1, factory, owner)

        assert calls == 1
        assert r1 is r2
        assert r2 is r3


def test_different_session_keys_get_different_runners(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    owner = Owner()
    with SessionExecutorRegistry.finalizer_owned() as registry:
        r1 = registry.get_or_create(K1, new_runner, owner)
        r2 = registry.get_or_create(K2, new_runner, owner)
        r3 = registry.get_or_create(K3, new_runner, owner)

        assert r1 is not r2
        assert r2 is not r3
        assert registry.size() == 3


def test_same_key_with_different_owner_throws(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    owner1 = Owner()
    owner2 = Owner()
    with SessionExecutorRegistry.finalizer_owned() as registry:
        registry.get_or_create(K1, new_runner, owner1)
        with pytest.raises(RuntimeError, match="different lifetime owner"):
            registry.get_or_create(K1, new_runner, owner2)
        # Keep both owners alive past the assertion.
        assert owner1 is not owner2


def test_mixing_ownerless_and_owned_calls_for_one_key_names_the_mix(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    # An owner mismatch too, but the message says which forms were mixed
    # rather than blaming an unstable ctx.session.
    owner = Owner()
    with SessionExecutorRegistry.strong_owned() as registry:
        registry.get_or_create(K1, new_runner)
        with pytest.raises(RuntimeError) as owned_after_ownerless:
            registry.get_or_create(K1, new_runner, owner)
        assert "first requested without an owner" in str(owned_after_ownerless.value)
        assert "ctx.session" not in str(owned_after_ownerless.value)

        registry.get_or_create(K2, new_runner, owner)
        with pytest.raises(RuntimeError) as ownerless_after_owned:
            registry.get_or_create(K2, new_runner)
        assert "first requested with an owner" in str(ownerless_after_owned.value)


def test_close_removes_one_runner_and_returns_true(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    owner = Owner()
    with SessionExecutorRegistry.finalizer_owned() as registry:
        registry.get_or_create(K1, new_runner, owner)
        registry.get_or_create(K2, new_runner, owner)

        assert registry.close(K1) is True
        assert registry.size() == 1
        assert registry.get(K1) is None
        assert registry.get(K2) is not None


def test_close_missing_key_is_noop_returning_false() -> None:
    with SessionExecutorRegistry.finalizer_owned() as registry:
        assert registry.close(K1) is False


def test_close_all_removes_every_runner(new_runner: Callable[[SessionKey], PetriRunner]) -> None:
    owner = Owner()
    registry = SessionExecutorRegistry.finalizer_owned()
    runners = [registry.get_or_create(k, new_runner, owner) for k in (K1, K2, K3)]
    assert registry.size() == 3

    registry.close_all()
    assert registry.size() == 0
    assert all(r.closed for r in runners)


def test_finalizer_owned_factory_yields_a_working_finalizer_registry(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    owner = Owner()
    with SessionExecutorRegistry.finalizer_owned() as registry:
        assert registry.is_finalizer_owned
        r1 = registry.get_or_create(K1, new_runner, owner)
        r2 = registry.get_or_create(K1, new_runner, owner)
        assert r1 is r2
        assert registry.size() == 1


def test_owner_gc_triggers_finalizer_shutdown(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    # The leak-prevention contract: once the caller's lifetime owner is
    # unreachable, the finalizer attached at registration tears the runner
    # down -- no orphaned executor, hot stream or marking.
    registry = SessionExecutorRegistry.finalizer_owned()
    # Sanity inside the helper: the runner is registered while the owner lives.
    owner_ref, runner = create_and_forget(registry, new_runner)

    await_gc(lambda: owner_ref() is None)
    await_gc(lambda: registry.size() == 0)

    assert registry.size() == 0
    assert runner.await_termination(2)


def test_explicit_close_then_owner_gc_is_safe(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    with SessionExecutorRegistry.finalizer_owned() as registry:
        owner = Owner()
        owner_ref = weakref.ref(owner)
        registry.get_or_create(K1, new_runner, owner)
        # Explicitly close before GC.
        assert registry.close(K1) is True
        assert registry.size() == 0
        # Now let the finalizer fire: it finds no entry and does nothing.
        del owner
        await_gc(lambda: owner_ref() is None)
        time.sleep(0.1)
        assert registry.size() == 0
        # And the key is free for a new session.
        again = Owner()
        assert registry.get_or_create(K1, new_runner, again) is registry.get(K1)


def test_an_owner_that_cannot_be_weakly_referenced_is_rejected_before_the_factory_runs(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    # Python-only: object(), str and int owners cannot carry a finalizer.
    # Rejecting one after the factory ran would orphan a started runner.
    calls = 0

    def factory(k: SessionKey) -> PetriRunner:
        nonlocal calls
        calls += 1
        return new_runner(k)

    with SessionExecutorRegistry.finalizer_owned() as registry:
        with pytest.raises(TypeError, match="weakly referenceable"):
            registry.get_or_create(K1, factory, object())
        assert calls == 0
        assert registry.size() == 0


def test_concurrent_first_call_installs_exactly_one_runner_and_closes_loser(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    owner = Owner()
    lock = threading.Lock()
    calls = 0
    entered = threading.Barrier(2, timeout=2)
    release = threading.Event()
    created: list[PetriRunner] = []

    def factory(k: SessionKey) -> PetriRunner:
        nonlocal calls
        with lock:
            calls += 1
        entered.wait()
        assert release.wait(5)
        runner = new_runner(k)
        with lock:
            created.append(runner)
        return runner

    with SessionExecutorRegistry.finalizer_owned() as registry:
        with ThreadPoolExecutor(2) as pool:
            f1 = pool.submit(registry.get_or_create, K1, factory, owner)
            f2 = pool.submit(registry.get_or_create, K1, factory, owner)
            try:
                deadline = time.monotonic() + 2
                while calls < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                assert calls == 2
            finally:
                release.set()
            runners = (f1.result(5), f2.result(5))

        assert runners[0] is runners[1]
        assert len(created) == 2
        loser = created[1] if created[0] is runners[0] else created[0]
        assert loser.await_termination(2) is True
        assert registry.size() == 1


def create_and_forget(
    registry: SessionExecutorRegistry, factory: Callable[[SessionKey], PetriRunner]
) -> tuple[weakref.ref[Owner], PetriRunner]:
    """Registers ``K1`` with a fresh owner, unreachable once this returns."""
    owner = Owner()
    runner = registry.get_or_create(K1, factory, owner)
    assert registry.size() == 1
    return weakref.ref(owner), runner


def await_gc(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Polls until ``condition`` holds, collecting each round.

    Raises rather than returns on timeout: otherwise a caller's headline
    assertion would pass without ever exercising the GC path it names.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        gc.collect()
        time.sleep(0.05)
    raise AssertionError(
        f"owner was not collected within {timeout}s; the GC-dependent property never ran"
    )
