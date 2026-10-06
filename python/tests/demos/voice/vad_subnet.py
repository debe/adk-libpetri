"""Voice-activity-detection *producer*: speech edges in, the barge-in voice window out.

Port of Java ``VadSubnet``. :mod:`.barge_in_subnet` and the recovery and
streaming subnets *consume* a ``VOICE_ACTIVITY_OPEN`` window token; this
subnet produces it from the Live API's speech-activity edges, structurally.

**Where the edges come from (Python differs from Java here).** ADK Java's
``GeminiLlmConnection`` drops the server-side VAD edges, so the Java exemplar
reads genai's Live session directly. ADK Python 2.11 does *not* drop them:
``GeminiLlmConnection.receive`` yields ``LlmResponse(voice_activity=...)`` and
the live flow copies it onto ``Event.voice_activity`` (locked by
``test_voice_vad_edge_adk_foil``). Either source works; the decode is the same::

    match msg.voice_activity.voice_activity_type:          # LiveServerMessage, or
        case types.VoiceActivityType.ACTIVITY_START:       # an ADK Event's field
            runner.signal(vad_subnet.Places.SPEECH_STARTED)
        case types.VoiceActivityType.ACTIVITY_END:
            runner.signal(vad_subnet.Places.SPEECH_STOPPED)

(:meth:`.genai_live_connection.GenaiLiveConnection.voice_signals` is that decode.)

**Why a net and not a boolean.** "Is the user speaking right now" is the shared
mutable flag behind barge-in races. Here it is the marking of one place,
:data:`VOICE_ACTIVITY_OPEN`, the *same* place barge-in reads (identical name and
type, so composition fuses them). Open and close are idempotent by structure:
a redundant start while open, or a stray stop while closed, is absorbed by a
read- or inhibitor-guarded ignore branch instead of corrupting the count::

    [SPEECH_STARTED] --Vad_OpenWindow-----------> [VOICE_ACTIVITY_OPEN]
                        inhibitor(VOICE_ACTIVITY_OPEN)   (closed: open it)
    [SPEECH_STARTED] --Vad_IgnoreRedundantStart-> [SPEECH_EDGE_IGNORED]
                        read(VOICE_ACTIVITY_OPEN)        (already open: absorb)
    [SPEECH_STOPPED] + [VOICE_ACTIVITY_OPEN] --Vad_CloseWindow--> [UTTERANCE_ENDED]
    [SPEECH_STOPPED] --Vad_IgnoreRedundantStop--> [SPEECH_EDGE_IGNORED]
                        inhibitor(VOICE_ACTIVITY_OPEN)   (already closed: absorb)

An example to copy and adapt, not a shipped subnet: the edge shape is
transport-specific, the VAD discipline is not.
"""

from __future__ import annotations

from adk_libpetri._spec import Action, Ctx, NetSpec, Place, Port, TransitionSpec, one, out
from adk_libpetri.subnet import bind

from . import barge_in_subnet

NAME = "Vad"


class Transitions:
    OPEN_WINDOW = f"{NAME}_OpenWindow"
    IGNORE_REDUNDANT_START = f"{NAME}_IgnoreRedundantStart"
    CLOSE_WINDOW = f"{NAME}_CloseWindow"
    IGNORE_REDUNDANT_STOP = f"{NAME}_IgnoreRedundantStop"


class Places:
    SPEECH_STARTED: Place[None] = Place(f"{NAME}_speechStarted")
    """Caller injects on Live-API ``activityStart`` (speech began)."""

    SPEECH_STOPPED: Place[None] = Place(f"{NAME}_speechStopped")
    """Caller injects on Live-API ``activityEnd`` (speech ended)."""

    UTTERANCE_ENDED: Place[None] = Place(f"{NAME}_utteranceEnded")
    """A real close happened: the downstream "user finished" trigger."""

    SPEECH_EDGE_IGNORED: Place[None] = Place(f"{NAME}_speechEdgeIgnored")
    """Observability: a redundant start/stop edge was absorbed (no state change)."""


VOICE_ACTIVITY_OPEN = barge_in_subnet.Places.VOICE_ACTIVITY_OPEN
"""Reused from barge-in, so the window this subnet opens is the one barge-in reads."""


DEF = NetSpec(
    NAME,
    (
        TransitionSpec(
            Transitions.OPEN_WINDOW,
            (one(Places.SPEECH_STARTED),),
            out(VOICE_ACTIVITY_OPEN),
            inhibitors=(VOICE_ACTIVITY_OPEN,),
        ),
        TransitionSpec(
            Transitions.IGNORE_REDUNDANT_START,
            (one(Places.SPEECH_STARTED),),
            out(Places.SPEECH_EDGE_IGNORED),
            reads=(VOICE_ACTIVITY_OPEN,),
        ),
        TransitionSpec(
            Transitions.CLOSE_WINDOW,
            (one(Places.SPEECH_STOPPED), one(VOICE_ACTIVITY_OPEN)),
            out(Places.UTTERANCE_ENDED),
        ),
        TransitionSpec(
            Transitions.IGNORE_REDUNDANT_STOP,
            (one(Places.SPEECH_STOPPED),),
            out(Places.SPEECH_EDGE_IGNORED),
            inhibitors=(VOICE_ACTIVITY_OPEN,),
        ),
    ),
    ports=(
        Port("speechStarted", "in", Places.SPEECH_STARTED),
        Port("speechStopped", "in", Places.SPEECH_STOPPED),
        Port("voiceActivityOpen", "inout", VOICE_ACTIVITY_OPEN),
        Port("utteranceEnded", "out", Places.UTTERANCE_ENDED),
        Port("speechEdgeIgnored", "out", Places.SPEECH_EDGE_IGNORED),
    ),
)


def _open_window(ctx: Ctx) -> None:
    ctx.input(Places.SPEECH_STARTED)
    ctx.signal(VOICE_ACTIVITY_OPEN)


def _ignore_start(ctx: Ctx) -> None:
    ctx.input(Places.SPEECH_STARTED)
    ctx.signal(Places.SPEECH_EDGE_IGNORED)


def _close_window(ctx: Ctx) -> None:
    ctx.input(Places.SPEECH_STOPPED)
    ctx.input(VOICE_ACTIVITY_OPEN)
    ctx.signal(Places.UTTERANCE_ENDED)


def _ignore_stop(ctx: Ctx) -> None:
    ctx.input(Places.SPEECH_STOPPED)
    ctx.signal(Places.SPEECH_EDGE_IGNORED)


def action_bindings() -> dict[str, Action]:
    return bind(
        DEF,
        {
            Transitions.OPEN_WINDOW: _open_window,
            Transitions.IGNORE_REDUNDANT_START: _ignore_start,
            Transitions.CLOSE_WINDOW: _close_window,
            Transitions.IGNORE_REDUNDANT_STOP: _ignore_stop,
        },
    )
