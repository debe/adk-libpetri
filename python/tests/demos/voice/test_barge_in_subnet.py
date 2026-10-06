"""Port of Java ``BargeInSubnetTest``: one interrupt, one structural decision."""

from __future__ import annotations

import libpetri as lp

from adk_libpetri._spec import NetSpec

from . import barge_in_subnet as BI

P = BI.Places


def run(voice_open: bool, interrupt_count: int) -> lp.MarkingView:
    net = NetSpec.compose("test", BI.DEF).build(BI.action_bindings())
    initial: dict[str, list[None]] = {P.INTERRUPTED.name: [None] * interrupt_count}
    if voice_open:
        initial[P.VOICE_ACTIVITY_OPEN.name] = [None]
    return lp.run_sync(net, initial=initial, event_store=lp.InMemoryEventStore())


def test_voice_open_routes_interrupt_to_send_barge_in() -> None:
    m = run(voice_open=True, interrupt_count=1)

    assert m.count(P.BARGE_IN_SENT.name) == 1
    assert m.count(P.INTERRUPT_DISCARDED.name) == 0


def test_voice_closed_routes_interrupt_to_discard() -> None:
    m = run(voice_open=False, interrupt_count=1)

    assert m.count(P.BARGE_IN_SENT.name) == 0
    assert m.count(P.INTERRUPT_DISCARDED.name) == 1


def test_multiple_interrupts_each_routed_independently() -> None:
    m = run(voice_open=True, interrupt_count=3)

    # VOICE_ACTIVITY_OPEN is read, not consumed: all three interrupts see it.
    assert m.count(P.BARGE_IN_SENT.name) == 3
    assert m.count(P.INTERRUPT_DISCARDED.name) == 0
    assert m.count(P.VOICE_ACTIVITY_OPEN.name) == 1


def test_interface_exposes_four_ports() -> None:
    assert sorted(p.name for p in BI.DEF.ports) == [
        "bargeInSent",
        "interruptDiscarded",
        "interrupted",
        "voiceActivityOpen",
    ]
    # And libpetri accepts them as a SubnetDef interface.
    BI.DEF.subnet_def(BI.action_bindings())


def test_def_declares_exactly_send_and_discard_transitions() -> None:
    assert sorted(BI.DEF.transition_names) == [
        BI.Transitions.DISCARD_INTERRUPT,
        BI.Transitions.SEND_BARGE_IN,
    ]
    assert BI.Transitions.SEND_BARGE_IN == "BargeIn_SendBargeIn"
    assert BI.Transitions.DISCARD_INTERRUPT == "BargeIn_DiscardInterrupt"
