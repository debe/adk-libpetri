"""Pattern B's YAML twin: ``yaml_quorum/root_agent.yaml`` against the demo's ``SPEC``.

The twin is the demo's net but for one place type. The demo's
``Quorum_Synthesize`` action builds an ``Event`` for ``eventOut``; in a
blueprint only ``action: emit`` builds an ``Event``, and emit forwards the one
token of a ``one()`` arc, not the three of ``exactly(3)``. So synthesis is a
node, its output is a ``str``, and the twin declares ``eventOut: {type: str}``.
A non-``Event`` answer becomes the ``PetriNet``'s output, which ADK yields when
the node finishes, that is after the late branches: the early fire shows on the
``synthesize`` node run, not on the turn's last event. See
``test_pattern_b_quorum_demo.py`` for the pattern itself.
"""

from __future__ import annotations

import json
from typing import Any

from google.adk.agents.config_agent_utils import from_config

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.net import PetriNet
from adk_libpetri.net.proofs import claim_options
from demos.patterns.test_pattern_b_quorum_demo import SPEC
from support.smt_proofs import requires_z3

from ._drive import HERE, agent_module, run_turn_then_drain

TWIN = HERE / "yaml_quorum" / "root_agent.yaml"
K = 3


def twin() -> PetriNet:
    node = from_config(str(TWIN))
    assert isinstance(node, PetriNet)
    return node


def _canonical(fp: dict[str, Any]) -> str:
    return json.dumps(fp, sort_keys=True)


def test_the_twin_is_the_demos_spec_but_for_event_outs_type() -> None:
    got = twin().spec.fingerprint()
    want = SPEC.fingerprint()
    assert (got.pop("name"), want.pop("name")) == ("quorum_agent", "kofn-quorum")
    assert got.pop("ports") == [
        {"direction": "out", "name": "eventOut", "place": "eventOut"},
        {"direction": "in", "name": "userIn", "place": "userIn"},
    ]
    assert want.pop("ports") == []
    # The closest honest equality: the one difference is eventOut's type.
    assert {"name": "eventOut", "type": "Event"} in want["places"]
    assert {"name": "eventOut", "type": "str"} in got["places"]
    retyped = [
        {"name": "eventOut", "type": "str"} if p["name"] == "eventOut" else p
        for p in want["places"]
    ]
    assert _canonical(got) == _canonical({**want, "places": retyped})


# ============================================================
#  Behaviour
# ============================================================


async def test_synthesis_fires_at_kth_result_without_waiting_for_slower_branches(
    orch: OrchestratorLoop,
) -> None:
    agent = agent_module("yaml_quorum")
    first_late = sorted(agent.DELAYS.values())[K]
    run = await run_turn_then_drain(orch, TWIN, text="synthesize a consensus answer")

    # One synthesis, from the three FASTEST branches in completion order.
    event, at = run.answer()
    assert event.output == "synth(b1,b2,b3)"
    # The load-bearing assertion: synthesis runs well before the 4th branch is
    # done (a barrier join would take the slowest branch's 0.8s) ...
    synthesized = run.node_run_at("synthesize")
    assert synthesized < first_late, (
        f"synthesis after {synthesized:.3f}s; quorum means < {first_late}s"
    )
    assert synthesized < run.node_run_at("b4") < run.node_run_at("b5")
    # ... and so does the answer, a str on eventOut: emitted as the net's output
    # at once, under the transition that answered, not when the losers are done.
    assert at < first_late
    assert event.node_info.path.endswith("@1/Quorum_Synthesize@1")

    # Drained after b4 and b5 finished: still one synthesis, and the late results
    # are kept on quorumDiscarded rather than lost.
    assert run.egress == ["synth(b1,b2,b3)"]
    assert run.final.count("quorumMet") == 1
    assert run.final.count("quorumResult") == 0
    assert [r.branch_id for r in run.final.tokens("quorumDiscarded")] == ["b4", "b5"]


# ============================================================
#  Proofs: the demo's claims, from the twin's prove: block
# ============================================================


def test_prove_block_states_the_demos_proof_options() -> None:
    bp = twin().blueprint
    [deadlock] = [c for c in bp.proof.claims if c.kind == "deadlock_free"]
    assert claim_options(bp, deadlock) == {
        "initial_marking": {"userIn": 1},
        "sink_places": ["eventOut", "quorumMet", "quorumDiscarded", "quorumResult"],
    }


@requires_z3
def test_quorum_twin_proves_exactly_one_synthesis_per_turn() -> None:
    proofs = twin().verify()
    assert [p.label for p in proofs] == [
        "one synthesis per turn: place_bound(QUORUM_MET, 1)",
        "one egress event per turn: place_bound(EVENT_OUT, 1)",
        "deadlock_free",
    ]
    failed = {p.label: f"{p.result.verdict}\n{p.result.report}" for p in proofs if not p.proven}
    assert not failed, failed
