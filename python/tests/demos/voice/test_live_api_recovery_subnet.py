"""Port of Java ``LiveApiRecoverySubnetTest``: the silence ladder and its cancellation.

Java runs every timing scenario on ``ManualClock`` so it can assert exact
boundaries (79 ms vs 80 ms). Here they run on libpetri's ``SteppedClock``,
which moves only when the test advances it, with the same exact boundaries.

Java's composed-net SCG check has no libpetri-py counterpart (there is no
``StateClassGraph`` binding). Its claim, that the composed BIDI net's reachable
state space is finite under a ``bounded(1)`` ``MODEL_QUIET``, is proved here
instead with SMT: every place of the composed net holds at most one token.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import libpetri as lp
import pytest

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import NetSpec
from adk_libpetri.runner import HandleRef, PetriRunner
from adk_libpetri.subnet import llm_streaming_step as LS
from adk_libpetri.subnet import merge
from support.fake_llm import ScriptedLlm
from support.smt_proofs import assert_each_proven, requires_z3

from . import barge_in_subnet as BI
from . import live_api_recovery_subnet as R
from ._support import (
    advance_and_settle,
    count,
    marked,
    settle,
    stepped_clock,
)

P = R.Places
T = R.Transitions

FAST = R.Config(timedelta(milliseconds=80), timedelta(milliseconds=80))


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("recovery-orchestrator")
    yield loop
    loop.close()


async def start(
    orch: OrchestratorLoop, config: R.Config, clock: Any = None
) -> tuple[PetriRunner, lp.InMemoryEventStore]:
    store = lp.InMemoryEventStore()
    b = (
        PetriRunner.builder(NetSpec.compose("test", R.def_(config)), R.action_bindings(config))
        .environment_places(P.RESPONSE_AWAITED, P.MODEL_ACTIVE, P.MODEL_QUIET)
        .event_store(store)
        .orchestrator(orch)
    )
    if clock is not None:
        b = b.clock(clock).deadline_tolerance(timedelta(0))
    return await b.astart(), store


async def finish(runner: PetriRunner) -> lp.MarkingView:
    """Java ``executor.drain()`` then ``task.get``: pending timers still fire."""
    runner.drain()
    return await asyncio.wait_for(runner.wait_closed(), 5)


def tokens_at(m: lp.MarkingView, place: Any) -> int:
    return m.count(place.name)


def fired(store: lp.InMemoryEventStore, transition: str) -> int:
    return sum(
        1
        for e in store.events()
        if e.type == "TransitionCompleted" and e.transition_name == transition
    )


# ============================================================
#  Silent model: nudge, then reconnect
# ============================================================


async def test_silent_model_triggers_nudge_then_reconnect(orch) -> None:
    clock = stepped_clock()
    runner, _ = await start(orch, FAST, clock)
    # Caller signals "the model should be responding but isn't".
    await settle(clock, lambda: runner.signal(P.RESPONSE_AWAITED))

    await advance_and_settle(clock, 79)
    assert not await marked(runner, P.NUDGE_NEEDED)
    await advance_and_settle(clock, 1)
    assert await marked(runner, P.NUDGE_NEEDED)

    # The reconnect window starts when the nudge fires, not before.
    await advance_and_settle(clock, 79)
    assert not await marked(runner, P.RECONNECT_NEEDED)
    await advance_and_settle(clock, 1)

    final = await finish(runner)
    assert tokens_at(final, P.NUDGE_NEEDED) == 1
    assert tokens_at(final, P.RECONNECT_NEEDED) == 1


# ============================================================
#  Model activity inhibits the ladder
# ============================================================


async def test_model_active_inhibits_nudge(orch) -> None:
    clock = stepped_clock()
    runner, _ = await start(orch, FAST, clock)
    # The model is actively responding: MODEL_ACTIVE lands first.
    await settle(clock, lambda: runner.signal(P.MODEL_ACTIVE))
    await settle(clock, lambda: runner.signal(P.RESPONSE_AWAITED))
    await advance_and_settle(clock, 10_000)

    final = await finish(runner)
    # Neither stage fired: model presence inhibited both.
    assert tokens_at(final, P.NUDGE_NEEDED) == 0
    assert tokens_at(final, P.RECONNECT_NEEDED) == 0


async def test_model_active_resumes_mid_window_blocks_both_recovery_stages(
    orch,
) -> None:
    # RESPONSE_AWAITED, then 40 ms later (well before the 80 ms nudge)
    # MODEL_ACTIVE: the inhibitor blocks both Nudge and Recover.
    clock = stepped_clock()
    runner, _ = await start(orch, FAST, clock)
    await settle(clock, lambda: runner.signal(P.RESPONSE_AWAITED))
    await advance_and_settle(clock, 40)
    await settle(clock, lambda: runner.signal(P.MODEL_ACTIVE))
    await advance_and_settle(clock, 10_000)

    final = await finish(runner)
    assert tokens_at(final, P.NUDGE_NEEDED) == 0
    assert tokens_at(final, P.RECONNECT_NEEDED) == 0


# ============================================================
#  Cancel on activity: an answer consumes the rung it lands on, and
#  MODEL_QUIET clears MODEL_ACTIVE in-net, so the ladder restarts on
#  the caller's next RESPONSE_AWAITED.
# ============================================================


async def test_model_answered_then_silence_then_fresh_response_awaited_nudges_after_3s(
    orch,
) -> None:
    clock = stepped_clock()
    runner, _ = await start(orch, R.Config.defaults(), clock)
    await settle(clock, lambda: runner.signal(P.RESPONSE_AWAITED))
    await advance_and_settle(clock, 1_000)
    # The model answers: the awaited rung is cancelled, not suspended.
    await settle(clock, lambda: runner.signal(P.MODEL_ACTIVE))
    assert not await marked(runner, P.RESPONSE_AWAITED)
    await advance_and_settle(clock, 1_000)
    # The model goes quiet, then the caller awaits the next reply.
    await settle(clock, lambda: runner.signal(P.MODEL_QUIET))
    assert not await marked(runner, P.MODEL_ACTIVE)
    await settle(clock, lambda: runner.signal(P.RESPONSE_AWAITED))

    await advance_and_settle(clock, 2_999)
    assert not await marked(runner, P.NUDGE_NEEDED)
    await advance_and_settle(clock, 1)
    assert await marked(runner, P.NUDGE_NEEDED)

    # The nudge works: the model answers inside the reconnect window, and
    # AnsweredLate cancels the second rung.
    await advance_and_settle(clock, 1_000)
    await settle(clock, lambda: runner.signal(P.MODEL_ACTIVE))
    assert not await marked(runner, P.RECOVERY_PENDING)
    await advance_and_settle(clock, 10_000)

    final = await finish(runner)
    assert tokens_at(final, P.NUDGE_NEEDED) == 1
    assert tokens_at(final, P.RECOVERY_PENDING) == 0
    assert tokens_at(final, P.RECONNECT_NEEDED) == 0


async def test_stale_response_awaited_after_a_reply_never_nudges(orch) -> None:
    clock = stepped_clock()
    runner, _ = await start(orch, FAST, clock)
    # The reply is already under way when a stale RESPONSE_AWAITED lands.
    await settle(clock, lambda: runner.signal(P.MODEL_ACTIVE))
    await settle(clock, lambda: runner.signal(P.RESPONSE_AWAITED))
    assert not await marked(runner, P.RESPONSE_AWAITED)
    # Even after the model goes quiet, nothing is left to escalate.
    await settle(clock, lambda: runner.signal(P.MODEL_QUIET))
    await advance_and_settle(clock, 10_000)

    final = await finish(runner)
    assert tokens_at(final, P.NUDGE_NEEDED) == 0
    assert tokens_at(final, P.RECOVERY_PENDING) == 0
    assert tokens_at(final, P.RECONNECT_NEEDED) == 0


async def test_model_quiet_clears_model_active(orch) -> None:
    clock = stepped_clock()
    runner, _ = await start(orch, FAST, clock)
    # Stacked activity injections clear in one ModelQuiet firing.
    await settle(clock, lambda: runner.signal(P.MODEL_ACTIVE))
    await settle(clock, lambda: runner.signal(P.MODEL_ACTIVE))
    assert await count(runner, P.MODEL_ACTIVE) == 2
    await settle(clock, lambda: runner.signal(P.MODEL_QUIET))
    assert not await marked(runner, P.MODEL_ACTIVE)
    assert not await marked(runner, P.MODEL_QUIET)
    assert not await marked(runner, P.QUIET_IGNORED)

    # Further quiets with the model not active are sunk, not stranded, and
    # the sink holds at most one token.
    await settle(clock, lambda: runner.signal(P.MODEL_QUIET))
    await settle(clock, lambda: runner.signal(P.MODEL_QUIET))
    assert not await marked(runner, P.MODEL_QUIET)

    # With MODEL_ACTIVE cleared, both timers run again.
    await settle(clock, lambda: runner.signal(P.RESPONSE_AWAITED))
    await advance_and_settle(clock, 80)
    await advance_and_settle(clock, 80)

    final = await finish(runner)
    assert tokens_at(final, P.MODEL_ACTIVE) == 0
    assert tokens_at(final, P.QUIET_IGNORED) == 1
    assert tokens_at(final, P.NUDGE_NEEDED) == 1
    assert tokens_at(final, P.RECONNECT_NEEDED) == 1


async def test_model_active_appears_between_nudge_and_reconnect_stops_recovery(
    orch,
) -> None:
    clock = stepped_clock()
    runner, _ = await start(orch, FAST, clock)
    await settle(clock, lambda: runner.signal(P.RESPONSE_AWAITED))
    await advance_and_settle(clock, 80)
    assert await marked(runner, P.NUDGE_NEEDED)
    # The model responds 79 ms into the 80 ms reconnect window.
    await advance_and_settle(clock, 79)
    await settle(clock, lambda: runner.signal(P.MODEL_ACTIVE))
    await advance_and_settle(clock, 10_000)

    final = await finish(runner)
    # Nudge fired (silent >= 80 ms), Recover did not (active before its window closed).
    assert tokens_at(final, P.NUDGE_NEEDED) == 1
    assert tokens_at(final, P.RECONNECT_NEEDED) == 0


# ============================================================
#  Composed BIDI voice net: streaming + barge-in + recovery is bounded.
# ============================================================


def composed_voice_net() -> tuple[NetSpec, lp.BuiltNet]:
    # Bind every subnet so the proof is about the net that runs; the actions
    # are never invoked, only the structure is encoded.
    spec = NetSpec.compose("voice-scg-check", LS.DEF, BI.DEF, R.def_(FAST))
    actions = merge(
        LS.action_bindings(ScriptedLlm.of(), LS.Config("scg", HandleRef())),
        BI.action_bindings(),
        R.action_bindings(FAST),
    )
    return spec, spec.build(actions)


# The bound is proved with assume_atomic_firing=True. libpetri-py's executor
# (Rust) may start a transition again while its earlier firing is in flight
# (CONC-002), and under that reading IgnoreQuiet's reset (at start) can be
# overtaken by two deposits (at completion): QUIET_IGNORED reaches 2. Every
# action of these subnets that has an output is a sync action, which completes
# inside its firing, so no firing overlaps another and the atomic reading is
# the executor's. Java's executor never overlaps firings, so Java needs no
# such assumption.
_BOUND_OPTS: dict[str, Any] = {
    "initial_marking": {C.LLM_REQUEST.name: 1},
    "environment_places": [P.MODEL_QUIET.name],
    "environment_mode": lp.bounded(1),
}


@requires_z3
def test_composed_bidi_voice_net_is_bounded_with_model_quiet_as_a_bounded_env_place() -> None:
    spec, net = composed_voice_net()

    # MODEL_QUIET is the only environment place modelled, refilled to at most
    # one resident token forever (bounded(1)). Its consumers are ModelQuiet
    # and IgnoreQuiet, so a quiet signal may arrive at any point of the turn.
    # Java shows boundedness by a state-class graph that completes; here
    # every place gets an SMT bound of one token.
    assert_each_proven(
        net,
        {f"bound({p.name})": lp.place_bound(p.name, 1) for p in spec.places},
        assume_atomic_firing=True,
        **_BOUND_OPTS,
    )


@requires_z3
def test_without_atomic_firing_quiet_ignored_bound_rests_on_firings_not_overlapping() -> None:
    # Locks the CONC-002 caveat above: without the assumption the verifier
    # finds two overlapping IgnoreQuiet firings and confirms the overlap trace.
    _, net = composed_voice_net()
    result = lp.verify(net, lp.place_bound(P.QUIET_IGNORED.name, 1), **_BOUND_OPTS)
    assert result.is_violated(), result.report
    assert "CONC-002" in result.report


@requires_z3
def test_the_bound_is_not_vacuous_quiet_ignored_needs_its_reset_arc() -> None:
    # Keep the proof honest: without IgnoreQuiet's reset, a stream of quiet
    # signals with the model idle grows QUIET_IGNORED past one, even with
    # firings atomic.
    spec = R.def_(FAST)
    no_reset = NetSpec(
        "no-reset",
        tuple(
            dataclasses.replace(t, resets=()) if t.name == T.IGNORE_QUIET else t
            for t in spec.transitions
        ),
    )
    result = lp.verify(
        no_reset.build(R.action_bindings(FAST)),
        lp.place_bound(P.QUIET_IGNORED.name, 1),
        environment_places=[P.MODEL_QUIET.name],
        environment_mode=lp.bounded(1),
        assume_atomic_firing=True,
    )
    assert result.is_violated(), result.report


# ============================================================
#  Shape
# ============================================================


def test_config_rejects_zero_or_negative_durations() -> None:
    with pytest.raises(ValueError):
        R.Config(timedelta(0), timedelta(seconds=1))
    with pytest.raises(ValueError):
        R.Config(timedelta(seconds=1), timedelta(milliseconds=-1))


def test_def_declares_six_transitions_and_six_ports() -> None:
    spec = R.def_(R.Config.defaults())
    assert sorted(spec.transition_names) == [
        T.ANSWERED,
        T.ANSWERED_LATE,
        T.IGNORE_QUIET,
        T.MODEL_QUIET,
        T.NUDGE,
        T.RECOVER,
    ]
    assert sorted(p.name for p in spec.ports) == [
        "modelActive",
        "modelQuiet",
        "nudgeNeeded",
        "quietIgnored",
        "reconnectNeeded",
        "responseAwaited",
    ]
    assert spec.transition(T.NUDGE).timing == R.delayed(3_000)
    assert spec.transition(T.RECOVER).timing == R.delayed(3_000)
    assert spec.transition(T.ANSWERED).priority == 10
    assert spec.transition(T.ANSWERED_LATE).priority == 10
