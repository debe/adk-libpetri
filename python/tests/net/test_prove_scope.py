"""What a ``prove:`` claim covers: turns, environment places, atomicity, sinks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from google.adk.agents.config_agent_utils import from_config
from google.adk.workflow import FunctionNode

from adk_libpetri.net import BlueprintError, PetriNet, parse_blueprint
from adk_libpetri.net.proofs import (
    TURN_NEXT,
    claim_options,
    claim_spec,
    quiet_place,
    turn_spec,
)
from support.smt_proofs import requires_z3

from .conftest import BLUEPRINTS

TURNS = BLUEPRINTS / "bp_turns"


def load(path: Path) -> PetriNet:
    node = from_config(str(path))
    assert isinstance(node, PetriNet)
    return node


def verdicts(node: PetriNet, k: int | None = None) -> dict[str, str]:
    return {p.label: p.result.verdict for p in node.verify(k)}


def _fn(name: str) -> FunctionNode:
    def f(node_input: Any = None) -> str:
        return name

    f.__name__ = name
    return FunctionNode(func=f)


# ----------------------------------------------------------------------------
#  Turns
# ----------------------------------------------------------------------------


@requires_z3
def test_deadlock_freedom_covers_the_second_turn() -> None:
    # The first turn spends the only permit: one turn is fine, the second hangs.
    node = load(TURNS / "permit_once.yaml")
    proofs = {p.label: p for p in node.verify()}
    assert proofs["deadlock_free"].violated
    assert TURN_NEXT in (proofs["deadlock_free"].result.counterexample_transitions or [])
    assert proofs["deadlock_free"].scope.startswith("2 turns, each input after the previous")
    assert proofs["one turn"].proven
    assert proofs["one turn"].scope.startswith("userIn: the 1 token(s) initial_marking seeds")


@requires_z3
def test_a_node_after_the_answer_is_covered_across_turns() -> None:
    assert verdicts(load(TURNS / "after_turn.yaml")) == {
        "deadlock_free": "proven",
        "place_bound(permit, 1)": "proven",
    }


def test_the_turn_net_waits_for_node_runs_and_reads_delays_as_immediate() -> None:
    bp = load(TURNS / "after_turn.yaml").blueprint
    spec = turn_spec(bp)
    assert spec is not None
    names = set(spec.transition_names)
    assert {"After_Later", "complete:After_Later:run", TURN_NEXT} <= names
    nxt = spec.transition(TURN_NEXT)
    assert [p.name for p in nxt.inhibitors] == ["inflight:After_Later:run"]
    [deadlock, _] = bp.proof.claims
    assert all(t.timing.kind == "immediate" for t in claim_spec(bp, deadlock).transitions)


def test_each_node_run_holds_a_quiet_token_of_its_own() -> None:
    # One shared token would make every node run depend on every other, and the
    # verifier's partial-order reduction (VER-024) could prune no interleaving.
    bp = load(TURNS / "approval.yaml").blueprint
    spec = turn_spec(bp)
    assert spec is not None
    quiets = {t: quiet_place(t) for t in bp.node_transitions}
    assert len(quiets) > 1
    for t, q in quiets.items():
        users = {u.name for u in spec.transitions if q in {p.name for p in u.places()}}
        assert users == {t, TURN_NEXT}
    assert {p.name for p in spec.transition(TURN_NEXT).reads} == set(quiets.values())
    [claim] = bp.proof.claims
    marking = claim_options(bp, claim, k=2)["initial_marking"]
    assert all(marking[q] == 1 for q in quiets.values())


@requires_z3
def test_a_node_runs_deposit_is_not_split_again() -> None:
    # Gate_Start resets what the Gate_Draft node puts down, so VER-004 splits
    # the transition that deposits it. Over two turns that is the node run's
    # completion step, already the deposit: only its start may be split.
    node = load(TURNS / "approval.yaml")
    [proof] = node.verify(k=2)
    line = next(
        line for line in proof.result.report.splitlines() if line.startswith("In-flight actions")
    )
    split = line.removeprefix("In-flight actions (VER-004): ").split(" are verified")[0].split(", ")
    assert "Gate_Draft" in split
    # Only the net's own transitions are split, never a step the turn model added.
    assert set(split) <= set(node.blueprint.spec.transition_names)


# ----------------------------------------------------------------------------
#  The environment
# ----------------------------------------------------------------------------


@requires_z3
def test_env_places_and_turn_abort_arrive_in_every_proof() -> None:
    # cancel is an env: place; turnAbort is signalled by the runner on a failure.
    assert verdicts(load(TURNS / "envdrop.yaml")) == {
        "never cancelled": "violated",
        "never aborted": "violated",
    }


def test_an_initial_marking_keeps_the_other_places_arriving() -> None:
    node = load(TURNS / "envdrop.yaml")
    cancelled, aborted = node.blueprint.proof.claims
    seeded = claim_options(node.blueprint, cancelled)
    assert seeded["initial_marking"] == {"userIn": 1}
    assert seeded["environment_places"] == ["cancel", "turnAbort"]
    assert claim_options(node.blueprint, aborted)["environment_places"] == ["cancel", "turnAbort"]


@requires_z3
def test_a_no_op_initial_marking_gives_the_default_verdict() -> None:
    node = load(TURNS / "approval.yaml")
    [claim] = node.blueprint.proof.claims
    assert node.verify()[0].violated  # an approval can arrive
    data = {k: getattr(node, k) for k in ("places", "transitions", "env")}
    prove = {
        "options": {"sinks": ["eventOut", "decided", "drafted"], "initial_marking": {"decided": 0}},
        "claims": [{"place_bound": {"place": "approvedText", "bound": 0}}],
    }
    nodes = {n: _fn(n) for n in ("draft", "decide", "escalate_msg")}
    bp = parse_blueprint("approval", {**data, "prove": prove}, nodes=nodes)
    assert claim_options(bp, bp.proof.claims[0])["environment_places"] == ["approval"]
    assert claim.label == "never approved"


def test_an_environment_that_leaves_out_an_env_place_is_a_load_error() -> None:
    data = {
        "env": ["approval"],
        "places": {"approval": {}, "approved": {}},
        "transitions": {
            "A_Answer": {"in": ["userIn"], "out": "eventOut", "action": "emit"},
            "A_Approve": {"in": ["approval"], "out": "approved"},
        },
        "prove": {
            "claims": [
                {
                    "place_bound": {"place": "approved", "bound": 0},
                    "options": {"environment": {"userIn": {"arrivals": [1, 1]}}},
                }
            ]
        },
    }
    with pytest.raises(BlueprintError) as err:
        parse_blueprint("gate", data)
    assert err.value.path == "prove.claims[0].options.environment"
    assert "['approval']" in err.value.message
    assert err.value.hint is not None
    assert "userIn: {arrivals: [1, 1]}, approval: {arrivals: [1, 1]}" in err.value.hint


def test_the_one_mode_error_shows_the_yaml_forms() -> None:
    data = {
        "env": ["approval"],
        "places": {"approval": {}},
        "transitions": {
            "A_Answer": {"in": ["userIn", "approval"], "out": "eventOut", "action": "emit"}
        },
        "prove": {
            "options": {
                "environment": {"userIn": {"arrivals": [1, 1]}, "approval": {"arrivals": 1}}
            },
            "claims": ["deadlock_free"],
        },
    }
    with pytest.raises(BlueprintError) as err:
        parse_blueprint("gate", data)
    assert "userIn: {arrivals: [1, 1]}, approval: {arrivals: 1}" in err.value.message
    assert "('arrivals'" not in str(err.value)


def test_k_on_a_closed_claim_says_it_has_no_effect() -> None:
    proofs = load(TURNS / "permit_once.yaml").verify(k=3)
    one = next(p for p in proofs if p.label == "one turn")
    assert any("does not change userIn" in n for n in one.notes)


# ----------------------------------------------------------------------------
#  Atomic firing
# ----------------------------------------------------------------------------


def test_assume_atomic_firing_on_a_net_with_nodes_needs_the_authors_word() -> None:
    with pytest.raises(BlueprintError) as err:
        load_atomic({"assume_atomic_firing": True})
    assert err.value.path == "prove.claims[0].options.assume_atomic_firing"
    assert "Atomic_RunA" in err.value.message
    node = load_atomic({"assume_atomic_firing": True, "assume_atomic_nodes": True})
    assert node.blueprint.asynchronous == ("Atomic_RunA", "Atomic_RunB")


@requires_z3
def test_the_atomic_assumption_is_named_where_it_is_made() -> None:
    node = load_atomic({"assume_atomic_firing": True, "assume_atomic_nodes": True})
    [proof] = node.verify()
    assert any("node runs included" in n for n in proof.notes)
    [honest] = load(TURNS / "atomic.yaml").verify()
    assert honest.violated  # both runs start before either puts `done` down


def load_atomic(options: dict[str, Any]) -> PetriNet:
    import yaml

    data = yaml.safe_load((TURNS / "atomic.yaml").read_text())
    data["prove"]["claims"][0]["options"] = options
    fields = {k: data[k] for k in ("places", "transitions", "prove")}
    return PetriNet(name="atomic_net", nodes=[[_fn("slow")]], **fields)  # pyright: ignore[reportArgumentType]


# ----------------------------------------------------------------------------
#  Sinks and claim names in composed nets
# ----------------------------------------------------------------------------


def test_a_mounted_llm_agents_resting_places_are_default_sinks() -> None:
    bp = load(BLUEPRINTS / "bp_llm" / "root.yaml").blueprint
    assert bp.rest == ("eventOut", "assistant/turnPermit", "assistant/transfer")
    bp2 = parse_blueprint(
        "assistant_net", {"subnets": bp_llm_subnets(), "prove": DEADLOCK}, nodes=LLM
    )
    assert claim_options(bp2, bp2.proof.claims[0])["sink_places"][:3] == list(bp.rest)


@requires_z3
def test_a_mounted_llm_agent_is_deadlock_free_for_a_turn_with_the_default_sinks() -> None:
    from adk_libpetri.net import verify_blueprint

    bp = parse_blueprint(
        "assistant_net", {"subnets": bp_llm_subnets(), "prove": DEADLOCK}, nodes=LLM
    )
    [one] = verify_blueprint(bp, k=1)
    assert one.proven, one.result.report
    # Two turns: a transfer ends the first turn with nothing on eventOut, and
    # a PetriNet turn ends on eventOut only, so the second never comes.
    [two] = verify_blueprint(bp)
    assert two.violated
    assert (two.result.counterexample_transitions or [])[-1] == "assistant/LlmAgent_EmitTransfer"


DEADLOCK = {"claims": ["deadlock_free"]}


def bp_llm_subnets() -> dict[str, Any]:
    bind = {"userIn": "userIn", "eventOut": "eventOut"}
    return {"assistant": {"stock": "llm_agent", "from": "helper", "bind": bind}}


def _llm_nodes() -> dict[str, Any]:
    from google.adk.agents.llm_agent import LlmAgent

    from support.fake_llm import ScriptedLlm, text

    return {"helper": LlmAgent(name="helper", model=ScriptedLlm.of(text("x")), instruction="x")}


LLM = _llm_nodes()


def test_a_claim_on_a_childs_place_by_net_name_or_bound_port_gets_a_hint() -> None:
    import yaml

    data = yaml.safe_load((BLUEPRINTS / "bp_compose" / "twice.yaml").read_text())
    child = load(BLUEPRINTS / "bp_compose" / "race.yaml")
    nodes = {"two_way_race": child, "text_of": _fn("text_of"), "join": _fn("join")}
    base = {k: data[k] for k in ("places", "subnets", "transitions")}

    def claim_error(place: str) -> BlueprintError:
        prove = {"claims": [{"place_bound": {"place": place, "bound": 1}}]}
        with pytest.raises(BlueprintError) as err:
            parse_blueprint("double_race", {**base, "prove": prove}, nodes=nodes)
        return err.value

    by_net = claim_error("two_way_race/won")
    assert by_net.hint is not None and "first/won, second/won" in by_net.hint
    bound = claim_error("first/question")
    assert bound.hint == "port 'question' of 'first' is bound to 'q1': name that place"


def test_no_transition_may_test_eventout() -> None:
    data = {
        "places": {"done": {}},
        "transitions": {
            "A_Answer": {"in": ["userIn"], "out": "eventOut", "action": "emit"},
            "A_Once": {"in": ["userIn"], "inhibit": ["eventOut"], "out": "done"},
        },
    }
    with pytest.raises(BlueprintError) as err:
        parse_blueprint("n", data)
    assert err.value.path == "transitions.A_Once.inhibit"


@requires_z3
def test_the_petri_net_docstring_example_parses_and_proves() -> None:
    import re
    import textwrap

    import yaml

    from adk_libpetri.net import node as node_module
    from adk_libpetri.net import verify_blueprint

    doc = node_module.__doc__ or ""
    block = re.search(r"::\n\n(.*?)\n\n``adk web``", doc, re.S)
    assert block is not None
    data = yaml.safe_load(textwrap.dedent(block.group(1)))
    nodes = {n: _fn(n) for n in ("fast_answer", "slow_answer")}
    body = {k: data[k] for k in ("places", "transitions", "prove")}
    proofs = verify_blueprint(parse_blueprint(data["name"], body, nodes=nodes))
    assert [p.result.verdict for p in proofs] == ["proven", "proven"]
