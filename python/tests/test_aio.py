"""``HotStream`` (RxJava ``PublishProcessor`` analogue) and ``OrchestratorLoop``.

``on_loop`` needs a running libpetri executor and is exercised by the runner tests.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest

from adk_libpetri._aio import HotStream, OrchestratorLoop, await_on

_NOTHING: Any = object()


async def _next(sub: AsyncIterator[Any], timeout: float = 0.05) -> Any:
    try:
        return await asyncio.wait_for(anext(sub), timeout)
    except TimeoutError:
        return _NOTHING


async def _values(sub: AsyncIterator[Any]) -> list[Any]:
    out: list[Any] = []
    while (item := await _next(sub)) is not _NOTHING:
        out.append(item)
    return out


# -- HotStream -------------------------------------------------------------


async def test_items_reach_a_subscriber_in_order() -> None:
    s: HotStream[int] = HotStream()
    sub = s.subscribe()
    for i in range(3):
        s.publish(i)
    assert await _values(sub) == [0, 1, 2]


async def test_is_hot_a_late_subscriber_misses_earlier_items() -> None:
    s: HotStream[str] = HotStream()
    s.publish("early")  # nobody subscribed: dropped, not buffered
    sub = s.subscribe()
    assert await _values(sub) == []
    s.publish("live")
    assert await _values(sub) == ["live"]


async def test_multiple_subscribers_each_get_every_item() -> None:
    s: HotStream[int] = HotStream()
    a, b = s.subscribe(), s.subscribe()
    assert s.subscriber_count == 2
    s.publish(1)
    s.publish(2)
    assert await _values(a) == [1, 2]
    assert await _values(b) == [1, 2]


async def test_a_subscriber_joining_midway_sees_only_later_items() -> None:
    s: HotStream[int] = HotStream()
    a = s.subscribe()
    s.publish(1)
    b = s.subscribe()
    s.publish(2)
    assert await _values(a) == [1, 2]
    assert await _values(b) == [2]


async def test_complete_ends_iteration_after_pending_items() -> None:
    s: HotStream[int] = HotStream()
    sub = s.subscribe()
    s.publish(1)
    s.publish(2)
    s.complete()
    assert [x async for x in sub] == [1, 2]
    assert s.terminated
    assert s.subscriber_count == 0
    # Iterating again after completion stays done.
    with pytest.raises(StopAsyncIteration):
        await anext(sub)


async def test_error_raises_after_pending_items() -> None:
    s: HotStream[int] = HotStream()
    sub = s.subscribe()
    s.publish(1)
    boom = ValueError("boom")
    s.error(boom)
    assert await anext(sub) == 1
    with pytest.raises(ValueError, match="boom") as info:
        await anext(sub)
    assert info.value is boom
    # A subscriber that saw the error is done.
    with pytest.raises(StopAsyncIteration):
        await anext(sub)
    assert s.terminated


async def test_terminal_is_first_wins_and_publish_after_it_is_dropped() -> None:
    s: HotStream[int] = HotStream()
    sub = s.subscribe()
    s.complete()
    s.error(RuntimeError("ignored"))
    s.complete()
    s.publish(99)
    assert [x async for x in sub] == []


async def test_late_subscriber_after_complete_completes_immediately() -> None:
    s: HotStream[int] = HotStream()
    s.publish(1)
    s.complete()
    late = s.subscribe()
    assert s.subscriber_count == 0
    assert [x async for x in late] == []


async def test_late_subscriber_after_error_gets_the_error() -> None:
    s: HotStream[int] = HotStream()
    s.error(KeyError("k"))
    late = s.subscribe()
    with pytest.raises(KeyError):
        await anext(late)


async def test_aclose_unsubscribes() -> None:
    s: HotStream[int] = HotStream()
    a, b = s.subscribe(), s.subscribe()
    await a.aclose()  # type: ignore[attr-defined]
    assert s.subscriber_count == 1
    s.publish(1)
    assert await _values(b) == [1]
    with pytest.raises(StopAsyncIteration):
        await anext(a)


async def test_publish_from_another_thread_reaches_the_subscriber_loop() -> None:
    s: HotStream[int] = HotStream()
    sub = s.subscribe()
    loop = asyncio.get_running_loop()
    delivered_on: list[threading.Thread] = []

    def producer() -> None:
        for i in range(100):
            s.publish(i)
        s.complete()

    t = threading.Thread(target=producer)
    t.start()
    got = []
    async for x in sub:
        got.append(x)
        delivered_on.append(threading.current_thread())
    t.join()
    assert got == list(range(100))
    # Items are consumed on the subscriber's own loop thread.
    assert set(delivered_on) == {threading.current_thread()}
    assert asyncio.get_running_loop() is loop


async def test_concurrent_publishers_lose_nothing() -> None:
    s: HotStream[int] = HotStream()
    sub = s.subscribe()
    threads = [
        threading.Thread(target=lambda base=base: [s.publish(base + i) for i in range(50)])
        for base in (0, 1000, 2000, 3000)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s.complete()
    got = [x async for x in sub]
    assert sorted(got) == sorted(b + i for b in (0, 1000, 2000, 3000) for i in range(50))


def test_subscribe_with_an_explicit_loop_from_a_thread_without_one() -> None:
    orch = OrchestratorLoop("test-hotstream")
    try:
        s: HotStream[str] = HotStream()
        sub = s.subscribe(orch.loop)  # no running loop on this thread

        async def collect() -> list[str]:
            return [x async for x in sub]

        fut = orch.submit(collect())
        s.publish("a")
        s.publish("b")
        s.complete()
        assert fut.result(5) == ["a", "b"]
    finally:
        orch.close()


def test_subscribe_without_a_loop_needs_a_running_one() -> None:
    with pytest.raises(RuntimeError):
        HotStream[int]().subscribe()


async def test_publish_to_a_subscriber_on_a_closed_loop_is_dropped() -> None:
    orch = OrchestratorLoop("test-closed")
    s: HotStream[int] = HotStream()
    s.subscribe(orch.loop)
    orch.close()
    assert orch.loop.is_closed()
    s.publish(1)  # must not raise
    s.complete()


# -- OrchestratorLoop ------------------------------------------------------


@pytest.fixture
def orch() -> Any:
    o = OrchestratorLoop("test-orchestrator")
    yield o
    o.close()


def test_call_runs_on_the_loop_thread_and_returns(orch: OrchestratorLoop) -> None:
    async def where() -> tuple[str, asyncio.AbstractEventLoop]:
        await asyncio.sleep(0.01)  # a real loop: sleep(x > 0) works
        return threading.current_thread().name, asyncio.get_running_loop()

    name, loop = orch.call(where(), timeout=5)
    assert name == "test-orchestrator"
    assert loop is orch.loop
    assert not orch.on_thread


def test_call_propagates_exceptions(orch: OrchestratorLoop) -> None:
    async def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        orch.call(boom(), timeout=5)


def test_call_from_its_own_thread_raises_instead_of_deadlocking(orch: OrchestratorLoop) -> None:
    async def inner() -> int:
        return 1

    async def reentrant() -> str:
        coro = inner()
        try:
            orch.call(coro)
        except RuntimeError as e:
            coro.close()
            return str(e)
        return "no error"

    assert "deadlock" in orch.call(reentrant(), timeout=5)


def test_call_timeout(orch: OrchestratorLoop) -> None:
    with pytest.raises(cf.TimeoutError):
        orch.call(asyncio.sleep(0.2), timeout=0.01)
    orch.call(asyncio.sleep(0.3), timeout=5)  # let the first finish before teardown


async def test_run_awaits_from_the_caller_loop(orch: OrchestratorLoop) -> None:
    async def on_orch() -> bool:
        await asyncio.sleep(0.01)
        return orch.on_thread

    assert await orch.run(on_orch()) is True


async def test_run_propagates_exceptions(orch: OrchestratorLoop) -> None:
    async def boom() -> None:
        raise KeyError("k")

    with pytest.raises(KeyError):
        await orch.run(boom())


def test_run_on_the_loop_itself_awaits_directly(orch: OrchestratorLoop) -> None:
    async def leaf() -> str:
        return threading.current_thread().name

    async def outer() -> str:
        return await orch.run(leaf())  # same loop: no hop, no deadlock

    assert orch.call(outer(), timeout=5) == "test-orchestrator"


async def test_await_on_hops_to_the_target_loop(orch: OrchestratorLoop) -> None:
    async def loop_of() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    assert await await_on(loop_of(), orch.loop) is orch.loop
    assert await await_on(loop_of(), asyncio.get_running_loop()) is asyncio.get_running_loop()


def test_close_stops_the_thread_and_closes_the_loop() -> None:
    o = OrchestratorLoop("test-close")
    assert not o.closed
    o.close()
    assert o.closed
    assert o.loop.is_closed()
    o.close()  # idempotent


def test_close_from_its_own_thread_stops_the_loop() -> None:
    o = OrchestratorLoop("test-self-close")

    async def self_close() -> None:
        o.close()

    o.submit(self_close()).result(5)
    o._thread.join(5)
    assert o.closed
    o.close()  # now closes the stopped loop from outside
    assert o.loop.is_closed()


def test_shared_is_opt_in_and_recreated_after_close() -> None:
    OrchestratorLoop.close_shared()
    try:
        a = OrchestratorLoop.shared()
        assert OrchestratorLoop.shared() is a
        OrchestratorLoop.close_shared()
        assert a.closed
        b = OrchestratorLoop.shared()
        assert b is not a and not b.closed
    finally:
        OrchestratorLoop.close_shared()
    OrchestratorLoop.close_shared()  # no-op when none exists
