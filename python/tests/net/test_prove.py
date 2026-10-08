"""``prove:``: a blueprint's claims, one ``libpetri.verify`` each, before anything runs."""

from __future__ import annotations

from pathlib import Path

import libpetri as lp
import pytest
from google.adk.agents.config_agent_utils import from_config

from adk_libpetri.net import BlueprintError, PetriNet, parse_blueprint
from adk_libpetri.net.proofs import TURNS_LEFT, claim_options
from support.smt_proofs import requires_z3

from .conftest import BLUEPRINTS

RACE = BLUEPRINTS / "bp_race"


def load(path: Path) -> PetriNet:
    node = from_config(str(path))
    assert isinstance(node, PetriNet)
    return node


@requires_z3
def test_the_race_claims_are_proven() -> None:
    proofs = load(RACE / "race.yaml").verify()
    assert [p.label for p in proofs] == [
        "one commit per turn",
        "place_bound(eventOut, 1)",
        "deadlock_free",
        "permit never stacks",
    ]
    assert [p.kind for p in proofs] == [
        "place_bound",
        "place_bound",
        "deadlock_free",
        "place_bound",
    ]
    failures = {p.label: p.result.report for p in proofs if not p.proven}
    assert not failures, failures


@requires_z3
def test_the_inhibitor_race_violates_its_claim() -> None:
    [proof] = load(RACE / "race_inhibitor.yaml").verify()
    assert proof.violated, proof.result.report
    assert not proof.proven


@requires_z3
@pytest.mark.parametrize("path", ["bp_compose/twice.yaml", "bp_llm/root.yaml"])
def test_a_composed_nets_claims_are_proven(path: str) -> None:
    proofs = load(BLUEPRINTS / path).verify()
    assert proofs
    failures = {p.label: p.result.report for p in proofs if not p.proven}
    assert not failures, failures


def _write(tmp_path: Path, claims: str) -> Path:
    d = tmp_path / "bp_on_load"
    d.mkdir(exist_ok=True)
    f = d / "net.yaml"
    f.write_text(
        "agent_class: adk_libpetri.net.PetriNet\n"
        "name: on_load\n"
        "places: {done: {}}\n"
        "transitions:\n"
        "  Once_Done: {in: [userIn], out: {and: [eventOut, done]}, action: emit}\n"
        f"prove: {{on_load: true, claims: {claims}}}\n"
    )
    return f


@requires_z3
def test_on_load_proves_the_claims_when_the_file_loads(tmp_path: Path) -> None:
    good = _write(tmp_path, "[{place_bound: {place: done, bound: 1}}]")
    assert isinstance(from_config(str(good)), PetriNet)
    bad = _write(tmp_path, "[{place_bound: {place: done, bound: 0}}]")
    with pytest.raises(BlueprintError, match=r"claims not proven: place_bound\(done, 0\)"):
        from_config(str(bad))


# ----------------------------------------------------------------------------
#  What each claim is checked under
# ----------------------------------------------------------------------------

NET = {
    "places": {"permit": {"seed": 1}},
    "transitions": {
        "Gate_Pass": {
            "in": ["userIn", "permit"],
            "out": {"and": ["eventOut", "permit"]},
            "action": "emit",
        }
    },
}


def options(prove: dict, k: int | None = None) -> list[dict]:
    b = parse_blueprint("gate", {**NET, "prove": prove})
    return [claim_options(b, c, k) for c in b.proof.claims]


def test_without_options_the_user_inputs_come_turn_by_turn() -> None:
    safety, deadlock = options(
        {"claims": [{"place_bound": {"place": "permit", "bound": 1}}, "deadlock_free"]}
    )
    # A safety claim: one turn, the seeds plus that turn's input.
    assert safety == {"initial_marking": {"permit": 1, "userIn": 1}}
    # deadlock_free: two turns, the second input after the first answer.
    assert deadlock == {
        "initial_marking": {
            "permit": 1,
            "userIn": 1,
            TURNS_LEFT: 1,
        },
        # No node runs, so no turn:quiet:<node> tokens.
        "sink_places": ["eventOut", "turn:answered"],
    }
    [k3] = options({"claims": ["deadlock_free"]}, k=3)
    assert k3["initial_marking"][TURNS_LEFT] == 2
    [s3] = options({"claims": [{"place_bound": {"place": "permit", "bound": 1}}]}, k=3)
    assert s3["initial_marking"][TURNS_LEFT] == 2


def test_claim_options_lay_over_the_shared_ones() -> None:
    shared, own = options(
        {
            "options": {
                "initial_marking": {"userIn": 2},
                "sinks": ["eventOut", "permit"],
                "assume_atomic_firing": True,
            },
            "claims": [
                "deadlock_free",
                {
                    "deadlock_free": None,
                    "label": "under arrivals",
                    "options": {
                        "initial_marking": {},
                        "environment": {"userIn": {"arrivals": [2, 2]}},
                        "sinks_when": {"permit": ["eventOut"]},
                    },
                },
            ],
        }
    )
    assert shared == {
        "initial_marking": {"permit": 1, "userIn": 2},
        "sink_places": ["eventOut", "permit"],
        "assume_atomic_firing": True,
    }
    assert own["initial_marking"] == {"permit": 1}
    assert own["environment_places"] == ["userIn"]
    assert str(own["environment_mode"]) == str(lp.arrivals(2, 2))
    assert own["sink_places"] == ["eventOut", "permit"]
    assert own["sink_places_when"] == {"permit": ["eventOut"]}
    assert own["assume_atomic_firing"] is True


def test_k_replaces_the_arrival_bounds() -> None:
    env = {"options": {"environment": {"userIn": {"arrivals": 2}}}}
    [o] = options({**env, "claims": ["deadlock_free"]}, k=4)
    assert str(o["environment_mode"]) == str(lp.arrivals(0, 4))
    [b] = options(
        {"options": {"environment": {"userIn": {"bounded": 2}}}, "claims": ["deadlock_free"]}, k=4
    )
    assert str(b["environment_mode"]) == str(lp.bounded(2))
