"""Test helpers shared by the voice demos: the virtual-clock gate and marking polls.

**Virtual clock.** Java's timing tests run on ``ManualClock``. Its libpetri-py
counterpart, ``lp.SteppedClock`` (``advance_ms``, ``asettle_after(action)``),
reaches the executor through
``PetriRunner.builder(...).clock(c).deadline_tolerance(timedelta(0))``. A clock
serves one run only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import libpetri as lp

from adk_libpetri._spec import Place
from adk_libpetri.runner import PetriRunner


def stepped_clock() -> lp.SteppedClock:
    return lp.SteppedClock()


SETTLE_TIMEOUT_S = 5.0


async def settle(clock: Any, action: Callable[[], Any] | None = None) -> None:
    """Run ``action``, then wait until the executor has parked again (Java ``settle``).

    Without an action, wait until the executor is parked now.
    """
    if action is None:
        ok = await clock.asettle(SETTLE_TIMEOUT_S)
    else:
        ok = await clock.asettle_after(action, SETTLE_TIMEOUT_S)
    if not ok:
        raise AssertionError(f"executor did not settle within {SETTLE_TIMEOUT_S}s")


async def advance_and_settle(clock: Any, ms: int) -> None:
    """Java ``ManualClock.advanceAndSettle``: move time by ``ms``, then settle."""
    await settle(clock, lambda: clock.advance_ms(ms))


async def marking(runner: PetriRunner) -> lp.MarkingView:
    return (await runner.snapshot()).marking


async def count(runner: PetriRunner, place: Place[Any]) -> int:
    return (await marking(runner)).count(place.name)


async def marked(runner: PetriRunner, place: Place[Any]) -> bool:
    return await count(runner, place) > 0


async def until_marked(runner: PetriRunner, place: Place[Any], timeout: float = 2.0) -> None:
    """Await a token on ``place``: a stronger barrier than quiescence."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await marked(runner, place):
            return
        await asyncio.sleep(0.002)
    raise AssertionError(f"{place.name} was not marked within {timeout}s")


async def until(cond: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"condition was not met within {timeout}s")


async def until_settled(
    runner: PetriRunner, *empty: Place[Any], timeout: float = 2.0
) -> lp.MarkingView:
    """No action in flight and every place in ``empty`` drained."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        snap = await runner.snapshot()
        if snap.is_restore_point and not any(snap.marking.count(p.name) for p in empty):
            return snap.marking
        await asyncio.sleep(0.002)
    raise AssertionError(f"runner did not settle within {timeout}s")
