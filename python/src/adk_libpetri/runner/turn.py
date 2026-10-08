"""One turn against a session runner (``PetriAgent``, ``PetriWorkflow``, ``PetriNet``)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.events.event import Event

from .. import colours as C
from .._aio import HotStream
from ..bridge import TransitionFailure
from .petri_runner import PetriRunner

_ABORTS = "adk_libpetri.turn.aborts_wired"


def abort_turns_on_failure(runner: PetriRunner) -> None:
    """One subscription per runner: every failure signals ``TURN_ABORT``.

    A failed transition keeps its turn from ever ending; on a one-turn-at-a-
    time net every later input would queue behind it. The abort clears that
    turn. It runs on the orchestrator loop and ends with the run.
    """
    if runner.attachments.get(_ABORTS) or not runner.declares_environment_place(C.TURN_ABORT):
        return
    runner.attachments[_ABORTS] = True
    sub = runner.failure_signal().subscribe(loop=runner.loop.loop)

    async def pump() -> None:
        async for _ in sub:
            runner.signal(C.TURN_ABORT)

    runner.loop.submit(pump())


async def run_turn(
    invocation_id: str,
    runner: PetriRunner,
    inject: Callable[[], bool],
    *,
    sse: bool = False,
    abort_signal: asyncio.Event | None = None,
    finish: Callable[[Event], Event] = lambda e: e,
    egress: HotStream[Any] | None = None,
    relay: Callable[[Any], bool] | None = None,
) -> AsyncGenerator[Any, None]:
    """The first of (terminal event, failure, abort) settles the turn.

    Both subscriptions are made before the inject (the streams are hot) and
    dropped with the turn, so nothing outlives the invocation on a long-lived
    runner. Every event is stamped with ``invocation_id``. Under ``sse``
    partials are yielded too; otherwise only the first non-partial event.

    ``egress`` replaces :meth:`PetriRunner.adk_events` as the stream the turn
    waits on. An item on it that is not an ``Event`` is terminal and yielded
    as is (``PetriNet`` taps every ``EVENT_OUT`` token that way), unless
    ``relay`` accepts it: then it is yielded and the turn goes on.
    """
    loop = asyncio.get_running_loop()
    events: Any = (egress if egress is not None else runner.adk_events()).subscribe()
    failures: Any = runner.failure_signal().subscribe()
    f_task = loop.create_task(failures.__anext__())
    a_task = loop.create_task(abort_signal.wait()) if abort_signal is not None else None
    e_task: asyncio.Task[Any] | None = None
    try:
        if not inject():
            raise RuntimeError("the session's runner is closed; it accepted no input")
        while True:
            if e_task is None:
                e_task = loop.create_task(events.__anext__())
            waiting = {e_task, f_task} | ({a_task} if a_task else set())
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if e_task in done:
                try:
                    event = e_task.result()
                except StopAsyncIteration:
                    raise RuntimeError(
                        "net event stream completed without emitting a terminal Event "
                        "for this invocation"
                    ) from None
                e_task = None
                if relay is not None and relay(event):
                    yield event
                    continue
                if not isinstance(event, Event):
                    yield event
                    return
                stamped = event.model_copy(update={"invocation_id": invocation_id})
                if stamped.partial:
                    if sse:
                        yield stamped
                    continue
                yield finish(stamped)
                return
            if f_task in done:
                try:
                    failure = f_task.result()
                except StopAsyncIteration:
                    raise RuntimeError("the session's net ended mid-turn") from None
                raise failure if isinstance(failure, TransitionFailure) else RuntimeError(failure)
            if a_task is not None and a_task in done:
                if runner.declares_environment_place(C.TURN_ABORT):
                    runner.signal(C.TURN_ABORT)
                return
    finally:
        for t in (e_task, f_task, a_task):
            if t is not None and not t.done():
                t.cancel()
        await events.aclose()
        await failures.aclose()
