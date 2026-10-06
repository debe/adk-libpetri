"""Z3 proofs for a compiled workflow (``@experimental``)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import libpetri as lp

from .. import colours as C
from .._experimental import experimental
from .._spec import NetSpec
from .compiler import TERMINAL, TURN_ACTIVE, CompiledWorkflow


@dataclass(frozen=True)
class WorkflowProof:
    label: str
    result: lp.VerificationResult

    @property
    def proven(self) -> bool:
        return self.result.is_proven()


@experimental
def workflow_properties(cw: CompiledWorkflow) -> dict[str, lp.SmtProperty]:
    """The safety claims every compiled workflow should satisfy."""
    props: dict[str, lp.SmtProperty] = {
        "one turn at a time: place_bound(turnActive, 1)": lp.place_bound(TURN_ACTIVE.name, 1),
        "permit never doubles: place_bound(turnPermit, 1)": lp.place_bound(C.TURN_PERMIT.name, 1),
        "at most one terminal output: place_bound(terminalOutput, 1)": lp.place_bound(
            TERMINAL.name, 1
        ),
    }
    for n in cw.node_names:
        props[f"{n} runs serially: place_bound({n}/idle, 1)"] = lp.place_bound(
            cw.idle_place(n).name, 1
        )
    unmatched = [p.name for p in cw.unmatched_places()]
    if unmatched:
        props[f"no route goes unmatched: unreachable({unmatched})"] = lp.unreachable(unmatched)
    return props


def _options(cw: CompiledWorkflow, k: int, exact: bool) -> dict[str, Any]:
    return {
        "initial_marking": cw.initial_counts(),
        "environment_places": [C.USER_IN.name],
        "environment_mode": lp.arrivals(k, k) if exact else lp.arrivals(k),
    }


@experimental
def verify_workflow(
    cw: CompiledWorkflow, k: int = 1, *, deadlock: bool = True, **verify_options: Any
) -> list[WorkflowProof]:
    """One ``verify()`` per property; read ``proven`` (``unknown`` is not a proof).

    Under ``arrivals(k)`` user inputs. Deadlock freedom (with the permit and
    the egress as the only sinks) is checked under exactly ``k`` arrivals.
    """
    spec = cw.spec
    suffix = ""
    if any(t.match is not None for t in spec.transitions):
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
        deadlock = False
    net = spec.build(cw.actions(_StructuralScope()))  # type: ignore[arg-type]
    proofs = [
        WorkflowProof(
            label + suffix, lp.verify(net, prop, **_options(cw, k, False), **verify_options)
        )
        for label, prop in workflow_properties(cw).items()
    ]
    if deadlock:
        sinks = [
            C.EVENT_OUT.name,
            C.TURN_PERMIT.name,
            "wf/quiet",
            *(cw.idle_place(n).name for n in cw.node_names),
        ]
        if cw.max_concurrency:
            sinks.append("wf/concurrency")
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
            )
        )
    return proofs


class _StructuralScope:
    """Never consulted: verification encodes structure only."""

    ctx = None
    loop = None
