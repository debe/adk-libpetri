"""Pattern A: speculative race with an at-most-once commit via a consumed ``RACE_PERMIT``.

Port of ``PatternA_SpeculativeRaceDemoTest.java``; the ADK-only half is
``test_pattern_a_adk_only_foil.py``.

Three branches start concurrently on one ``USER_IN``. The first result to land
commits to ``EVENT_OUT``. Committing consumes the turn's single ``RACE_PERMIT``,
so no other commit is enabled, and marks ``RACE_WON``, which opens the losers'
discard path. The net does not cancel the losers (libpetri has no action
cancellation): their actions run to completion and their results drain to
``RACE_DISCARDED``. They cannot commit.

**Why this exists.** In ADK Python 2.11 a ``Workflow`` has no first-wins join, a
plain successor commits once per branch, and nothing cancels the slow branches
(see the foil). The deprecated ``ParallelAgent`` *does* end at the first escalation
and cancel the other branches; that is first-escalation-wins, which a branch has to
opt into, and nothing proves the commit happens at most once. This net states the
race in topology and Z3 proves the bound.

Topology::

    [USER_IN] --Race_Start--> AND(triggerA, triggerB, triggerC, RACE_PERMIT)
                              reset(RACE_PERMIT, RACE_WON, trigger*, branch*Done, RACE_DISCARDED)
    [triggerX] --Race_RunBranchX--> [branchXDone]                    inhibitor(RACE_WON)
    [branchXDone] + [RACE_PERMIT] --Race_CommitX--> AND(EVENT_OUT, RACE_WON)    prio +10
    [branchXDone] --Race_DiscardX--> [RACE_DISCARDED]        read(RACE_WON), prio -10

**Why a permit, not an inhibitor on RACE_WON.** An inhibitor reads the marking at
the start of an orchestrator pass and a commit's ``RACE_WON`` lands at its end, so
results ready in the same pass all commit. That holds on the Rust executor too:
``test_inhibitor_encoding_commits_every_result_ready_in_one_pass`` locks it in (all
three commit). A consumed permit has no such window.

**At-most-once on the Rust executor.** It may start an async transition again while
an earlier firing of it is still in flight (Java never does). That does not touch
the bound: each branch has one trigger per turn, and every commit needs the one
permit, whatever runs concurrently. The commit actions are synchronous anyway.

**How it runs.** The behaviour test goes through ``PetriAgent`` under a stock
``InMemoryRunner``, as Java does. The one-pass replay drives the net directly with
``lp.run_async``, since ADK adds nothing to a replayed marking.
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
from adk_libpetri._spec import Action, Ctx, NetSpec, Place, TransitionSpec, and_, one, out
from support.smt_proofs import assert_each_proven, requires_z3

from ._petri_harness import model_event, orchestrator, run_turn_then_drain, text_of

FAST, MEDIUM, SLOW = 0.02, 0.12, 0.30
AGENT = "race_agent"


@dataclass(frozen=True)
class BranchResult:
    branch_id: str
    text: str


# ============================================================
#  Places and the net (~45 lines of user code, no stock subnet)
# ============================================================

BRANCHES = ("A", "B", "C")
TRIGGER = {x: Place(f"trigger{x}") for x in BRANCHES}
DONE = {x: Place(f"branch{x}Done", BranchResult) for x in BRANCHES}
RACE_PERMIT = Place("racePermit")
RACE_WON = Place("raceWon")
RACE_DISCARDED = Place("raceDiscarded", BranchResult)


def race_spec(*, inhibitor_encoding: bool = False) -> NetSpec:
    """The race. ``inhibitor_encoding`` swaps the permit for ``inhibitor(RACE_WON)``
    on every commit: the broken encoding, kept only to show why it is broken."""
    ts = [
        TransitionSpec(
            "Race_Start",
            (one(C.USER_IN),),
            and_(*TRIGGER.values(), RACE_PERMIT),
            # Wipe whatever the prior turn left: winner marker, cancelled losers'
            # triggers, undrained results, an unspent permit.
            resets=(RACE_PERMIT, RACE_WON, *TRIGGER.values(), *DONE.values(), RACE_DISCARDED),
        )
    ]
    for x in BRANCHES:
        commit_inputs = (one(DONE[x]),) if inhibitor_encoding else (one(DONE[x]), one(RACE_PERMIT))
        ts += [
            # Don't even start if the race is already decided.
            TransitionSpec(
                f"Race_RunBranch{x}", (one(TRIGGER[x]),), out(DONE[x]), inhibitors=(RACE_WON,)
            ),
            TransitionSpec(
                f"Race_Commit{x}",
                commit_inputs,
                and_(C.EVENT_OUT, RACE_WON),
                inhibitors=(RACE_WON,) if inhibitor_encoding else (),
                priority=10,  # beat discard if both are enabled
            ),
            TransitionSpec(
                f"Race_Discard{x}",
                (one(DONE[x]),),
                out(RACE_DISCARDED),
                reads=(RACE_WON,),  # only drain once the race is over
                priority=-10,
            ),
        ]
    return NetSpec("speculative-race", tuple(ts))


SPEC = race_spec()


# ============================================================
#  Actions (closures over per-branch identity)
# ============================================================

NAMES = {"A": "fast", "B": "medium", "C": "slow"}


def race_actions(
    delays: dict[str, float], finished: list[str] | None = None, *, spec: NetSpec = SPEC
) -> dict[str, Action]:
    def start(ctx: Ctx) -> None:
        ctx.input(C.USER_IN)
        for t in TRIGGER.values():
            ctx.signal(t)
        ctx.signal(RACE_PERMIT)

    def run_branch(x: str) -> Action:
        async def run(ctx: Ctx) -> None:
            ctx.input(TRIGGER[x])
            await on_loop(asyncio.sleep(delays[x]))  # the stand-in for an LLM call
            if finished is not None:
                finished.append(NAMES[x])
            ctx.output(DONE[x], BranchResult(NAMES[x], f"answer from {NAMES[x]}"))

        return run

    def commit(x: str) -> Action:
        takes_permit = one(RACE_PERMIT) in spec.transition(f"Race_Commit{x}").inputs

        def fire(ctx: Ctx) -> None:
            result = ctx.input(DONE[x])
            if takes_permit:
                ctx.input(RACE_PERMIT)
            ctx.output(C.EVENT_OUT, model_event(AGENT, f"race-{result.branch_id}", result.text))
            ctx.signal(RACE_WON)

        return fire

    def discard(x: str) -> Action:
        return lambda ctx: ctx.output(RACE_DISCARDED, ctx.input(DONE[x]))

    actions: dict[str, Action] = {"Race_Start": start}
    for x in BRANCHES:
        actions[f"Race_RunBranch{x}"] = run_branch(x)
        actions[f"Race_Commit{x}"] = commit(x)
        actions[f"Race_Discard{x}"] = discard(x)
    return actions


DELAYS = {"A": FAST, "B": MEDIUM, "C": SLOW}


def all_results_ready() -> dict[str, list[object]]:
    return {DONE[x].name: [BranchResult(NAMES[x], f"answer from {NAMES[x]}")] for x in BRANCHES}


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    yield from orchestrator("pattern-a-orchestrator")


# ============================================================
#  Behaviour
# ============================================================


async def test_fastest_branch_commits_first_and_losers_drain_to_discard(
    orch: OrchestratorLoop,
) -> None:
    finished: list[str] = []
    run = await run_turn_then_drain(
        orch,
        SPEC,
        race_actions(DELAYS, finished),
        agent_name=AGENT,
        description="Speculative race across three branches",
        text="which branch wins?",
    )

    # The turn is the fast branch's answer, and it ends before the slow branch does.
    [event] = run.authored_by(AGENT)
    assert text_of(event) == "answer from fast"
    assert run.elapsed < SLOW, f"turn took {run.elapsed:.3f}s; first-wins means < {SLOW}s"

    # Over the runner's whole life, drained after the losers finished: one commit.
    assert [text_of(e) for e in run.egress] == ["answer from fast"]
    assert run.final.count(RACE_WON.name) == 1
    assert run.final.count(RACE_PERMIT.name) == 0
    # The losers were not cancelled; they ran out and their results were dropped.
    assert finished == ["fast", "medium", "slow"]
    assert sorted(r.branch_id for r in run.final.tokens(RACE_DISCARDED.name)) == ["medium", "slow"]


async def test_all_results_ready_in_one_pass_commit_exactly_once() -> None:
    # The marking that made the inhibitor encoding commit more than once: every branch
    # result ready in the same pass. The permit admits exactly one commit.
    initial = all_results_ready()
    initial[RACE_PERMIT.name] = [None]

    final = await lp.run_async(SPEC.build(race_actions(DELAYS)), initial=initial)

    assert final.count(RACE_WON.name) == 1
    assert final.count(C.EVENT_OUT.name) == 1
    assert final.count(RACE_DISCARDED.name) == 2
    assert final.count(RACE_PERMIT.name) == 0


async def test_inhibitor_encoding_commits_every_result_ready_in_one_pass() -> None:
    # Why the permit is load-bearing, on this executor too: inhibitor(RACE_WON) reads
    # the pass-start marking, so every result ready in the pass commits.
    spec = race_spec(inhibitor_encoding=True)
    initial = all_results_ready()

    final = await lp.run_async(spec.build(race_actions(DELAYS, spec=spec)), initial=initial)

    assert final.count(C.EVENT_OUT.name) == len(BRANCHES)
    assert final.count(RACE_WON.name) == len(BRANCHES)


# ============================================================
#  Proofs (safety; the latency win above is empirical)
# ============================================================


@requires_z3
def test_race_net_proves_at_most_one_commit_per_turn() -> None:
    # Bound net, as it runs (actions are never invoked; only structure is encoded).
    # No assume_atomic_firing: these hold with every commit split into start and
    # completion, because the permit is consumed at start.
    assert_each_proven(
        SPEC.build(race_actions(DELAYS)),
        {
            "one commit per turn: place_bound(RACE_WON, 1)": lp.place_bound(RACE_WON.name, 1),
            "one egress event per turn: place_bound(EVENT_OUT, 1)": lp.place_bound(
                C.EVENT_OUT.name, 1
            ),
            "deadlock_free": lp.deadlock_free(),
        },
        initial_marking={C.USER_IN.name: 1},
        sink_places=[C.EVENT_OUT.name, RACE_WON.name, RACE_DISCARDED.name],
        # A loser that never started keeps its trigger: the inhibitor on RACE_WON is
        # the cancellation. Excuse those triggers only once the race is won, so a
        # trigger stranded without a winner would still be reported.
        sink_places_when={RACE_WON.name: [t.name for t in TRIGGER.values()]},
    )


@requires_z3
def test_race_permit_never_stacks_across_two_turns() -> None:
    # A second turn must not stack a second permit on the first: Race_Start resets the
    # place before it seeds. assume_atomic_firing is exact here: the only
    # counterexample without it is Race_Start starting again while an earlier firing
    # is in flight, and its action is synchronous, so it completes inside its firing
    # on the Rust executor as well (Java relies on CONC-002 instead).
    # RACE_WON and EVENT_OUT are per-turn bounds only: a turn that starts while the
    # previous turn's commit is in flight sees that commit land after its reset.
    assert_each_proven(
        SPEC.build(race_actions(DELAYS)),
        {"permit never stacks: place_bound(RACE_PERMIT, 1)": lp.place_bound(RACE_PERMIT.name, 1)},
        environment_places=[C.USER_IN.name],
        environment_mode=lp.arrivals(2, 2),
        assume_atomic_firing=True,
    )
