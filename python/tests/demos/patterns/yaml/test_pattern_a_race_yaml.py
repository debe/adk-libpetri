"""Pattern A's YAML twin: ``yaml_race/root_agent.yaml`` is ``race_spec()`` and acts as it.

The twin is loaded by ADK's own ``from_config`` and run under a stock
``InMemoryRunner``; its branches are ``FunctionNode``\\ s (``.agent.fast``, ...),
its commits ``action: emit`` and its discards the default move. See
``test_pattern_a_speculative_race_demo.py`` for the pattern itself.
"""

from __future__ import annotations

import json
from typing import Any

import libpetri as lp
from google.adk.agents.config_agent_utils import from_config

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.net import PetriNet
from adk_libpetri.net.blueprint import NetScope
from adk_libpetri.net.proofs import claim_options
from demos.patterns.test_pattern_a_speculative_race_demo import race_spec
from support.smt_proofs import requires_z3

from ._drive import HERE, agent_module, run_turn_then_drain

TWIN = HERE / "yaml_race" / "root_agent.yaml"


def twin() -> PetriNet:
    node = from_config(str(TWIN))
    assert isinstance(node, PetriNet)
    return node


def _canonical(fp: dict[str, Any]) -> str:
    return json.dumps(fp, sort_keys=True)


def test_the_twin_is_race_spec() -> None:
    got = twin().spec.fingerprint()
    want = race_spec().fingerprint()
    # Two keys differ by construction, and only these: a blueprint's name is an
    # ADK node name (an identifier, so not "speculative-race"), and a top-level
    # blueprint gets the turn protocol's ports, which the hand-written spec
    # leaves undeclared.
    assert (got.pop("name"), want.pop("name")) == ("race_agent", "speculative-race")
    assert got.pop("ports") == [
        {"direction": "out", "name": "eventOut", "place": "eventOut"},
        {"direction": "in", "name": "userIn", "place": "userIn"},
    ]
    assert want.pop("ports") == []
    # Every place (with its type name), arc, priority and timing is the same.
    assert _canonical(got) == _canonical(want)


# ============================================================
#  Behaviour
# ============================================================


async def test_fastest_branch_commits_first_and_losers_drain_to_discard(
    orch: OrchestratorLoop,
) -> None:
    agent = agent_module("yaml_race")
    agent.FINISHED.clear()
    run = await run_turn_then_drain(orch, TWIN, text="which branch wins?")

    # The answer is the fast branch's, and it comes before the slow branch is done.
    event, at = run.answer()
    assert event.output == agent.BranchResult("fast", "answer from fast")
    assert at < agent.SLOW, f"answer after {at:.3f}s; first-wins means < {agent.SLOW}s"
    # The invocation itself lasts until the losers' node runs are over: they run
    # inside it (PetriAgent, in the Python demo, ends the turn at the answer).
    assert run.elapsed >= agent.SLOW
    assert at < run.node_run_at("medium") < run.node_run_at("slow")

    # Over the runner's whole life, drained after the losers finished: one commit.
    assert [e.output for e in run.egress] == [agent.BranchResult("fast", "answer from fast")]
    assert run.final.count("raceWon") == 1
    assert run.final.count("racePermit") == 0
    # The losers were not cancelled; they ran out and their results were dropped.
    assert agent.FINISHED == ["fast", "medium", "slow"]
    assert sorted(r.branch_id for r in run.final.tokens("raceDiscarded")) == ["medium", "slow"]


async def test_all_results_ready_in_one_pass_commit_exactly_once() -> None:
    # The twin's own actions (emit and move; no node fires), from the marking that
    # made the inhibitor encoding commit more than once.
    agent = agent_module("yaml_race")
    bp = twin().blueprint
    initial: dict[str, list[object]] = {
        f"branch{x}Done": [agent.BranchResult(n, f"answer from {n}")]
        for x, n in zip("ABC", ("fast", "medium", "slow"), strict=True)
    }
    initial["racePermit"] = [None]

    final = await lp.run_async(bp.spec.build(bp.actions(NetScope())), initial=initial)

    assert final.count("raceWon") == 1
    assert final.count("eventOut") == 1
    assert final.count("raceDiscarded") == 2
    assert final.count("racePermit") == 0


# ============================================================
#  Proofs: the demo's claims, from the twin's prove: block
# ============================================================


def test_prove_block_states_the_demos_proof_options() -> None:
    bp = twin().blueprint
    by_label = {c.label: c for c in bp.proof.claims}
    sinks = {
        "initial_marking": {"userIn": 1},
        "sink_places": ["eventOut", "raceWon", "raceDiscarded"],
        "sink_places_when": {"raceWon": ["triggerA", "triggerB", "triggerC"]},
    }
    assert claim_options(bp, by_label["deadlock_free"]) == sinks
    for label in (
        "one commit per turn: place_bound(RACE_WON, 1)",
        "one egress event per turn: place_bound(EVENT_OUT, 1)",
    ):
        assert claim_options(bp, by_label[label]) == {"initial_marking": {"userIn": 1}}
    stacking = claim_options(bp, by_label["permit never stacks: place_bound(RACE_PERMIT, 1)"])
    assert stacking.pop("environment_mode").__repr__() == lp.arrivals(2, 2).__repr__()
    assert stacking == {
        "initial_marking": {},
        "environment_places": ["userIn"],
        "assume_atomic_firing": True,
    }


@requires_z3
def test_race_twin_proves_at_most_one_commit_per_turn_and_no_stacked_permit() -> None:
    proofs = twin().verify()
    assert [p.label for p in proofs] == [
        "one commit per turn: place_bound(RACE_WON, 1)",
        "one egress event per turn: place_bound(EVENT_OUT, 1)",
        "deadlock_free",
        "permit never stacks: place_bound(RACE_PERMIT, 1)",
    ]
    failed = {p.label: f"{p.result.verdict}\n{p.result.report}" for p in proofs if not p.proven}
    assert not failed, failed


@requires_z3
def test_race_twin_permit_never_stacks_over_three_turns() -> None:
    # verify(k) replaces the arrivals bound: the two-turn permit claim, over three.
    [stacking] = [p for p in twin().verify(k=3) if "never stacks" in p.label]
    assert stacking.proven, stacking.result.report
