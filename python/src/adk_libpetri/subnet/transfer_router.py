"""Demultiplex ``TransferTarget`` tokens over compile-time-known per-agent places.

    [TRANSFER] --TransferRouter_Demux--> xor([target/A], [target/B], ..., [target/_unknown])
    [target/_unknown] --TransferRouter_EmitUnknownError--> [EVENT_OUT]

ADK resolves a ``transfer_to_agent`` name with ``root_agent.find_agent(...)``
at runtime; a hallucinated name finds nothing. Here the set of reachable
destinations is part of the net topology (and its diagram): a known name lands
on its own place, anything else on ``_unknown``, which emits a typed error
``Event``. No runtime tree walk.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from google.adk.events.event import Event
from google.genai import types

from .. import colours as C
from .._spec import Action, Ctx, NetSpec, Place, Port, TransitionSpec, one, out, xor
from ._common import IdSupplier, random_id
from .actions import bind

NAME = "TransferRouter"
TARGET_PLACE_PREFIX = f"{NAME}_target/"
UNKNOWN_TARGET: Place[C.TransferTarget] = Place(f"{TARGET_PLACE_PREFIX}_unknown", C.TransferTarget)


class Transitions:
    DEMUX = f"{NAME}_Demux"
    EMIT_UNKNOWN_ERROR = f"{NAME}_EmitUnknownError"


def target_place(agent_name: str) -> Place[C.TransferTarget]:
    """The place a known agent's transfers land on; compose a downstream agent against it."""
    return Place(f"{TARGET_PLACE_PREFIX}{agent_name}", C.TransferTarget)


@dataclass(frozen=True)
class Config:
    author: str
    invocation_id_supplier: IdSupplier = field(default=random_id)


def _ordered(names: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(names))


def def_(known_agent_names: Iterable[str]) -> NetSpec:
    """The subnet for ``known_agent_names`` (order kept for deterministic topology)."""
    names = _ordered(known_agent_names)
    targets = [target_place(n) for n in names]
    demux = TransitionSpec(Transitions.DEMUX, (one(C.TRANSFER),), xor(*targets, UNKNOWN_TARGET))
    emit_unknown = TransitionSpec(
        Transitions.EMIT_UNKNOWN_ERROR, (one(UNKNOWN_TARGET),), out(C.EVENT_OUT)
    )
    ports = [Port("transfer", "in", C.TRANSFER), Port("eventOut", "out", C.EVENT_OUT)]
    ports += [Port(f"target/{n}", "out", p) for n, p in zip(names, targets, strict=True)]
    ports.append(Port("target/_unknown", "out", UNKNOWN_TARGET))
    return NetSpec(NAME, (demux, emit_unknown), ports=tuple(ports))


def action_bindings(known_agent_names: Iterable[str], config: Config) -> dict[str, Action]:
    names = _ordered(known_agent_names)
    lookup = {n: target_place(n) for n in names}

    def demux(ctx: Ctx) -> None:
        target = ctx.input(C.TRANSFER)
        ctx.output(lookup.get(target.agent_name, UNKNOWN_TARGET), target)

    def emit_unknown(ctx: Ctx) -> None:
        bad = ctx.input(UNKNOWN_TARGET)
        ctx.output(
            C.EVENT_OUT,
            Event(
                invocation_id=config.invocation_id_supplier(),
                author=config.author,
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(text=f"Cannot transfer to unknown agent: '{bad.agent_name}'")
                    ],
                ),
            ),
        )

    return bind(
        def_(names), {Transitions.DEMUX: demux, Transitions.EMIT_UNKNOWN_ERROR: emit_unknown}
    )
