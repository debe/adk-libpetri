"""Voice-activity-gated barge-in: the ``BARGE_IN_SENT`` / ``INTERRUPT_DISCARDED`` pair.

Port of Java ``BargeInSubnet``.

**Why a net.** Barge-in is the classic voice-agent race: the user starts
speaking, and the system must decide "is the user still speaking right now?"
to route the interrupt. Shared mutable flags are where the lost-update and
stale-read bugs live. Here two transitions compete for the *same*
``INTERRUPTED`` token, one with ``read(VOICE_ACTIVITY_OPEN)`` and one with
``inhibitor(VOICE_ACTIVITY_OPEN)``. The marking is the single source of truth
for "is the user speaking": at any marking exactly one of the two is enabled,
so the decision is structural, not procedural::

    [INTERRUPTED] --BargeIn_SendBargeIn-------> [BARGE_IN_SENT]
                     read(VOICE_ACTIVITY_OPEN)        (user IS speaking: send)
    [INTERRUPTED] --BargeIn_DiscardInterrupt--> [INTERRUPT_DISCARDED]
                     inhibitor(VOICE_ACTIVITY_OPEN)   (user stopped: no-op)

**Caller wiring.** Inject ``INTERRUPTED`` when voice-activity detection
fires; open and close ``VOICE_ACTIVITY_OPEN`` as the voice window opens and
closes (or compose :mod:`.vad_subnet`, which does it from speech edges); watch
``BARGE_IN_SENT`` to cancel in-flight model audio. ``INTERRUPT_DISCARDED`` is
the no-op branch, usually just observed.
"""

from __future__ import annotations

from adk_libpetri._spec import Action, Ctx, NetSpec, Place, Port, TransitionSpec, one, out
from adk_libpetri.subnet import bind

NAME = "BargeIn"


class Transitions:
    SEND_BARGE_IN = f"{NAME}_SendBargeIn"
    DISCARD_INTERRUPT = f"{NAME}_DiscardInterrupt"


class Places:
    INTERRUPTED: Place[None] = Place(f"{NAME}_interrupted")
    """Caller injects when voice-activity detection fires."""

    VOICE_ACTIVITY_OPEN: Place[None] = Place(f"{NAME}_voiceActivityOpen")
    """Caller injects/drains as the user's voice window opens/closes."""

    BARGE_IN_SENT: Place[None] = Place(f"{NAME}_bargeInSent")
    """Caller observes: a barge-in signal should be sent."""

    INTERRUPT_DISCARDED: Place[None] = Place(f"{NAME}_interruptDiscarded")
    """Caller observes: the interrupt was correctly ignored (user already stopped)."""


DEF = NetSpec(
    NAME,
    (
        TransitionSpec(
            Transitions.SEND_BARGE_IN,
            (one(Places.INTERRUPTED),),
            out(Places.BARGE_IN_SENT),
            reads=(Places.VOICE_ACTIVITY_OPEN,),
        ),
        TransitionSpec(
            Transitions.DISCARD_INTERRUPT,
            (one(Places.INTERRUPTED),),
            out(Places.INTERRUPT_DISCARDED),
            inhibitors=(Places.VOICE_ACTIVITY_OPEN,),
        ),
    ),
    ports=(
        Port("interrupted", "in", Places.INTERRUPTED),
        Port("voiceActivityOpen", "inout", Places.VOICE_ACTIVITY_OPEN),
        Port("bargeInSent", "out", Places.BARGE_IN_SENT),
        Port("interruptDiscarded", "out", Places.INTERRUPT_DISCARDED),
    ),
)


def _send(ctx: Ctx) -> None:
    ctx.input(Places.INTERRUPTED)
    ctx.signal(Places.BARGE_IN_SENT)


def _discard(ctx: Ctx) -> None:
    ctx.input(Places.INTERRUPTED)
    ctx.signal(Places.INTERRUPT_DISCARDED)


def action_bindings() -> dict[str, Action]:
    return bind(DEF, {Transitions.SEND_BARGE_IN: _send, Transitions.DISCARD_INTERRUPT: _discard})
