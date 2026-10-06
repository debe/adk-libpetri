"""Pattern B: late-join / K-of-N quorum via ``exactly(K, RESULT)``.

Port of ``PatternB_QuorumDemoTest.java``; the ADK-only half is
``test_pattern_b_adk_only_foil.py``.

Five branches start concurrently and each deposits a token on one shared
``RESULT`` place. Synthesis fires the moment the K-th token lands (K=3), driven
by an ``exactly(K, RESULT)`` input arc: K lives in the topology, where the
verifier sees it, not in an action guard. Late results, from the branches that
finish after the quorum closed, drain to ``DISCARDED`` through a
``read(QUORUM_MET)``-gated absorber, where a parent net could observe them.

**Why this exists.** ADK Python 2.11's only join, ``JoinNode``, is all-of-N and has
no cardinality field; a hand-rolled counter in a plain successor synthesizes at the
K-th arrival, but the workflow still waits for all N; and the single-node
``take(K)`` escape ends the turn early by cancelling the late branches, so their
results go nowhere (see the foil). The Java foil's claims hold unchanged.

Topology::

    [USER_IN] --Quorum_Start--> AND(trigger1..5)     reset(RESULT, QUORUM_MET, DISCARDED)
    [trigger_i] --Quorum_RunBranch_i--> [RESULT]
    [RESULT]x3 --Quorum_Synthesize--> AND(EVENT_OUT, QUORUM_MET)
                exactly(3, RESULT), inhibitor(QUORUM_MET), prio +10
    [RESULT] --Quorum_AbsorbLate--> [DISCARDED]       read(QUORUM_MET), prio -10

**At-most-once.** The inhibitor on ``QUORUM_MET`` is not what bounds synthesis; it
reads the pass-start marking (see Pattern A). Counting does: five results per turn
cannot feed two ``exactly(3)`` firings. That holds on the Rust executor too, which
may start an async transition again while an earlier firing is in flight, because a
second firing would still need three more tokens.

**How it runs.** Through ``PetriAgent`` under a stock ``InMemoryRunner``, as Java
does; the runner is then drained so the late branches finish and drain.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import libpetri as lp
import pytest

from adk_libpetri import colours as C
from adk_libpetri import on_loop
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import Action, Ctx, NetSpec, Place, TransitionSpec, and_, exactly, one, out
from support.smt_proofs import assert_each_proven, requires_z3

from ._petri_harness import model_event, orchestrator, run_turn_then_drain, text_of

K = 3
DELAYS = {"b1": 0.03, "b2": 0.06, "b3": 0.09, "b4": 0.40, "b5": 0.80}
FIRST_LATE = sorted(DELAYS.values())[K]
AGENT = "quorum_agent"


@dataclass(frozen=True)
class BranchResult:
    branch_id: str
    text: str


# ============================================================
#  Places and the net (~30 lines of user code, no stock subnet)
# ============================================================

TRIGGERS = {b: Place(f"trigger{b[1:]}") for b in DELAYS}
RESULT = Place("quorumResult", BranchResult)
QUORUM_MET = Place("quorumMet")
DISCARDED = Place("quorumDiscarded", BranchResult)

SPEC = NetSpec(
    "kofn-quorum",
    (
        TransitionSpec(
            "Quorum_Start",
            (one(C.USER_IN),),
            and_(*TRIGGERS.values()),
            resets=(RESULT, QUORUM_MET, DISCARDED),
        ),
        *(
            TransitionSpec(f"Quorum_RunBranch_{b[1:]}", (one(t),), out(RESULT))
            for b, t in TRIGGERS.items()
        ),
        # The load-bearing transition: K lives in the input arc, not in an action guard.
        TransitionSpec(
            "Quorum_Synthesize",
            (exactly(K, RESULT),),
            and_(C.EVENT_OUT, QUORUM_MET),
            inhibitors=(QUORUM_MET,),
            priority=10,
        ),
        # Late absorber: drains the RESULT tokens that arrive after the quorum.
        TransitionSpec(
            "Quorum_AbsorbLate",
            (one(RESULT),),
            out(DISCARDED),
            reads=(QUORUM_MET,),
            priority=-10,
        ),
    ),
)


# ============================================================
#  Actions
# ============================================================


def quorum_actions(delays: dict[str, float]) -> dict[str, Action]:
    def start(ctx: Ctx) -> None:
        ctx.input(C.USER_IN)
        for t in TRIGGERS.values():
            ctx.signal(t)

    def run_branch(b: str) -> Action:
        async def run(ctx: Ctx) -> None:
            ctx.input(TRIGGERS[b])
            await on_loop(asyncio.sleep(delays[b]))  # the stand-in for an LLM call
            ctx.output(RESULT, BranchResult(b, f"answer from {b}"))

        return run

    def synthesize(ctx: Ctx) -> None:
        # The arc hands over exactly K results; the action only shapes them.
        winners = ctx.inputs(RESULT)
        summary = "synth(" + ",".join(r.branch_id for r in winners) + ")"
        ctx.output(C.EVENT_OUT, model_event(AGENT, "quorum-synth", summary))
        ctx.signal(QUORUM_MET)

    def absorb_late(ctx: Ctx) -> None:
        ctx.output(DISCARDED, ctx.input(RESULT))

    return {
        "Quorum_Start": start,
        **{f"Quorum_RunBranch_{b[1:]}": run_branch(b) for b in TRIGGERS},
        "Quorum_Synthesize": synthesize,
        "Quorum_AbsorbLate": absorb_late,
    }


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    yield from orchestrator("pattern-b-orchestrator")


# ============================================================
#  Behaviour
# ============================================================


async def test_synthesis_fires_at_kth_result_without_waiting_for_slower_branches(
    orch: OrchestratorLoop,
) -> None:
    run = await run_turn_then_drain(
        orch,
        SPEC,
        quorum_actions(DELAYS),
        agent_name=AGENT,
        description="K-of-N quorum across five branches",
        text="synthesize a consensus answer",
    )

    # One synthesis, from the three FASTEST branches in completion order.
    [event] = run.authored_by(AGENT)
    assert text_of(event) == "synth(b1,b2,b3)"
    # The load-bearing assertion: the turn ends well before the 4th branch (a
    # barrier join, as in the foil, would take the slowest branch's 0.8s).
    assert run.elapsed < FIRST_LATE, f"turn took {run.elapsed:.3f}s; quorum means < {FIRST_LATE}s"

    # Drained after b4 and b5 finished: still one synthesis, and the late results
    # are kept on DISCARDED rather than lost.
    assert [text_of(e) for e in run.egress] == ["synth(b1,b2,b3)"]
    assert run.final.count(QUORUM_MET.name) == 1
    assert run.final.count(RESULT.name) == 0
    assert [r.branch_id for r in run.final.tokens(DISCARDED.name)] == ["b4", "b5"]


async def test_all_results_ready_in_one_pass_synthesize_once() -> None:
    # Every result already on RESULT: one synthesis takes K, the rest drain.
    initial = {RESULT.name: [BranchResult(b, f"answer from {b}") for b in DELAYS]}

    final = await lp.run_async(SPEC.build(quorum_actions(DELAYS)), initial=initial)

    assert final.count(C.EVENT_OUT.name) == 1
    assert final.count(QUORUM_MET.name) == 1
    assert final.count(DISCARDED.name) == len(DELAYS) - K
    assert final.count(RESULT.name) == 0


# ============================================================
#  Proofs (safety; the early fire above is empirical)
# ============================================================


@requires_z3
def test_quorum_net_proves_exactly_one_synthesis_per_turn() -> None:
    # Bound net, as it runs (actions are never invoked; only structure is encoded).
    assert_each_proven(
        SPEC.build(quorum_actions(DELAYS)),
        {
            "one synthesis per turn: place_bound(QUORUM_MET, 1)": lp.place_bound(
                QUORUM_MET.name, 1
            ),
            "one egress event per turn: place_bound(EVENT_OUT, 1)": lp.place_bound(
                C.EVENT_OUT.name, 1
            ),
            "deadlock_free": lp.deadlock_free(),
        },
        initial_marking={C.USER_IN.name: 1},
        # RESULT may hold the N-K leftovers until the absorber drains them; as a sink
        # the deadlock check ignores that natural post-quorum tail.
        sink_places=[C.EVENT_OUT.name, QUORUM_MET.name, DISCARDED.name, RESULT.name],
    )
