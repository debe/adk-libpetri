"""The bidirectional pump wiring an ADK BIDI/Live channel to a session runner (``@experimental``).

What the library owns:

* **Input pump**: each ``LiveRequest`` from the ADK ``LiveRequestQueue`` goes
  to the connection (``blob -> send_realtime``, ``content -> send_content``,
  ``close -> close``).
* **Output pump**: the connection's raw server stream is read and each frame
  handed to ``on_server_message``. The stream the bridge returns is the net's
  egress (``runner.adk_events()``) *only*.
* **Dispose**: both pumps stop, and the connection closes, when the egress
  ends or the consumer stops iterating, on every outcome.

**The net authors every event.** The bridge maps no frames to events. A model
turn enters the net like every external signal: ``on_server_message`` calls
``runner.inject(MODEL_CHUNK, content)`` and ``runner.signal(TURN_COMPLETE)``;
a transition authors the outbound ``Event``. Keeping content in the marking
is what lets a barge-in wipe the queued backlog with a reset arc.

**Egress order is net structure, not callback discipline.** Injection order
is preserved, firing is not: a burst admitted in one pass can let a terminal
transition fire between chunks. Inhibit the terminal transition on the chunk
place so it cannot fire while content is queued. Priority is not a substitute.

A transport error from ``raw_receive`` ends the returned stream with that
error instead of stranding the consumer on a dead connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.events.event import Event

from .._experimental import experimental
from .live_connection import LiveConnection
from .petri_runner import PetriRunner

log = logging.getLogger("adk_libpetri.bidi")


@experimental
async def bridge(
    inbound: LiveRequestQueue,
    connection: LiveConnection,
    runner: PetriRunner,
    on_server_message: Callable[[Any, PetriRunner], None],
) -> AsyncGenerator[Event, None]:
    """Run both pumps; yield the net's egress events until it ends or the caller stops."""
    loop = asyncio.get_running_loop()
    events = runner.adk_events().subscribe()
    transport_error: list[BaseException] = []

    async def input_pump() -> None:
        try:
            while True:
                req = await inbound.get()
                if req.blob is not None:
                    await connection.send_realtime(req.blob)
                if req.content is not None:
                    await connection.send_content(req.content)
                if req.close:
                    await connection.close()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("live input pump failed; closing the connection", exc_info=True)
            with contextlib.suppress(Exception):
                await connection.close()

    async def output_pump() -> None:
        try:
            async for msg in connection.raw_receive():
                on_server_message(msg, runner)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            transport_error.append(err)

    inp = loop.create_task(input_pump())
    outp = loop.create_task(output_pump())
    nxt: asyncio.Task[Event] | None = None
    try:
        while True:
            nxt = loop.create_task(events.__anext__())  # type: ignore[attr-defined]
            done, _ = await asyncio.wait({nxt, outp}, return_when=asyncio.FIRST_COMPLETED)
            if nxt in done:
                try:
                    event = nxt.result()
                except StopAsyncIteration:
                    return
                nxt = None
                yield event
                continue
            if transport_error:
                raise transport_error[0]
            # The server stream completed normally: the egress, hot and
            # long-lived, keeps going until the net or the caller ends it.
            outp = loop.create_future()
    finally:
        for t in (nxt, inp, outp):
            if t is not None and not t.done():
                t.cancel()
        await events.aclose()  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            await connection.close()
