"""Stock subnet persisting ``LegacySessionWrite`` tokens through ``append_event``.

    [LEGACY_SESSION_WRITE] --PersistState_Persist--> (sink)

The only place state writes happen in an adk-libpetri net: every envelope
funnels through the single ``Persist`` transition, so concurrent producers
cannot race on ``Session.state``. The Rust executor libpetri-py runs on may
start a transition again while an earlier firing of it is still in flight
(CONC-002), so the bound actions also hold a lock around ``append_event``:
appends run one at a time. The action fails with ``TimeoutError`` once
``persist_timeout`` passes, so a hung session service surfaces as a
``TransitionFailed`` instead of holding the transition forever. The timeout
runs in real time, also under an injected clock.

A timing ``deadline`` on the transition would bound something else (how long
it may stay enabled before it starts), and a late orchestrator reaps a
missed-deadline transition, stranding the write (found by the stock proofs).
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.session import Session

from .. import colours as C
from .._aio import on_loop
from .._spec import Action, Ctx, NetSpec, Port, TransitionSpec, one
from ._common import IdSupplier, random_id
from .actions import bind

NAME = "PersistState"
DEFAULT_PERSIST_TIMEOUT = timedelta(seconds=5)


class Transitions:
    PERSIST = f"{NAME}_Persist"


@dataclass(frozen=True)
class Config:
    author: str
    session_service: BaseSessionService
    session_supplier: Callable[[], Session]
    invocation_id_supplier: IdSupplier = field(default=random_id)
    persist_timeout: timedelta = DEFAULT_PERSIST_TIMEOUT

    def __post_init__(self) -> None:
        if self.persist_timeout <= timedelta(0):
            raise ValueError(f"persist_timeout must be positive, got: {self.persist_timeout}")


DEF = NetSpec(
    NAME,
    (TransitionSpec(Transitions.PERSIST, (one(C.LEGACY_SESSION_WRITE),)),),
    ports=(Port("legacySessionWrite", "in", C.LEGACY_SESSION_WRITE),),
)


def action_bindings(config: Config) -> dict[str, Action]:
    # One lock per loop the appends run on (an asyncio.Lock binds to one loop).
    locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
        weakref.WeakKeyDictionary()
    )

    async def persist(ctx: Ctx) -> None:
        write = ctx.input(C.LEGACY_SESSION_WRITE)
        event = Event(
            invocation_id=config.invocation_id_supplier(),
            author=config.author,
            actions=EventActions(state_delta=dict(write.delta)),
        )

        async def append() -> None:
            lock = locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
            async with lock:
                await asyncio.wait_for(
                    config.session_service.append_event(config.session_supplier(), event),
                    config.persist_timeout.total_seconds(),
                )

        await on_loop(append())

    return bind(DEF, {Transitions.PERSIST: persist})


def legacy_write(delta: Mapping[str, Any]) -> C.LegacySessionWrite:
    """The sanctioned way to build an envelope: names the legacy bridge at the call site."""
    return C.LegacySessionWrite(dict(delta))


def merge(envelopes: Iterable[C.LegacySessionWrite]) -> C.LegacySessionWrite:
    """Merge envelopes in order (later keys win), for batching before ``Persist``."""
    merged: dict[str, Any] = {}
    for e in envelopes:
        merged.update(e.delta)
    return C.LegacySessionWrite(merged)
