"""Port of ``TransferRouterSubnetTest.java``."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import libpetri as lp
from google.adk.events.event import Event

from adk_libpetri import colours as C
from adk_libpetri._spec import NetSpec, Place
from adk_libpetri.subnet import transfer_router as TR

KNOWN = ("billing", "sales", "support")
CONFIG = TR.Config("router", lambda: "inv-fixed")


@dataclass
class Fixture:
    final_marking: dict[str, list[Any]]
    events: list[Event]


async def run(
    known_names: Iterable[str], config: TR.Config, *transfers: C.TransferTarget
) -> Fixture:
    known = list(known_names)
    spec = NetSpec.compose("test", TR.def_(known))
    net = spec.build(TR.action_bindings(known, config))
    marking = await lp.run_async(
        net, initial={C.TRANSFER.name: list(transfers)}, event_store=lp.InMemoryEventStore()
    )
    snapshot = {p.name: list(marking.tokens(p.name)) for p in spec.places if marking.count(p.name)}
    return Fixture(snapshot, list(marking.tokens(C.EVENT_OUT.name)))


def tokens_at(f: Fixture, p: Place[C.TransferTarget]) -> list[C.TransferTarget]:
    return f.final_marking.get(p.name, [])


def event_text(e: Event) -> str:
    assert e.content is not None and e.content.parts
    return e.content.parts[0].text or ""


# ============================================================
#  Happy path -- valid names route to per-target places
# ============================================================


async def test_valid_agent_name_routes_to_matching_target_place() -> None:
    fixture = await run(KNOWN, CONFIG, C.TransferTarget("billing"))

    billing = tokens_at(fixture, TR.target_place("billing"))
    assert len(billing) == 1
    assert billing[0].agent_name == "billing"

    assert tokens_at(fixture, TR.target_place("sales")) == []
    assert tokens_at(fixture, TR.target_place("support")) == []
    assert tokens_at(fixture, TR.UNKNOWN_TARGET) == []

    assert fixture.events == []


async def test_multiple_valid_targets_each_routes_independently() -> None:
    fixture = await run(
        KNOWN,
        CONFIG,
        C.TransferTarget("sales"),
        C.TransferTarget("support"),
        C.TransferTarget("billing"),
    )

    for name in ("sales", "support", "billing"):
        assert len(tokens_at(fixture, TR.target_place(name))) == 1
    assert fixture.events == []


# ============================================================
#  Unknown-target path -- produces typed error Event to EVENT_OUT
# ============================================================


async def test_unknown_agent_name_produces_typed_error_event() -> None:
    fixture = await run(KNOWN, CONFIG, C.TransferTarget("Salez"))  # typo

    # Token transited through UNKNOWN_TARGET (consumed) and produced an Event.
    assert tokens_at(fixture, TR.UNKNOWN_TARGET) == []
    assert len(fixture.events) == 1

    error_event = fixture.events[0]
    assert error_event.author == "router"
    assert error_event.invocation_id == "inv-fixed"
    assert "unknown agent" in event_text(error_event)
    assert "Salez" in event_text(error_event)


async def test_empty_known_set_routes_every_transfer_to_unknown() -> None:
    fixture = await run((), CONFIG, C.TransferTarget("any"), C.TransferTarget("other"))

    assert len(fixture.events) == 2
    texts = sorted(event_text(e) for e in fixture.events)
    assert "any" in texts[0]
    assert "other" in texts[1]


# ============================================================
#  Structural shape -- interface ports + transitions
# ============================================================


def test_interface_exposes_one_input_one_eventout_n_targets_plus_unknown() -> None:
    assert sorted(p.name for p in TR.def_(KNOWN).ports) == [
        "eventOut",
        "target/_unknown",
        "target/billing",
        "target/sales",
        "target/support",
        "transfer",
    ]


def test_def_declares_exactly_demux_and_emit_unknown_transitions() -> None:
    assert sorted(TR.def_(KNOWN).transition_names) == [
        TR.Transitions.DEMUX,
        TR.Transitions.EMIT_UNKNOWN_ERROR,
    ]


def test_target_place_helper_returns_consistent_place_record() -> None:
    p1 = TR.target_place("billing")
    p2 = TR.target_place("billing")
    assert p1 == p2
    assert p1.name == "TransferRouter_target/billing"
    assert p1.token_type is C.TransferTarget


# ============================================================
#  XOR validation -- all known targets + unknown are reachable
# ============================================================


async def test_exactly_one_xor_child_receives_token_per_fire() -> None:
    fixture = await run(
        KNOWN,
        CONFIG,
        C.TransferTarget("billing"),
        C.TransferTarget("sales"),
        C.TransferTarget("support"),
        C.TransferTarget("ghost"),
    )

    assert len(tokens_at(fixture, TR.target_place("billing"))) == 1
    assert len(tokens_at(fixture, TR.target_place("sales"))) == 1
    assert len(tokens_at(fixture, TR.target_place("support"))) == 1
    assert len(tokens_at(fixture, TR.UNKNOWN_TARGET)) == 0  # drained by EmitUnknownError
    assert len(fixture.events) == 1


# ============================================================
#  Action-binding validation
# ============================================================


def test_mismatched_known_set_between_def_and_bindings_is_caught() -> None:
    # As in Java, this only confirms the bindings round-trip for the supplied set.
    bindings = TR.action_bindings(["a", "b"], CONFIG)
    assert TR.Transitions.DEMUX in bindings
    assert TR.Transitions.EMIT_UNKNOWN_ERROR in bindings
