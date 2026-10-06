"""Two-stage silence recovery: the nudge, then reconnect, escalation ladder for Live/BIDI.

Port of Java ``LiveApiRecoverySubnet``.

**Why a net.** Live voice agents share a "model went silent mid-turn" failure:
the connection is up, but the model produces nothing for seconds. The fix is a
soft nudge (re-send ``turn_complete=True``), then a hard reconnect if silence
continues, and neither may fire once the model answers. With callbacks and
timers this is where the off-by-one bugs live (the nudge fires just as the
model starts speaking, the reconnect throws away an in-flight reply, the timer
was not reset on resume).

**Cancel on activity.** Each rung is a place drained by a timed transition:
``RESPONSE_AWAITED`` by ``Nudge``, ``RECOVERY_PENDING`` by ``Recover``. Both
are inhibited by ``MODEL_ACTIVE``, so neither fires while the model produces
output. The model answering also *cancels* the rung it lands on:
``Answered`` consumes ``RESPONSE_AWAITED`` and ``AnsweredLate`` consumes
``RECOVERY_PENDING``, each reading ``MODEL_ACTIVE`` at a priority above the
timers. Once the model has answered nothing of the ladder survives, so a later
silence cannot escalate from a stale rung.

``MODEL_ACTIVE`` has an in-net consumer: when the model stops, the caller
injects ``MODEL_QUIET`` and ``ModelQuiet`` takes it with *every* ``MODEL_ACTIVE``
token (``all``, so stacked activity clears in one firing). ``IgnoreQuiet``
sinks a ``MODEL_QUIET`` that arrives while the model is not active; its reset
arc keeps that sink at one token::

    [RESPONSE_AWAITED] --Nudge  (delayed nudge_after)-----> and([NUDGE_NEEDED], [RECOVERY_PENDING])
                          inhibitor MODEL_ACTIVE
    [RECOVERY_PENDING] --Recover (delayed reconnect_after)-> [RECONNECT_NEEDED]
                          inhibitor MODEL_ACTIVE
    [RESPONSE_AWAITED] --Answered-->      (nothing)  read MODEL_ACTIVE, priority 10
    [RECOVERY_PENDING] --AnsweredLate-->  (nothing)  read MODEL_ACTIVE, priority 10
    [MODEL_QUIET] + all([MODEL_ACTIVE]) --ModelQuiet--> (nothing)
    [MODEL_QUIET] --IgnoreQuiet--> [QUIET_IGNORED]   inhibitor MODEL_ACTIVE, reset QUIET_IGNORED

Every signal, in or out, is an env-place token (commitment 1): the host injects
``RESPONSE_AWAITED`` right after it sends ``turn_complete=True``, injects
``MODEL_ACTIVE``/``MODEL_QUIET`` as output starts and stops, and watches
``NUDGE_NEEDED`` and ``RECONNECT_NEEDED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from adk_libpetri._spec import (
    Action,
    Ctx,
    NetSpec,
    Place,
    Port,
    TransitionSpec,
    all_tokens,
    and_,
    delayed,
    one,
    out,
)
from adk_libpetri.subnet import bind

NAME = "LiveApiRecovery"


class Transitions:
    NUDGE = f"{NAME}_Nudge"
    RECOVER = f"{NAME}_Recover"
    ANSWERED = f"{NAME}_Answered"
    ANSWERED_LATE = f"{NAME}_AnsweredLate"
    MODEL_QUIET = f"{NAME}_ModelQuiet"
    IGNORE_QUIET = f"{NAME}_IgnoreQuiet"


class Places:
    RESPONSE_AWAITED: Place[None] = Place(f"{NAME}_responseAwaited")
    """Caller signals "the model should be talking but isn't"."""

    MODEL_ACTIVE: Place[None] = Place(f"{NAME}_modelActive")
    """Caller injects when the model produces output. Inhibits both timed rungs,
    cancels the rung it lands on, and is cleared in-net by ``MODEL_QUIET``."""

    MODEL_QUIET: Place[None] = Place(f"{NAME}_modelQuiet")
    """Caller injects when the model stops producing output; clears ``MODEL_ACTIVE``."""

    QUIET_IGNORED: Place[None] = Place(f"{NAME}_quietIgnored")
    """Sink for a ``MODEL_QUIET`` while the model is not active; holds at most one token."""

    RECOVERY_PENDING: Place[None] = Place(f"{NAME}_recoveryPending")
    """Internal: between Nudge and Recover."""

    NUDGE_NEEDED: Place[None] = Place(f"{NAME}_nudgeNeeded")
    """Caller observes: nudge the model with another ``turn_complete``."""

    RECONNECT_NEEDED: Place[None] = Place(f"{NAME}_reconnectNeeded")
    """Caller observes: re-establish the Live-API session."""


def _ms(d: timedelta) -> int:
    return round(d.total_seconds() * 1000)


@dataclass(frozen=True)
class Config:
    nudge_after: timedelta
    reconnect_after: timedelta

    def __post_init__(self) -> None:
        if self.nudge_after <= timedelta(0):
            raise ValueError(f"nudge_after must be positive: {self.nudge_after}")
        if self.reconnect_after <= timedelta(0):
            raise ValueError(f"reconnect_after must be positive: {self.reconnect_after}")

    @staticmethod
    def defaults() -> Config:
        """3 s of silence triggers the nudge, 3 s more the reconnect."""
        return Config(timedelta(seconds=3), timedelta(seconds=3))


def def_(config: Config) -> NetSpec:
    return NetSpec(
        NAME,
        (
            TransitionSpec(
                Transitions.NUDGE,
                (one(Places.RESPONSE_AWAITED),),
                and_(Places.NUDGE_NEEDED, Places.RECOVERY_PENDING),
                inhibitors=(Places.MODEL_ACTIVE,),
                timing=delayed(_ms(config.nudge_after)),
            ),
            TransitionSpec(
                Transitions.RECOVER,
                (one(Places.RECOVERY_PENDING),),
                out(Places.RECONNECT_NEEDED),
                inhibitors=(Places.MODEL_ACTIVE,),
                timing=delayed(_ms(config.reconnect_after)),
            ),
            # The model answered: cancel the rung it landed on. Priority above
            # the timers, which the MODEL_ACTIVE inhibitor already holds off.
            TransitionSpec(
                Transitions.ANSWERED,
                (one(Places.RESPONSE_AWAITED),),
                reads=(Places.MODEL_ACTIVE,),
                priority=10,
            ),
            TransitionSpec(
                Transitions.ANSWERED_LATE,
                (one(Places.RECOVERY_PENDING),),
                reads=(Places.MODEL_ACTIVE,),
                priority=10,
            ),
            # The model went quiet: clear every stacked MODEL_ACTIVE token.
            # all() enables only on 1+ tokens; IgnoreQuiet covers zero.
            TransitionSpec(
                Transitions.MODEL_QUIET,
                (one(Places.MODEL_QUIET), all_tokens(Places.MODEL_ACTIVE)),
            ),
            # The reset keeps the sink at one token, so a stream of quiet
            # signals with the model idle does not grow the marking.
            TransitionSpec(
                Transitions.IGNORE_QUIET,
                (one(Places.MODEL_QUIET),),
                out(Places.QUIET_IGNORED),
                inhibitors=(Places.MODEL_ACTIVE,),
                resets=(Places.QUIET_IGNORED,),
            ),
        ),
        ports=(
            Port("responseAwaited", "in", Places.RESPONSE_AWAITED),
            Port("modelActive", "inout", Places.MODEL_ACTIVE),
            Port("modelQuiet", "in", Places.MODEL_QUIET),
            Port("nudgeNeeded", "out", Places.NUDGE_NEEDED),
            Port("reconnectNeeded", "out", Places.RECONNECT_NEEDED),
            Port("quietIgnored", "out", Places.QUIET_IGNORED),
        ),
    )


def _nudge(ctx: Ctx) -> None:
    ctx.input(Places.RESPONSE_AWAITED)
    ctx.signal(Places.NUDGE_NEEDED)
    ctx.signal(Places.RECOVERY_PENDING)


def _recover(ctx: Ctx) -> None:
    ctx.input(Places.RECOVERY_PENDING)
    ctx.signal(Places.RECONNECT_NEEDED)


def _consume_only(ctx: Ctx) -> None:
    """No outputs: the firing itself consumes the inputs."""


def _ignore_quiet(ctx: Ctx) -> None:
    ctx.input(Places.MODEL_QUIET)
    ctx.signal(Places.QUIET_IGNORED)


def action_bindings(config: Config) -> dict[str, Action]:
    """Pure ctx-only actions: the behaviour is the topology, timing and inhibitors."""
    return bind(
        def_(config),
        {
            Transitions.NUDGE: _nudge,
            Transitions.RECOVER: _recover,
            Transitions.ANSWERED: _consume_only,
            Transitions.ANSWERED_LATE: _consume_only,
            Transitions.MODEL_QUIET: _consume_only,
            Transitions.IGNORE_QUIET: _ignore_quiet,
        },
    )
