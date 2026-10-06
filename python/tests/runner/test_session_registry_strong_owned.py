"""Port of Java ``SessionExecutorRegistryStrongOwnedTest``.

The ``strong_owned()`` mode's distinguishing behaviours: owner GC never
evicts an entry (only an explicit close does), the same owner identity reuses
the runner, and a different owner for a known key still raises. The
finalizer-owned path is in ``test_session_registry_finalizer_owned``.
"""

from __future__ import annotations

import gc
import time
import weakref
from collections.abc import Callable, Iterator

import pytest

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.runner import (
    Builder,
    PetriAgent,
    PetriRunner,
    SessionExecutorRegistry,
    SessionKey,
)
from adk_libpetri.subnet import llm_agent as LA
from support.fake_llm import ScriptedLlm

K1 = SessionKey("app", "u1", "s1")


class Owner:
    """A lifetime owner that a weak reference can observe."""


@pytest.fixture(scope="module")
def orchestrator() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop()
    yield loop
    loop.close()


def _builder(orchestrator: OrchestratorLoop) -> Builder:
    actions = LA.action_bindings(ScriptedLlm.of(), LA.Config(name="test-agent", model="m"))
    return (
        PetriRunner.builder(LA.DEF, actions).environment_place(C.USER_IN).orchestrator(orchestrator)
    )


@pytest.fixture
def new_runner(orchestrator: OrchestratorLoop) -> Callable[[SessionKey], PetriRunner]:
    return lambda _: _builder(orchestrator).start()


def test_factories_return_distinct_mode_instances() -> None:
    with (
        SessionExecutorRegistry.strong_owned() as strong,
        SessionExecutorRegistry.finalizer_owned() as finalizer,
    ):
        assert strong is not finalizer
        assert not strong.is_finalizer_owned
        assert finalizer.is_finalizer_owned
        assert strong.size() == 0
        assert finalizer.size() == 0


def test_strong_mode_pins_the_owner_so_gc_can_never_evict(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    # strong_owned() holds the owner strongly, so the registry itself keeps
    # it alive: under GC pressure it is NOT collected, no GC-driven eviction
    # is reachable, and only an explicit close removes the entry. Make the
    # strong slot hold a weak reference and the first assertion fails.
    with SessionExecutorRegistry.strong_owned() as registry:
        owner_ref = register_and_forget(registry, new_runner)

        apply_gc_pressure(0.5)

        assert owner_ref() is not None
        assert registry.size() == 1

        # Explicit close is required and sufficient.
        assert registry.close(K1) is True
        assert registry.size() == 0


def test_strong_mode_reuses_runner_for_same_owner(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    owner = Owner()
    with SessionExecutorRegistry.strong_owned() as registry:
        r1 = registry.get_or_create(K1, new_runner, owner)
        r2 = registry.get_or_create(K1, new_runner, owner)
        assert r1 is r2


def test_strong_mode_rejects_different_owner_for_same_key(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    with SessionExecutorRegistry.strong_owned() as registry:
        owner1 = Owner()
        owner2 = Owner()
        registry.get_or_create(K1, new_runner, owner1)
        with pytest.raises(RuntimeError, match="different lifetime owner"):
            registry.get_or_create(K1, new_runner, owner2)
        assert owner1 is not owner2


def test_ownerless_get_or_create_reuses_one_runner_per_key(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    def must_not_create(_: SessionKey) -> PetriRunner:
        raise AssertionError("must reuse, not create")

    with SessionExecutorRegistry.strong_owned() as registry:
        first = registry.get_or_create(K1, new_runner)
        again = registry.get_or_create(K1, must_not_create)
        assert again is first
        assert registry.close(K1) is True
        assert registry.size() == 0


def test_ownerless_and_explicit_owners_do_not_mix_for_one_key(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    with SessionExecutorRegistry.strong_owned() as registry:
        registry.get_or_create(K1, new_runner)
        with pytest.raises(RuntimeError):
            registry.get_or_create(K1, new_runner, Owner())


def test_ownerless_get_or_create_is_rejected_by_a_finalizer_owned_registry(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    # In finalizer mode the owner is what tears the runner down: it cannot be omitted.
    with SessionExecutorRegistry.finalizer_owned() as registry:
        with pytest.raises(ValueError, match="lifetime owner"):
            registry.get_or_create(K1, new_runner)
        assert registry.size() == 0


def test_builder_rejects_a_finalizer_owned_registry_without_an_owner_extractor(
    new_runner: Callable[[SessionKey], PetriRunner],
) -> None:
    with SessionExecutorRegistry.finalizer_owned() as registry:
        with pytest.raises(ValueError, match="owner_extractor"):
            PetriAgent.builder("agent", registry, new_runner).build()
        # With an extractor it builds.
        PetriAgent.builder("agent", registry, new_runner).owner_extractor(
            lambda ctx: ctx.session
        ).build()


async def test_aget_or_create_takes_an_async_factory(orchestrator: OrchestratorLoop) -> None:
    # Python-only: the async lookup awaits an async factory (astart).
    calls = 0

    async def factory(_: SessionKey) -> PetriRunner:
        nonlocal calls
        calls += 1
        return await _builder(orchestrator).astart()

    registry = SessionExecutorRegistry.strong_owned()
    try:
        r1 = await registry.aget_or_create(K1, factory)
        r2 = await registry.aget_or_create(K1, factory)
        assert r1 is r2
        assert calls == 1
        assert await registry.aclose(K1) is True
        assert r1.closed
        assert registry.size() == 0
    finally:
        await registry.aclose_all()


def register_and_forget(
    registry: SessionExecutorRegistry, factory: Callable[[SessionKey], PetriRunner]
) -> weakref.ref[Owner]:
    """Registers ``K1`` with a fresh owner, unreachable from the caller once this returns."""
    owner = Owner()
    registry.get_or_create(K1, factory, owner)
    return weakref.ref(owner)


def apply_gc_pressure(seconds: float) -> None:
    """Collects repeatedly for a fixed window, asserting nothing.

    For checks that an object is *not* collected: there is no condition to
    await, only every chance given to a collector.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        gc.collect()
        time.sleep(0.05)
