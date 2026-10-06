"""Asyncio plumbing between libpetri's Tokio actions and ADK's asyncio world.

Three facts drive this module (spikes S1/S2, ADR 0006):

1. A libpetri async action runs on a Tokio thread with **no running asyncio
   loop**. ``asyncio.sleep(x > 0)``, ``create_task``, ``gather`` and anything
   built on anyio/httpx (genai's async client) fail there. Every ADK coroutine
   an action awaits must hop to a real loop: :func:`on_loop`.
2. libpetri-py captures **one** asyncio loop per process for executors in
   flight; starting an executor from a second loop while one runs raises.
   Long-lived per-session executors therefore start on a single long-lived
   :class:`OrchestratorLoop`, never on whichever loop a request arrived on.
3. ADK's sync ``Runner.run`` makes a fresh loop per call, so a caller loop
   is not a stable home for anything that outlives a turn.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import contextlib
import threading
from collections.abc import AsyncIterator, Awaitable, Coroutine
from typing import Any, Generic, TypeVar

import libpetri as lp
from libpetri import _libpetri

T = TypeVar("T")


def on_loop(
    coro: Coroutine[Any, Any, T], loop: asyncio.AbstractEventLoop | None = None
) -> Awaitable[T]:
    """Run ``coro`` on a real asyncio loop and await it from inside an action.

    ``loop`` defaults to the loop libpetri captured for the running executor.
    Pass another loop (e.g. the ADK invocation's) to run the coroutine there;
    the action still awaits the result. Without ``loop`` this is
    ``libpetri.action_on_loop``.
    """
    if loop is None:
        return lp.action_on_loop(coro)
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return asyncio.wrap_future(fut, loop=_libpetri.captured_event_loop())


def on_future(fut: cf.Future[T]) -> Awaitable[T]:
    """Await a ``concurrent.futures.Future`` from inside an action."""
    return asyncio.wrap_future(fut, loop=_libpetri.captured_event_loop())


def await_on(coro: Coroutine[Any, Any, T], loop: asyncio.AbstractEventLoop) -> Awaitable[T]:
    """Await ``coro`` scheduled on ``loop`` from whatever loop is running now."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        return coro
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return asyncio.wrap_future(fut)


class OrchestratorLoop:
    """A long-lived asyncio loop on its own daemon thread.

    Every :class:`~adk_libpetri.runner.PetriRunner` starts its executor here.
    The caller owns it: create one per process (libpetri allows one captured
    loop at a time) and :meth:`close` it at shutdown. There is no library
    singleton; :meth:`shared` is a convenience the caller opts into.
    """

    _shared: OrchestratorLoop | None = None
    _shared_lock = threading.Lock()

    def __init__(self, name: str = "adk-libpetri-orchestrator") -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    @classmethod
    def shared(cls) -> OrchestratorLoop:
        """A process-wide loop, created on first use. Opt-in; close with :meth:`close_shared`."""
        with cls._shared_lock:
            if cls._shared is None or cls._shared.closed:
                cls._shared = cls()
            return cls._shared

    @classmethod
    def close_shared(cls) -> None:
        with cls._shared_lock:
            if cls._shared is not None:
                cls._shared.close()
                cls._shared = None

    @property
    def closed(self) -> bool:
        return self.loop.is_closed() or not self._thread.is_alive()

    @property
    def on_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def submit(self, coro: Coroutine[Any, Any, T]) -> cf.Future[T]:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run ``coro`` on the loop and block the calling thread for its result."""
        if self.on_thread:
            raise RuntimeError("OrchestratorLoop.call from its own thread would deadlock")
        return self.submit(coro).result(timeout)

    def run(self, coro: Coroutine[Any, Any, T]) -> Awaitable[T]:
        """Run ``coro`` on the loop and await it from the caller's loop."""
        return await_on(coro, self.loop)

    def close(self) -> None:
        if self.loop.is_closed():
            return
        if not self.on_thread and self._thread.is_alive():
            # Cancel what is still running (a runner nobody closed) so its
            # tasks end here, not as "Task was destroyed" at interpreter exit.
            with contextlib.suppress(Exception):
                self.submit(_cancel_pending()).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        if not self.on_thread:
            self._thread.join(5)
            if not self._thread.is_alive():
                self.loop.close()


async def _cancel_pending() -> None:
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


_END: Any = object()


class HotStream(Generic[T]):
    """A hot multicast stream (RxJava ``PublishProcessor`` analogue).

    :meth:`publish` is thread-safe and never blocks; each subscriber gets its
    own unbounded queue on the loop it subscribed from. A late subscriber
    misses earlier items, as with Java's hot ``Flowable``: subscribe before
    you inject.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[_Sub[T]] = []
        self._terminal: tuple[str, BaseException | None] | None = None

    def subscribe(self, loop: asyncio.AbstractEventLoop | None = None) -> AsyncIterator[T]:
        """Register a subscriber now; iterate it with ``async for`` on ``loop``.

        ``loop`` defaults to the running loop. Registration is immediate, so
        nothing published after this call is missed.
        """
        sub: _Sub[T] = _Sub(self, loop or asyncio.get_running_loop())
        with self._lock:
            if self._terminal is not None:
                kind, err = self._terminal
                sub.deliver(_END if kind == "complete" else _Err(cast_err(err)))
            else:
                self._subs.append(sub)
        return sub

    def publish(self, item: T) -> None:
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s.deliver(item)

    def complete(self) -> None:
        self._finish(("complete", None), _END)

    def error(self, err: BaseException) -> None:
        self._finish(("error", err), _Err(err))

    def _finish(self, terminal: tuple[str, BaseException | None], marker: Any) -> None:
        with self._lock:
            if self._terminal is not None:
                return
            self._terminal = terminal
            subs, self._subs = self._subs, []
        for s in subs:
            s.deliver(marker)

    @property
    def terminated(self) -> bool:
        return self._terminal is not None

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def _remove(self, sub: _Sub[T]) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)


def cast_err(err: BaseException | None) -> BaseException:
    return err if err is not None else RuntimeError("stream errored")


class _Err:
    __slots__ = ("err",)

    def __init__(self, err: BaseException) -> None:
        self.err = err


class _Sub(AsyncIterator[T]):
    def __init__(self, stream: HotStream[T], loop: asyncio.AbstractEventLoop) -> None:
        self._stream = stream
        self._loop = loop
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._done = False

    def deliver(self, item: Any) -> None:
        # Always via the loop's callback queue, even from the loop itself: a
        # direct put_nowait would overtake items other threads already queued
        # with call_soon_threadsafe, so a same-loop complete() could drop them.
        if self._loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # the loop closed after the check
            self._loop.call_soon_threadsafe(self._q.put_nowait, item)

    def __aiter__(self) -> _Sub[T]:
        return self

    async def __anext__(self) -> T:
        if self._done:
            raise StopAsyncIteration
        item = await self._q.get()
        if item is _END:
            self._done = True
            raise StopAsyncIteration
        if isinstance(item, _Err):
            self._done = True
            raise item.err
        return item

    async def aclose(self) -> None:
        self._done = True
        self._stream._remove(self)
