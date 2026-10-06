"""Structural and SMT invariants for adk-libpetri nets (Java ``AdkNetInvariants``).

Structural checks (no Z3) walk a :class:`~adk_libpetri._spec.NetSpec` and
return a list of :class:`Violation` (empty: the invariant holds). They catch
the three bug classes the design names as structurally absent:

1. **legacy-session-write race** -- at most one transition consumes
   ``LEGACY_SESSION_WRITE`` (:func:`single_legacy_session_writer`);
2. **hallucinated transfer target** -- a transfer demux's ``_unknown`` place
   has a consumer (:func:`transfer_demux_has_unknown_fallback`);
3. **fire after end-invocation** -- every advancing transition is inhibited
   by ``END_INVOCATION`` (:func:`end_invocation_inhibits_all`).

The SMT factories wrap ``libpetri.place_bound`` under names that say what the
bound is for. State budget bounds in seeds: libpetri models an N-permit seed
as one token, so ``max_tokens = 1`` is the claim that matters.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import libpetri as lp

from .. import colours as C
from .._spec import NetSpec, Place
from ..subnet import transfer_router


@dataclass(frozen=True, slots=True)
class Violation:
    invariant: str
    message: str


def _consumers(net: NetSpec, p: Place[object]) -> list[str]:
    return [t.name for t in net.transitions if any(i.place.name == p.name for i in t.inputs)]


def single_legacy_session_writer(net: NetSpec) -> list[Violation]:
    consumers = _consumers(net, C.LEGACY_SESSION_WRITE)
    if len(consumers) > 1:
        return [
            Violation(
                "singleLegacySessionWriter",
                "Expected at most one transition consuming from LEGACY_SESSION_WRITE; "
                f"found {len(consumers)}: {consumers}",
            )
        ]
    return []


def end_invocation_inhibits_all(net: NetSpec, advancing_names: Iterable[str]) -> list[Violation]:
    """Every transition in ``advancing_names`` must carry ``inhibitor(END_INVOCATION)``.

    These inhibitors end one invocation and leave the session's runner serving
    the next; a terminal place would end the whole session run instead.
    """
    advancing = set(advancing_names)
    missing = [
        t.name
        for t in net.transitions
        if t.name in advancing and all(p.name != C.END_INVOCATION.name for p in t.inhibitors)
    ]
    unknown = sorted(advancing - set(net.transition_names))
    issues = []
    if missing:
        issues.append(
            Violation(
                "endInvocationInhibitsAll",
                f"Advancing transitions missing inhibitor(END_INVOCATION): {missing}",
            )
        )
    if unknown:
        issues.append(
            Violation(
                "endInvocationInhibitsAll",
                f"Declared advancing transitions not in net: {unknown}",
            )
        )
    return issues


def transfer_demux_has_unknown_fallback(net: NetSpec) -> list[Violation]:
    if not net.has_place(transfer_router.UNKNOWN_TARGET):
        return []
    if not _consumers(net, transfer_router.UNKNOWN_TARGET):
        return [
            Violation(
                "transferDemuxHasUnknownFallback",
                "TransferRouterSubnet's UNKNOWN_TARGET place has no consumer — "
                "hallucinated agent names will accumulate as dead-letters",
            )
        ]
    return []


def budget_place_bounded(budget_place: Place[object], max_tokens: int) -> lp.SmtProperty:
    """``budget_place`` never holds more than ``max_tokens`` (state it in seeds)."""
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be >= 1, got: {max_tokens}")
    return lp.place_bound(budget_place.name, max_tokens)


def event_out_bounded(max_buffered: int) -> lp.SmtProperty:
    """``EVENT_OUT`` never buffers more than ``max_buffered`` events at once."""
    if max_buffered < 1:
        raise ValueError(f"max_buffered must be >= 1, got: {max_buffered}")
    return lp.place_bound(C.EVENT_OUT.name, max_buffered)
