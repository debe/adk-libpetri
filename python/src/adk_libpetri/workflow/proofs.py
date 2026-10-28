"""Z3 proofs for a compiled workflow (``@experimental``)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

import libpetri as lp

from .. import colours as C
from .._experimental import experimental
from .._spec import NetSpec
from .compiler import CONFLICT, TURN_ACTIVE, CompiledWorkflow, TurnScope

Kind = Literal["safety", "deadlock", "route coverage"]


DEFAULT_K = 1
"""User inputs the proofs assume when ``k`` is ``None``."""


@experimental
@dataclass(frozen=True)
class WorkflowProof:
    """One claim's verdict, with :class:`~adk_libpetri.net.NetProof`'s surface.

    Read :attr:`proven`: ``unknown`` is not a proof.
    """

    label: str
    result: lp.VerificationResult
    kind: Kind = "safety"
    """``route coverage`` is a lint, not a safety claim: a violation means the
    net cannot rule out a route with no edge, which ADK treats as the end of
    that branch (ADK's own loop samples exit that way)."""
    scope: str = ""
    """What the proof assumed arrives from outside, e.g. ``userIn: at most 1
    arrival(s)``."""
    notes: tuple[str, ...] = ()
    """Assumptions the verdict rests on (the match-free over-approximation)."""

    @property
    def proven(self) -> bool:
        return self.result.is_proven()

    @property
    def violated(self) -> bool:
        return self.result.is_violated()


@experimental
def workflow_properties(cw: CompiledWorkflow) -> dict[str, lp.SmtProperty]:
    """The safety claims every compiled workflow should satisfy."""
    props: dict[str, lp.SmtProperty] = {
        "one turn at a time: place_bound(turnActive, 1)": lp.place_bound(TURN_ACTIVE.name, 1),
        "permit never doubles: place_bound(turnPermit, 1)": lp.place_bound(C.TURN_PERMIT.name, 1),
    }
    terminals = cw.terminal_nodes
    for n in terminals:
        props[f"{n} keeps one output: place_bound({n}/terminalOutput, 1)"] = lp.place_bound(
            cw.terminal_place(n).name, 1
        )
    if len(terminals) > 1:
        label = "at most one terminal node outputs (ADK raises otherwise)"
        props[f"{label}: unreachable(terminalConflict)"] = lp.unreachable([CONFLICT.name])
    for n in cw.node_names:
        props[f"{n} runs serially: place_bound({n}/idle, 1)"] = lp.place_bound(
            cw.idle_place(n).name, 1
        )
    return props


@experimental
def route_coverage(cw: CompiledWorkflow) -> dict[str, lp.SmtProperty]:
    """The lint: no node ends its branch on a route with no edge."""
    unmatched = [p.name for p in cw.unmatched_places()]
    if not unmatched:
        return {}
    return {f"route coverage: unreachable({unmatched})": lp.unreachable(unmatched)}


def _interrupt_matches(spec: NetSpec) -> bool:
    return any(t.match is not None for t in spec.transitions)


def claim_count(cw: CompiledWorkflow) -> int:
    """How many proofs :func:`verify_workflow` returns for ``cw`` (with ``deadlock``)."""
    deadlock = not _interrupt_matches(cw.verification_spec)
    return len(workflow_properties(cw)) + len(route_coverage(cw)) + int(deadlock)


def _options(cw: CompiledWorkflow, k: int, exact: bool) -> dict[str, Any]:
    return {
        "initial_marking": cw.initial_counts(),
        "environment_places": [C.USER_IN.name],
        "environment_mode": lp.arrivals(k, k) if exact else lp.arrivals(k),
    }


@experimental
def verify_workflow(
    cw: CompiledWorkflow, k: int | None = None, *, deadlock: bool = True, **verify_options: Any
) -> list[WorkflowProof]:
    """One ``verify()`` per property; read ``proven`` (``unknown`` is not a proof).

    Under ``arrivals(k)`` user inputs (``None``: :data:`DEFAULT_K`, one). Deadlock
    freedom (with the permit and the egress as the only sinks) is checked under
    exactly ``k`` arrivals. The proofs run on
    :attr:`CompiledWorkflow.verification_spec`, where each retry loop is folded
    into its run; they hold on the executed net.
    """
    k = DEFAULT_K if k is None else k
    if k < 1:
        raise ValueError(f"k is a number of user inputs, at least 1; got {k}")
    spec = cw.verification_spec
    suffix = ""
    notes: tuple[str, ...] = ()
    if _interrupt_matches(spec):
        # Interrupt ids arrive from the environment (unbounded ν-names, NU-040).
        # Safety is proved on the net with the ν-match guards dropped: that only
        # adds behaviours, so a bound or unreachability proved there holds on the
        # real net. Deadlock freedom does not transfer and is not claimed.
        spec = NetSpec(
            spec.name,
            tuple(replace(t, match=None) for t in spec.transitions),
            spec.extra_places,
            spec.ports,
            spec.membership,
        )
        suffix = " [on the match-free over-approximation]"
        notes = (
            "proved on the net with the interrupt-id match guards dropped, which only "
            "adds behaviours; deadlock freedom is not claimed",
        )
        deadlock = False
    acts = cw.actions(_StructuralScope())
    net = spec.build({t: a for t, a in acts.items() if t in spec.transition_names})
    proofs: list[WorkflowProof] = []
    claims: tuple[tuple[Kind, dict[str, lp.SmtProperty]], ...] = (
        ("safety", workflow_properties(cw)),
        ("route coverage", route_coverage(cw)),
    )
    for kind, props in claims:
        for label, prop in props.items():
            result = lp.verify(net, prop, **_options(cw, k, False), **verify_options)
            proofs.append(WorkflowProof(label + suffix, result, kind, _scope(k, False), notes))
    if deadlock:
        sinks = [
            C.EVENT_OUT.name,
            C.TURN_PERMIT.name,
            "wf/quiet",
            *(cw.idle_place(n).name for n in cw.node_names),
        ]
        if cw.max_concurrency:
            sinks.append("wf/concurrency")
        if len(cw.terminal_nodes) > 1:
            sinks.append(CONFLICT.name)
        proofs.append(
            WorkflowProof(
                "deadlock_free",
                lp.verify(
                    net,
                    lp.deadlock_free(),
                    **_options(cw, k, True),
                    sink_places=sinks,
                    **verify_options,
                ),
                "deadlock",
                _scope(k, True),
            )
        )
    return proofs


def _scope(k: int, exact: bool) -> str:
    how = "exactly" if exact else "at most"
    return f"{C.USER_IN.name}: {how} {k} arrival(s)"


class _StructuralScope(TurnScope):
    """Never consulted: verification encodes structure only."""
