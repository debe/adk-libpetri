"""Pattern C: optimistic commit with a pre-warmed fallback, XOR validation, one ``COMMITTED``.

Port of ``PatternC_OptimisticCommitDemoTest.java``; the ADK-only half is
``test_pattern_c_adk_only_foil.py``.

The cheap and slow paths start concurrently on one ``USER_IN``. The cheap result
goes through a validation transition whose output is an XOR: on pass the cheap
path commits and the slow result is discarded; on fail the slow path commits,
waiting for its result if it is not ready yet. Which path commits is a
consequence of the ``VALIDATION_PASSED`` / ``VALIDATION_FAILED`` and
``COMMITTED`` markings: no caller-side retry, no loop, no ``session.state``
field carrying the verdict across node boundaries.

**Why this exists.** ADK Python 2.11 is closer than ADK Java here, and the foil
says so: a ``Workflow`` route *can* skip the slow path when the cheap answer
validates, so the Java claim that the stock agents cannot express the conditional
fallback holds only for the deprecated ``SequentialAgent``. What no Workflow
expresses is the *pre-warmed* form with one commit: routes start the slow path only
after cheap has failed (a failed turn costs cheap plus slow), a fan-out from START
commits twice, and a ``JoinNode`` holds a validated cheap answer until slow ends.
This net is the pre-warmed form, and Z3 proves the single commit.

Topology::

    [USER_IN] --Opt_StartBoth--> AND(cheapTrigger, slowTrigger)
                                 reset(COMMITTED, VALIDATION_*, *Trigger, cheapDone,
                                       slowDone, cheapPending, slowDiscarded)
    [cheapTrigger] --Opt_RunCheap--> [cheapDone]
    [slowTrigger]  --Opt_RunSlow-->  [slowDone]                    inhibitor(COMMITTED)
    [cheapDone] --Opt_Validate--> XOR(AND(VALIDATION_PASSED, cheapPending),
                                      AND(VALIDATION_FAILED, cheapPending))
    [VALIDATION_PASSED] + [cheapPending] --Opt_CommitCheap--> AND(EVENT_OUT, COMMITTED)
                                                         inhibitor(COMMITTED), prio +10
    [slowDone] --Opt_CommitSlow--> AND(EVENT_OUT, COMMITTED)
                                   read(VALIDATION_FAILED), inhibitor(COMMITTED), prio +10
    [slowDone] --Opt_DiscardSlow--> [slowDiscarded]          read(COMMITTED), prio -10

**The XOR, not inhibitor(COMMITTED), keeps this at one commit.** An inhibitor reads
the pass-start marking (Pattern A lost its bound that way). Only one commit is ever
enabled in a turn: ``Opt_Validate`` picks one branch, and the slow path produces one
``slowDone`` per turn. Java adds that a second ``slowDone`` would reach the same
``Opt_CommitSlow``, which its executor never restarts while in flight. The Rust
executor may restart an async transition while an earlier firing is in flight, so in
Python that argument rests instead on the commit actions being synchronous (they
complete inside their firing) and on the one-token-per-turn trigger. The proofs
below need neither: they hold without ``assume_atomic_firing``.

**How it runs.** Through ``PetriAgent`` under a stock ``InMemoryRunner``, as Java
does; the runner is then drained so a discarded slow path finishes and drains.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import libpetri as lp
import pytest

from adk_libpetri import colours as C
from adk_libpetri import on_loop
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import Action, Ctx, NetSpec, Place, TransitionSpec, and_, one, out, xor
from support.smt_proofs import assert_each_proven, requires_z3

from ._petri_harness import NetRun, model_event, orchestrator, run_turn_then_drain, text_of

THRESHOLD = 50
AGENT = "opt_agent"


@dataclass(frozen=True)
class BranchResult:
    branch_id: str
    text: str
    score: int


# ============================================================
#  Places and the net (~50 lines of user code, no stock subnet)
# ============================================================

CHEAP_TRIGGER = Place("cheapTrigger")
SLOW_TRIGGER = Place("slowTrigger")
CHEAP_DONE = Place("cheapDone", BranchResult)
SLOW_DONE = Place("slowDone", BranchResult)
VALIDATION_PASSED = Place("validationPassed")
VALIDATION_FAILED = Place("validationFailed")
COMMITTED = Place("committed")
SLOW_DISCARDED = Place("slowDiscarded", BranchResult)
# Validation consumes cheapDone but emits only a verdict; the result waits here
# for Opt_CommitCheap to author the event.
CHEAP_PENDING = Place("cheapPending", BranchResult)

SPEC = NetSpec(
    "optimistic-commit",
    (
        TransitionSpec(
            "Opt_StartBoth",
            (one(C.USER_IN),),
            and_(CHEAP_TRIGGER, SLOW_TRIGGER),
            # Triggers too: a turn the cheap path won can leave slowTrigger behind,
            # and a long-lived session would otherwise run it on the next turn.
            resets=(
                COMMITTED,
                VALIDATION_PASSED,
                VALIDATION_FAILED,
                CHEAP_TRIGGER,
                SLOW_TRIGGER,
                CHEAP_DONE,
                SLOW_DONE,
                CHEAP_PENDING,
                SLOW_DISCARDED,
            ),
        ),
        TransitionSpec("Opt_RunCheap", (one(CHEAP_TRIGGER),), out(CHEAP_DONE)),
        # Don't bother if cheap already won.
        TransitionSpec(
            "Opt_RunSlow", (one(SLOW_TRIGGER),), out(SLOW_DONE), inhibitors=(COMMITTED,)
        ),
        TransitionSpec(
            "Opt_Validate",
            (one(CHEAP_DONE),),
            xor(and_(VALIDATION_PASSED, CHEAP_PENDING), and_(VALIDATION_FAILED, CHEAP_PENDING)),
        ),
        TransitionSpec(
            "Opt_CommitCheap",
            (one(VALIDATION_PASSED), one(CHEAP_PENDING)),
            and_(C.EVENT_OUT, COMMITTED),
            inhibitors=(COMMITTED,),
            priority=10,
        ),
        TransitionSpec(
            "Opt_CommitSlow",
            (one(SLOW_DONE),),
            and_(C.EVENT_OUT, COMMITTED),
            reads=(VALIDATION_FAILED,),
            inhibitors=(COMMITTED,),
            priority=10,
        ),
        TransitionSpec(
            "Opt_DiscardSlow",
            (one(SLOW_DONE),),
            out(SLOW_DISCARDED),
            reads=(COMMITTED,),
            priority=-10,
        ),
    ),
)


# ============================================================
#  Actions
# ============================================================


def opt_actions(
    *, cheap_score: int, slow_score: int, cheap_delay: float, slow_delay: float
) -> dict[str, Action]:
    passes: Callable[[BranchResult], bool] = lambda r: r.score >= THRESHOLD  # noqa: E731

    def start(ctx: Ctx) -> None:
        ctx.input(C.USER_IN)
        ctx.signal(CHEAP_TRIGGER)
        ctx.signal(SLOW_TRIGGER)

    def branch(name: str, score: int, delay: float, trigger: Place, done: Place) -> Action:
        async def run(ctx: Ctx) -> None:
            ctx.input(trigger)
            await on_loop(asyncio.sleep(delay))  # the stand-in for an LLM call
            ctx.output(done, BranchResult(name, f"answer from {name}", score))

        return run

    def validate(ctx: Ctx) -> None:
        result = ctx.input(CHEAP_DONE)
        # The only place that decides pass versus fail; the commits are gated
        # purely on which verdict place this marks.
        ctx.signal(VALIDATION_PASSED if passes(result) else VALIDATION_FAILED)
        ctx.output(CHEAP_PENDING, result)

    def commit_cheap(ctx: Ctx) -> None:
        ctx.input(VALIDATION_PASSED)
        cheap = ctx.input(CHEAP_PENDING)
        ctx.output(C.EVENT_OUT, model_event(AGENT, "opt-cheap", cheap.text))
        ctx.signal(COMMITTED)

    def commit_slow(ctx: Ctx) -> None:
        slow = ctx.input(SLOW_DONE)
        ctx.output(C.EVENT_OUT, model_event(AGENT, "opt-slow", slow.text))
        ctx.signal(COMMITTED)

    def discard_slow(ctx: Ctx) -> None:
        ctx.output(SLOW_DISCARDED, ctx.input(SLOW_DONE))

    return {
        "Opt_StartBoth": start,
        "Opt_RunCheap": branch("cheap", cheap_score, cheap_delay, CHEAP_TRIGGER, CHEAP_DONE),
        "Opt_RunSlow": branch("slow", slow_score, slow_delay, SLOW_TRIGGER, SLOW_DONE),
        "Opt_Validate": validate,
        "Opt_CommitCheap": commit_cheap,
        "Opt_CommitSlow": commit_slow,
        "Opt_DiscardSlow": discard_slow,
    }


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    yield from orchestrator("pattern-c-orchestrator")


async def run_once(orch: OrchestratorLoop, actions: dict[str, Action], text: str) -> NetRun:
    return await run_turn_then_drain(
        orch,
        SPEC,
        actions,
        agent_name=AGENT,
        description="Optimistic commit with structural fallback",
        text=text,
    )


# ============================================================
#  Behaviour
# ============================================================


async def test_cheap_path_commits_when_validation_passes_slow_discarded(
    orch: OrchestratorLoop,
) -> None:
    cheap, slow = 0.03, 0.25
    run = await run_once(
        orch,
        opt_actions(cheap_score=100, slow_score=100, cheap_delay=cheap, slow_delay=slow),
        "answer me cheaply",
    )

    [event] = run.authored_by(AGENT)
    assert text_of(event) == "answer from cheap"
    # The validated cheap answer does not wait for the pre-warmed slow path.
    assert run.elapsed < slow, f"turn took {run.elapsed:.3f}s; a cheap commit means < {slow}s"

    # Drained after slow finished: one commit, and slow's late answer was dropped.
    assert [text_of(e) for e in run.egress] == ["answer from cheap"]
    assert run.final.count(COMMITTED.name) == 1
    assert [r.branch_id for r in run.final.tokens(SLOW_DISCARDED.name)] == ["slow"]


async def test_slow_path_commits_when_validation_fails(orch: OrchestratorLoop) -> None:
    cheap, slow = 0.10, 0.30
    run = await run_once(
        orch,
        opt_actions(cheap_score=10, slow_score=100, cheap_delay=cheap, slow_delay=slow),
        "give me a thorough answer",
    )

    [event] = run.authored_by(AGENT)
    assert text_of(event) == "answer from slow"
    # Pre-warmed: the fallback ran alongside cheap, so a failed turn costs about the
    # slow path alone, not cheap plus slow as the foil's sequential routes do.
    assert slow <= run.elapsed < cheap + slow, (
        f"turn took {run.elapsed:.3f}s; pre-warm means < {cheap + slow:.2f}s"
    )

    assert [text_of(e) for e in run.egress] == ["answer from slow"]
    assert run.final.count(COMMITTED.name) == 1
    assert run.final.count(VALIDATION_FAILED.name) == 1
    assert run.final.count(SLOW_DISCARDED.name) == 0


# ============================================================
#  Proofs (safety; the latency wins above are empirical)
# ============================================================


@requires_z3
def test_optimistic_commit_net_proves_at_most_one_commit_per_turn() -> None:
    # Bound net, as it runs (actions are never invoked; only structure is encoded).
    # No assume_atomic_firing: these hold with every commit split into start and
    # completion.
    assert_each_proven(
        SPEC.build(opt_actions(cheap_score=100, slow_score=100, cheap_delay=0, slow_delay=0)),
        {
            "one commit per turn: place_bound(COMMITTED, 1)": lp.place_bound(COMMITTED.name, 1),
            "validation verdicts exclusive: mutual_exclusion(PASSED, FAILED)": (
                lp.mutual_exclusion([VALIDATION_PASSED.name, VALIDATION_FAILED.name])
            ),
            "deadlock_free": lp.deadlock_free(),
        },
        initial_marking={C.USER_IN.name: 1},
        sink_places=[
            C.EVENT_OUT.name,
            COMMITTED.name,
            SLOW_DISCARDED.name,
            VALIDATION_PASSED.name,
            VALIDATION_FAILED.name,
            CHEAP_PENDING.name,
        ],
        # slowTrigger is stranded by design once the cheap path commits (Opt_RunSlow is
        # inhibited by COMMITTED: that is the cancellation). Excuse it only under that
        # marker, so a slowTrigger left behind without a commit is still reported.
        sink_places_when={COMMITTED.name: [SLOW_TRIGGER.name]},
    )
