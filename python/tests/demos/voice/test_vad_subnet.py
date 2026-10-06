"""Port of Java ``VadSubnetTest``: idempotent speech edges, and the window barge-in reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import libpetri as lp

from adk_libpetri._spec import NetSpec, Place
from adk_libpetri.subnet import merge

from . import barge_in_subnet as BI
from . import vad_subnet as VAD

P = VAD.Places


def vad_net() -> lp.BuiltNet:
    return NetSpec.compose("vad", VAD.DEF).build(VAD.action_bindings())


def vad_barge_in_net() -> lp.BuiltNet:
    # Composition fuses VOICE_ACTIVITY_OPEN by (name, type): the window
    # VadSubnet opens is the one BargeInSubnet reads.
    spec = NetSpec.compose("vad+bargein", VAD.DEF, BI.DEF)
    return spec.build(merge(VAD.action_bindings(), BI.action_bindings()))


def seed(place: Place[None], n: int = 1) -> dict[str, list[None]]:
    return {place.name: [None] * n}


def run(net: lp.BuiltNet, initial: Mapping[str, Sequence[Any]]) -> lp.MarkingView:
    return lp.run_sync(net, initial=dict(initial), event_store=lp.InMemoryEventStore())


def test_speech_start_when_closed_opens_the_window() -> None:
    m = run(vad_net(), seed(P.SPEECH_STARTED))

    assert m.count(VAD.VOICE_ACTIVITY_OPEN.name) == 1
    assert m.count(P.SPEECH_EDGE_IGNORED.name) == 0


def test_redundant_speech_start_while_open_is_absorbed() -> None:
    initial = seed(P.SPEECH_STARTED) | seed(VAD.VOICE_ACTIVITY_OPEN)

    m = run(vad_net(), initial)

    # The window count stays exactly 1 (read, not re-produced); the edge is logged.
    assert m.count(VAD.VOICE_ACTIVITY_OPEN.name) == 1
    assert m.count(P.SPEECH_EDGE_IGNORED.name) == 1


def test_speech_stop_while_open_closes_the_window_and_ends_the_utterance() -> None:
    initial = seed(P.SPEECH_STOPPED) | seed(VAD.VOICE_ACTIVITY_OPEN)

    m = run(vad_net(), initial)

    assert m.count(VAD.VOICE_ACTIVITY_OPEN.name) == 0
    assert m.count(P.UTTERANCE_ENDED.name) == 1


def test_redundant_speech_stop_while_closed_is_absorbed() -> None:
    m = run(vad_net(), seed(P.SPEECH_STOPPED))

    assert m.count(P.UTTERANCE_ENDED.name) == 0
    assert m.count(P.SPEECH_EDGE_IGNORED.name) == 1


def test_window_opened_by_vad_routes_a_subsequent_interrupt_to_barge_in() -> None:
    net = vad_barge_in_net()

    # Phase 1: speech starts, VadSubnet opens the window.
    after_speech = run(net, seed(P.SPEECH_STARTED))
    assert after_speech.count(VAD.VOICE_ACTIVITY_OPEN.name) == 1

    # Phase 2: carry that exact window state forward, plus a barge-in interrupt.
    phase2 = {
        VAD.VOICE_ACTIVITY_OPEN.name: list(after_speech.tokens(VAD.VOICE_ACTIVITY_OPEN.name)),
        **seed(BI.Places.INTERRUPTED),
    }
    after_interrupt = run(net, phase2)
    assert after_interrupt.count(BI.Places.BARGE_IN_SENT.name) == 1
    assert after_interrupt.count(BI.Places.INTERRUPT_DISCARDED.name) == 0


def test_the_window_place_is_barge_ins_by_identity() -> None:
    # Java reuses the Place object; here identity is (name, type), which fuses.
    assert VAD.VOICE_ACTIVITY_OPEN == BI.Places.VOICE_ACTIVITY_OPEN
    spec = NetSpec.compose("vad+bargein", VAD.DEF, BI.DEF)
    assert [p.name for p in spec.places].count(VAD.VOICE_ACTIVITY_OPEN.name) == 1
