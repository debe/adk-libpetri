"""Pattern C's YAML twin: ``yaml_optimistic/root_agent.yaml`` is the demo's ``SPEC``.

The validation is a node (``.agent.validate``) whose route, ``pass`` or
``fail``, picks the branch of ``Opt_Validate``'s route-labelled xor; the
labels are the blueprint's, the net's xor is the demo's. Both commits are
``action: emit``. See ``test_pattern_c_optimistic_commit_demo.py`` for the
pattern itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from google.adk.agents.config_agent_utils import from_config

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.net import PetriNet
from adk_libpetri.net.proofs import claim_options
from demos.patterns.test_pattern_c_optimistic_commit_demo import SPEC
from support.smt_proofs import requires_z3

from ._drive import HERE, agent_module, run_turn_then_drain

TWIN = HERE / "yaml_optimistic" / "root_agent.yaml"


def twin() -> PetriNet:
    node = from_config(str(TWIN))
    assert isinstance(node, PetriNet)
    return node


@pytest.fixture
def agent() -> Iterator[Any]:
    """The twin's node module, its per-test scores and delays reset afterwards."""
    module: Any = agent_module("yaml_optimistic")
    yield module
    module.CONFIG = module.Config()


def test_the_twin_is_the_demos_spec() -> None:
    got = twin().spec.fingerprint()
    want = SPEC.fingerprint()
    assert (got.pop("name"), want.pop("name")) == ("opt_agent", "optimistic-commit")
    assert got.pop("ports") == [
        {"direction": "out", "name": "eventOut", "place": "eventOut"},
        {"direction": "in", "name": "userIn", "place": "userIn"},
    ]
    assert want.pop("ports") == []
    assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)


# ============================================================
#  Behaviour
# ============================================================


async def test_cheap_path_commits_when_validation_passes_slow_discarded(
    orch: OrchestratorLoop, agent: Any
) -> None:
    cheap, slow = 0.03, 0.25
    agent.CONFIG = agent.Config(cheap_score=100, slow_score=100, cheap_delay=cheap, slow_delay=slow)
    run = await run_turn_then_drain(orch, TWIN, text="answer me cheaply")

    event, at = run.answer()
    assert event.output is not None
    assert event.output.branch_id == "cheap"
    # The validated cheap answer does not wait for the pre-warmed slow path.
    assert at < slow, f"answer after {at:.3f}s; a cheap commit means < {slow}s"
    assert at < run.node_run_at("slow")

    # Drained after slow finished: one commit, and slow's late answer was dropped.
    assert [e.output.text for e in run.egress] == ["answer from cheap"]
    assert run.final.count("committed") == 1
    assert run.final.count("validationPassed") == 0  # consumed by the commit
    assert [r.branch_id for r in run.final.tokens("slowDiscarded")] == ["slow"]


async def test_slow_path_commits_when_validation_fails(orch: OrchestratorLoop, agent: Any) -> None:
    cheap, slow = 0.10, 0.30
    agent.CONFIG = agent.Config(cheap_score=10, slow_score=100, cheap_delay=cheap, slow_delay=slow)
    run = await run_turn_then_drain(orch, TWIN, text="give me a thorough answer")

    event, at = run.answer()
    assert event.output is not None
    assert event.output.branch_id == "slow"
    # Pre-warmed: the fallback ran alongside cheap, so a failed turn costs about the
    # slow path alone, not cheap plus slow as sequential routes do.
    assert slow <= at < cheap + slow, (
        f"answer after {at:.3f}s; pre-warm means < {cheap + slow:.2f}s"
    )

    assert [e.output.text for e in run.egress] == ["answer from slow"]
    assert run.final.count("committed") == 1
    assert run.final.count("validationFailed") == 1
    assert run.final.count("slowDiscarded") == 0
    # The failed cheap result is parked, never committed.
    assert [r.branch_id for r in run.final.tokens("cheapPending")] == ["cheap"]


# ============================================================
#  Proofs: the demo's claims, from the twin's prove: block
# ============================================================


def test_prove_block_states_the_demos_proof_options() -> None:
    bp = twin().blueprint
    [deadlock] = [c for c in bp.proof.claims if c.kind == "deadlock_free"]
    assert claim_options(bp, deadlock) == {
        "initial_marking": {"userIn": 1},
        "sink_places": [
            "eventOut",
            "committed",
            "slowDiscarded",
            "validationPassed",
            "validationFailed",
            "cheapPending",
        ],
        "sink_places_when": {"committed": ["slowTrigger"]},
    }


@requires_z3
def test_optimistic_twin_proves_at_most_one_commit_per_turn() -> None:
    proofs = twin().verify()
    assert [p.label for p in proofs] == [
        "one commit per turn: place_bound(COMMITTED, 1)",
        "validation verdicts exclusive: mutual_exclusion(PASSED, FAILED)",
        "deadlock_free",
    ]
    failed = {p.label: f"{p.result.verdict}\n{p.result.report}" for p in proofs if not p.proven}
    assert not failed, failed
