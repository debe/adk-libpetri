"""ν-joins in a blueprint: ``match:`` joins tokens by name, ``mint:`` makes the names.

``bp_nu_join`` is the timed escalation with names: each turn mints one, a
reply joins its own turn's clock by it, and a reply after the escalation
joins the retired name and is dropped.
"""

from __future__ import annotations

import dataclasses
from typing import Any, cast

import libpetri as lp
import yaml
from google.adk.agents.config_agent_utils import from_config
from google.adk.workflow import FunctionNode

from adk_libpetri.net import NodeError, PetriNet, parse_blueprint
from adk_libpetri.net.blueprint import Blueprint, Claim, NetScope, _take
from adk_libpetri.net.counterexample import step_lines, steps
from adk_libpetri.net.proofs import (
    TURNS_LEFT,
    Names,
    _flows,
    claim_options,
    names,
    split_nodes,
    turn_spec,
    verify_blueprint,
)
from adk_libpetri.net.report import _claim
from support.smt_proofs import requires_z3

from ._harness import answers, runner_of, session, settled, text_of
from .conftest import BLUEPRINTS, Serve
from .test_blueprint_parse import NODES

NU = BLUEPRINTS / "bp_nu_join"


def load(serve: Serve) -> PetriNet:
    node = from_config(str(NU / "root.yaml"))
    assert isinstance(node, PetriNet)
    return serve(node)


def test_match_keys_each_input_by_where_its_token_holds_the_name() -> None:
    data: dict[str, Any] = {
        "places": {"s": {"type": "str"}, "e": {"type": "NodeError"}, "out": {"type": "NodeError"}},
        "transitions": {
            "T_Join": {"in": ["e", "s"], "match": {"e": "node", "s": "self"}, "out": "out"}
        },
    }
    bp = parse_blueprint("n", data)
    match = bp.spec.transition("T_Join").match
    assert match is not None
    (e, by_node), (s, by_self) = match.keys
    assert (e.name, s.name) == ("e", "s")
    assert by_node(NodeError("turn-1", "c", "m")) == "turn-1"
    assert by_self("turn-1") == "turn-1"
    # The str place is the join's witness: the move forwards the NodeError.
    plan = bp.plans["T_Join"]
    assert plan.witnesses == frozenset({"s"})


def run(data: dict[str, Any], **initial: list[Any]) -> lp.MarkingView:
    """Parse ``data`` (move and emit actions only) and run it from ``initial``."""
    bp = parse_blueprint("n", data)
    return lp.run_sync(bp.spec.build(bp.actions(NetScope())), initial=initial)


S = {"type": "str"}


def test_a_join_of_names_alone_forwards_the_name() -> None:
    data: dict[str, Any] = {
        "places": {"s": S, "t": S, "u": S},
        "transitions": {
            "T_Join": {
                "in": ["s", "t"],
                "match": {"s": "self", "t": "self"},
                "out": "u",
                "relay": {"u": "self"},
            }
        },
    }
    bp = parse_blueprint("n", data)
    match = bp.spec.transition("T_Join").match
    assert match is not None
    [(u, _)] = match.relay_to
    assert u.name == "u"
    # No other matched input carries the name, so the first witness stays in.
    assert bp.plans["T_Join"].witnesses == frozenset({"t"})
    final = run(data, s=["x", "y"], t=["y"])
    assert (final.tokens("u"), final.tokens("s"), final.tokens("t")) == (("y",), ("x",), ())


class _Ctx:
    """The ``Ctx`` methods :func:`_take` calls, over fixed tokens."""

    def __init__(self, tokens: dict[str, Any]) -> None:
        self.tokens = tokens

    def input(self, p: Any) -> Any:
        return self.tokens[p.name]

    inputs = read = input


def value_of(decl: dict[str, Any], **tokens: Any) -> Any:
    """The value a node of transition ``decl`` is handed for ``tokens``."""
    places = {"s": S, "t": S, "u": S, "w": S, "cfg": S, "e": {"type": "NodeError"}}
    data = {"places": places, "transitions": {"T_J": {**decl, "out": "u", "node": "f"}}}
    plan = parse_blueprint("n", data, nodes=NODES).plans["T_J"]
    return _take(cast(Any, _Ctx(tokens)), plan, with_reads=True)


def test_a_join_hands_its_node_the_name_when_only_witnesses_hold_it() -> None:
    selves = {"in": ["s", "t"], "match": {"s": "self", "t": "self"}}
    # A read is no matched input: the name stays in next to it.
    assert value_of({**selves, "read": ["cfg"]}, s="n1", t="n1", cfg="c") == {
        "s": "n1",
        "cfg": "c",
    }
    # Nor is an input the match does not key.
    assert value_of({**selves, "in": ["s", "t", "w"]}, s="n1", t="n1", w="v") == {
        "s": "n1",
        "w": "v",
    }
    # A matched input with the name in a field carries it: the witness leaves.
    err = NodeError("n1", "c", "m")
    by_node = {"in": ["e", "s"], "match": {"e": "node", "s": "self"}, "read": ["cfg"]}
    assert value_of(by_node, e=err, s="n1", cfg="c") == {"e": err, "cfg": "c"}


def test_a_self_input_of_a_count_is_no_witness() -> None:
    decl = {"in": ["s", {"place": "t", "count": 2}], "match": {"s": "self", "t": "self"}}
    # t's list holds the name, so s leaves the value and t's tokens stay in.
    assert value_of(decl, s="n1", t=["n1", "n1"]) == ["n1", "n1"]


def test_when_only_self_inputs_match_the_first_in_in_stays() -> None:
    decl = {"in": ["t", "s"], "match": {"s": "self", "t": "self"}, "out": "u", "node": "f"}
    data = {"places": {"s": S, "t": S, "u": S}, "transitions": {"T_J": decl}}
    plan = parse_blueprint("n", data, nodes=NODES).plans["T_J"]
    # The order of in:, not of match:, picks the one that stays.
    assert plan.witnesses == frozenset({"s"})
    assert value_of(decl, s="n1", t="n1") == "n1"


def test_a_join_picks_its_partner_by_name() -> None:
    work = "bp_nu_join.agent.Work"
    data: dict[str, Any] = {
        "places": {
            "reply": {"type": work},
            "waiting": S,
            "retired": S,
            "answered": {"type": work},
            "dropped": {"type": work},
        },
        "transitions": {
            "Nu_Reply": {
                "in": ["reply", "waiting"],
                "match": {"reply": "turn", "waiting": "self"},
                "out": "answered",
                "priority": 10,
            },
            "Nu_DropLate": {
                "in": ["reply", "retired"],
                "match": {"reply": "turn", "retired": "self"},
                "out": "dropped",
            },
        },
    }
    from bp_nu_join.agent import Work

    late, fresh = Work("late", "n1"), Work("fresh", "n2")
    # The late reply is first in the queue, and Nu_Reply has the higher priority:
    # only the names keep it from answering turn n2.
    final = run(data, reply=[late], waiting=["n2"], retired=["n1"])
    assert (final.tokens("answered"), final.tokens("dropped")) == ((), (late,))
    assert final.tokens("waiting") == ("n2",)
    final = run(data, reply=[late, fresh], waiting=["n2"], retired=["n1"])
    assert (final.tokens("answered"), final.tokens("dropped")) == ((fresh,), (late,))


def test_a_mint_from_a_unit_trigger_forks_one_fresh_name() -> None:
    data: dict[str, Any] = {
        "places": {"go": {}, "a": S, "b": S},
        "transitions": {"T_Fork": {"in": ["go"], "out": {"and": ["a", "b"]}, "mint": ["a", "b"]}},
    }
    final = run(data, go=[None])
    [a], [b] = final.tokens("a"), final.tokens("b")
    assert a == b and a.startswith("T_Fork#")


NAMES = "bp_nu_join.names"


def test_names_in_nested_optional_and_pydantic_fields() -> None:
    order = {"type": f"{NAMES}.Order"}
    places = {"o": order, "o2": order, "s": S, "pj": {"type": f"{NAMES}.PJob"}}
    places |= {
        "pj2": {"type": f"{NAMES}.PJob"},
        "p": {"type": f"{NAMES}.Priced"},
        "p2": {"type": f"{NAMES}.Priced"},
    }
    data: dict[str, Any] = {
        "places": places,
        "transitions": {
            # A nested key, relayed on the same path.
            "T_Job": {
                "in": ["o", "s"],
                "match": {"o": "job.id", "s": "self"},
                "out": "o2",
                "relay": {"o2": "job.id"},
            }
        },
    }
    from bp_nu_join.names import Held, Job, Order, PJob

    keyed = Order(Job("n1"))
    final = run(data, o=[keyed], s=["n0", "n1"])
    assert (final.tokens("o2"), final.tokens("s")) == ((keyed,), ("n0",))

    # A key through a Job | None step: a None job has no name, so it never joins.
    held = {"type": f"{NAMES}.Held"}
    data["places"] |= {"h": held, "h2": held}
    data["transitions"] = {
        "T_Held": {
            "in": ["h", "s"],
            "match": {"h": "job.id", "s": "self"},
            "out": "h2",
            "relay": {"h2": "job.id"},
        }
    }
    final = run(data, h=[Held(), Held(Job("n1"))], s=["n1"])
    assert (final.tokens("h2"), final.tokens("h")) == ((Held(Job("n1")),), (Held(),))

    # A str | None field is a name, and so is a str field next to an annotation
    # only a type checker resolves.
    data["transitions"] = {
        "T_Note": {"in": ["o", "s"], "match": {"o": "note", "s": "self"}, "out": "o2"},
        "T_Priced": {"in": ["p", "s"], "match": {"p": "turn", "s": "self"}, "out": "p2"},
    }
    plans = parse_blueprint("n", data).plans
    assert plans["T_Note"].witnesses == plans["T_Priced"].witnesses == frozenset({"s"})

    # mint: as one string, into a pydantic field and into a nested dataclass field.
    data["transitions"] = {
        "T_P": {"in": ["pj"], "out": "pj2", "mint": "pj2.id"},
        "T_O": {"in": ["o"], "out": "o2", "mint": ["o2.job.id"]},
    }
    final = run(data, pj=[PJob()], o=[Order(Job("old", 3), "x")])
    [pj], [o] = final.tokens("pj2"), final.tokens("o2")
    assert isinstance(pj, PJob) and pj.id.startswith("T_P#")
    assert o.job.id.startswith("T_O#") and (o.job.n, o.note) == (3, "x")


async def test_an_answer_in_time_joins_its_own_turns_name(serve: Serve) -> None:
    from bp_nu_join import agent

    agent.SEEN.clear()
    node = load(serve)
    s = await session(node)
    turn = await s.say("fast")
    assert turn.error is None, turn.error
    [answer] = answers(turn.events, "nu_join")
    assert answer.output == agent.Work("answer to fast", agent.SEEN[0])
    assert agent.SEEN[0]  # a minted name, not the open_work default ""
    snap = await settled(runner_of(node, s), lambda m: not m.action_in_flight)
    left = {p.name: snap.marking.count(p.name) for p in node.spec.places}
    assert {p: n for p, n in left.items() if n} == {"eventOut": 1}


async def test_a_late_reply_joins_the_retired_name_and_is_dropped(serve: Serve) -> None:
    from bp_nu_join import agent

    agent.SEEN.clear()
    agent.RELEASE.clear()
    node = load(serve)
    s = await session(node)
    try:
        turn = await s.say("slow")
    finally:
        # The turn ended on the escalation's answer, so the reply comes after it.
        agent.RELEASE.set()
    assert turn.error is None, turn.error
    [answer] = answers(turn.events, "nu_join")
    assert text_of(answer) == "a specialist will follow up"
    snap = await settled(
        runner_of(node, s), lambda m: m.marking.count("dropped") and not m.action_in_flight
    )
    [dropped] = snap.marking.tokens("dropped")
    assert dropped == agent.Work("answer to slow", agent.SEEN[0])
    for p in ("reply", "retired", "waiting", "work"):
        assert snap.marking.count(p) == 0, p
    # The next turn mints a name of its own and answers in time.
    again = await s.say("fast")
    assert again.error is None, again.error
    [answer] = answers(again.events, "nu_join")
    assert answer.output == agent.Work("answer to fast", agent.SEEN[1])
    assert agent.SEEN[1] != agent.SEEN[0]


def test_typed_ids_are_names_and_a_newtype_of_str_takes_a_mint() -> None:
    from bp_nu_join.names import Lane, Sid, Tid, Typed

    typed = {"type": f"{NAMES}.Typed"}
    token = Typed(Sid("a"), Tid("a"), Lane.A, "a")
    # A str subclass, a NewType of str, a StrEnum and a Literal of strs all join.
    for name in ("sid", "tid", "lane", "kind"):
        data: dict[str, Any] = {
            "places": {"w": typed, "s": S, "w2": typed},
            "transitions": {
                "T_J": {"in": ["w", "s"], "match": {"w": name, "s": "self"}, "out": "w2"}
            },
        }
        final = run(data, w=[token], s=["q", "a"])
        assert (final.tokens("w2"), final.tokens("s")) == ((token,), ("q",)), name
    # fresh_name()'s plain str is a Tid, so a mint can write one.
    data = {
        "places": {"w": typed, "w2": typed},
        "transitions": {"T_M": {"in": ["w"], "out": "w2", "mint": ["w2.tid"]}},
    }
    [minted] = run(data, w=[token]).tokens("w2")
    assert minted.tid.startswith("T_M#") and minted.sid == "a"


async def test_a_mounted_mint_and_match_work_on_the_prefixed_places(serve: Serve) -> None:
    node = from_config(str(NU / "mount.yaml"))
    assert isinstance(node, PetriNet)
    node = serve(node)
    match = node.spec.transition("first/Pair_Join").match
    assert match is not None
    assert [p.name for p, _ in match.keys] == ["first/tag", "first/held"]
    s = await session(node)
    turn = await s.say("go")
    assert turn.error is None, turn.error
    [answer] = answers(turn.events, "nu_mount")
    r1, r2 = text_of(answer).split()
    # Each instance joined the name its own Pair_Open minted, a fresh one each.
    assert r1 and r2 and r1 != r2
    snap = await settled(runner_of(node, s), lambda m: not m.action_in_flight)
    for inst in ("first", "second"):
        assert snap.marking.count(f"{inst}/tag") == snap.marking.count(f"{inst}/held") == 0


# -- proofs ----------------------------------------------------------------


def nu_variant(
    places: dict[str, Any] | None = None,
    turn: dict[str, Any] | None = None,
    *,
    env: list[str] | None = None,
    claims: list[Any] | None = None,
    **transitions: Any,
) -> Blueprint:
    """``bp_nu_join`` with ``places`` added, ``turn``, ``env`` and ``claims`` set
    and ``transitions`` laid over its own (``None`` drops one)."""
    from bp_nu_join import agent

    data = yaml.safe_load((NU / "root.yaml").read_text())
    data["places"].update(places or {})
    if turn is not None:
        data["turn"] = turn
    if env is not None:
        data["env"] = env
    if claims is not None:
        data["prove"]["claims"] = claims
    for t, decl in transitions.items():
        if decl is None:
            del data["transitions"][t]
        else:
            data["transitions"][t] = decl
    body = {k: v for k, v in data.items() if k not in ("agent_class", "name", "nodes")}
    fns = (agent.open_work, agent.specialist, agent.canned_reply)
    nodes = {f.__name__: FunctionNode(func=f) for f in fns}
    return parse_blueprint("nu_join", body, nodes=nodes, package="bp_nu_join")


RELAY_NOTE = (
    "relay contract (NU-051) assumed for node Nu_Ask: a run puts the name it took into each "
    "place it marks that carries one, in the same field"
)


def test_the_names_are_the_mints_the_carriers_between_and_the_relay_nodes() -> None:
    bp = load_bp()
    assert names(bp) == Names(("Nu_Start",), ("work",), ("Nu_Ask",), (RELAY_NOTE,))
    deadlock, *_ = bp.proof.claims
    two = claim_options(bp, deadlock)
    assert (two["fragment_mode"], two["carrier_places"]) == ("extended", ["work"])
    # Over two turns Nu_Start is split, and its deposit is what mints.
    assert two["mint_transitions"] == ["complete:Nu_Start:run"]
    assert claim_options(bp, deadlock, 1)["mint_transitions"] == ["Nu_Start"]
    # Nu_Ask's run carries the name, which libpetri cannot split: it stays whole.
    spec = turn_spec(bp)
    assert spec is not None
    assert "complete:Nu_Start:run" in spec.transition_names
    assert "complete:Nu_Ask:run" not in spec.transition_names
    assert "turn:quiet:Nu_Ask" not in two["initial_marking"]
    assert "turn:quiet:Nu_Ask" not in two["sink_places"]


def test_a_net_without_a_match_passes_no_names() -> None:
    bp = nu_variant(Nu_Reply=None, Nu_DropLate=None)
    assert names(bp) is None
    assert "mint_transitions" not in claim_options(bp, bp.proof.claims[0])


def load_bp() -> Blueprint:
    node = from_config(str(NU / "root.yaml"))
    assert isinstance(node, PetriNet)
    return node.blueprint


@requires_z3
def test_no_late_reply_is_stranded_over_one_turn_and_two() -> None:
    bp = load_bp()
    for k in (1, None):
        proofs = verify_blueprint(bp, k)
        assert [(p.label, p.proven) for p in proofs] == [
            ("deadlock_free", True),
            ("no reply is stranded", True),
            ("no retired name is stranded", True),
        ], [p.result.reason or p.result.report for p in proofs]
        # Decided name by name, not by the name-blind over-approximation.
        assert all("Route B" in p.result.report for p in proofs)
        assert all(RELAY_NOTE in p.notes for p in proofs)


@requires_z3
def test_without_the_drop_a_late_reply_is_stranded() -> None:
    bp = nu_variant(Nu_DropLate=None)
    deadlock, reply, retired = verify_blueprint(bp, 1)
    assert reply.violated and retired.violated and deadlock.violated
    assert "Route B" in reply.result.report
    result = _claim("nu_join", reply, bp.proof.claims[1])
    assert result.markings[-1].get("reply") and result.markings[-1].get("retired")
    assert "at rest with a token left on reply" in step_lines(result)[-1]
    ss = steps(result)
    assert ss[-1].bad == {"reply"}
    assert not any(s.bad for s in ss[:-1])  # the claim breaks at rest: the last step only


@requires_z3
def test_without_the_drop_a_late_reply_is_stranded_over_two_turns() -> None:
    proofs = verify_blueprint(nu_variant(Nu_DropLate=None), None)
    assert [(p.label, p.violated) for p in proofs] == [
        ("deadlock_free", True),
        ("no reply is stranded", True),
        ("no retired name is stranded", True),
    ], [p.result.reason or p.result.report for p in proofs]
    assert all("Route B" in p.result.report for p in proofs)


@requires_z3
def test_a_mint_that_leaves_a_key_without_a_fresh_name_proves_nothing() -> None:
    # Nu_Start puts waiting down unminted: its token is no name Nu_Reply can join by.
    start = {
        "in": ["userIn"],
        "out": {"and": ["work", "waiting"]},
        "node": "open_work",
        "mint": ["work.turn"],
    }
    bp = nu_variant(Nu_Start=start)
    nu = names(bp)
    assert nu is not None and nu.mints == ()
    assert any("Nu_Start is not declared to mint" in n for n in nu.notes)
    assert any("add waiting to its mint:" in n for n in nu.notes)
    assert not any(p.proven for p in verify_blueprint(bp, 1))


NO_MINT = (
    "Nu_Start has no mint:, yet its tokens reach the join keys reply, retired, waiting: they "
    "hold no fresh name, so the joins cannot tell them apart and the claims stay unknown "
    "(libpetri's reason may also name a node that only passes the name on, which needs no "
    "mint:); add mint: [waiting, work.turn] to Nu_Start"
)


def test_a_net_that_forgets_mint_names_the_transition_to_mint_in() -> None:
    # libpetri's reason names Nu_Ask too, which only relays the name: the note
    # points at Nu_Start alone.
    start = {"in": ["userIn"], "out": {"and": ["work", "waiting"]}, "node": "open_work"}
    bp = nu_variant(Nu_Start=start)
    assert names(bp) == Names((), (), (), (NO_MINT,))
    if lp.z3_available():
        proofs = verify_blueprint(bp, 1)
        assert [p.result.verdict for p in proofs] == ["unknown"] * 3
        assert all(p.notes == (NO_MINT,) for p in proofs)


def test_a_missing_mint_is_asked_of_the_first_transition_whose_tokens_hold_a_name() -> None:
    # Nu_In starts the tokens but puts a Content down, which cannot hold a name:
    # the note asks the mint of Nu_Start, and that line loads.
    unminted = {"in": ["msg"], "out": {"and": ["work", "waiting"]}, "node": "open_work"}
    bp = nu_variant(
        {"msg": {"type": "Content"}},
        Nu_In={"in": ["userIn"], "out": "msg"},
        Nu_Start=unminted,
    )
    nu = names(bp)
    assert nu is not None
    assert nu.notes == (
        NO_MINT.replace("Nu_Start has no", "Nu_In has no").replace(
            "add mint: [waiting, work.turn] to Nu_Start",
            "Nu_In's tokens cannot hold a name, so add mint: [waiting, work.turn] to "
            "Nu_Start, which takes them on toward the keys",
        ),
    )
    fixed = nu_variant(
        {"msg": {"type": "Content"}},
        Nu_In={"in": ["userIn"], "out": "msg"},
        Nu_Start={**unminted, "mint": ["waiting", "work.turn"]},
    )
    assert names(fixed) == Names(("Nu_Start",), ("work",), ("Nu_Ask",), (RELAY_NOTE,))


def test_a_missing_mint_no_output_can_hold_is_not_given_a_line() -> None:
    # Nu_Start puts a Content on msg and a str on waiting: msg cannot take a
    # mint, and no one transition takes both on, so no mint: line would load.
    bp = nu_variant(
        {"msg": {"type": "Content"}},
        Nu_Start={"in": ["userIn"], "out": {"and": ["msg", "waiting"]}, "node": "open_work"},
        Nu_Open={"in": ["msg"], "out": "work", "node": "open_work"},
    )
    nu = names(bp)
    assert nu is not None
    assert nu.notes == (
        NO_MINT.replace(
            "add mint: [waiting, work.turn] to Nu_Start",
            "add mint: to the first transition that writes the name into a str place "
            "or a str field",
        ),
    )


@requires_z3
def test_a_relay_whose_plain_output_is_tested_proves_nothing() -> None:
    # Nu_Ask stays whole, but Nu_Late inhibits its unit output: libpetri would
    # split it (VER-004) and refuses, since it writes a name-carrying place.
    bp = nu_variant(
        {"asked": {}, "late": {}},
        Nu_Ask={"in": ["work"], "out": {"and": ["reply", "asked"]}, "node": "specialist"},
        Nu_Late={"in": ["due"], "inhibit": ["asked"], "out": "late"},
    )
    assert "Nu_Ask" not in split_nodes(bp)
    for k in (1, None):
        proofs = verify_blueprint(bp, k)
        assert not any(p.proven for p in proofs)
        for p in proofs:
            reason = p.result.reason or ""
            assert reason.startswith("transition 'Nu_Ask'") and "VER-004" in reason, reason


def test_a_permit_holds_no_name() -> None:
    # The permit comes back from Nu_Escalate, which takes a name: a unit token
    # holds none, so the permit is no carrier and Nu_Start stays a mint.
    bp = nu_variant(
        {"permit": {"seed": 1}},
        Nu_Start={
            "in": ["userIn", "permit"],
            "out": {"and": ["work", "waiting"]},
            "node": "open_work",
            "mint": ["work.turn", "waiting"],
        },
        Nu_Escalate={
            "in": ["waiting"],
            "out": {"and": ["retired", "due", "permit"]},
            "timing": {"delayed": 2000},
        },
    )
    assert names(bp) == Names(("Nu_Start",), ("work",), ("Nu_Ask",), (RELAY_NOTE,))
    if lp.z3_available():
        _, reply, retired = verify_blueprint(bp, 1)
        assert reply.proven and retired.proven, [reply.result.reason, retired.result.reason]
        assert "Route B" in reply.result.report and "Route B" in retired.result.report


def test_a_node_join_is_left_whole_and_no_relay_is_assumed_for_it() -> None:
    bp = nu_variant(
        Nu_DropLate={
            "in": ["reply", "retired"],
            "match": {"reply": "turn", "retired": "self"},
            "out": "fallback",
            "node": "canned_reply",
        }
    )
    nu = names(bp)
    assert nu is not None
    assert (nu.relays, nu.notes) == (("Nu_Ask", "Nu_DropLate"), (RELAY_NOTE,))
    assert "Nu_DropLate" not in split_nodes(bp)


HOP = {
    "places": {"work2": {"type": ".agent.Work"}},
    "Nu_Hop": {"in": ["work"], "out": "work2", "action": "move"},
    "Nu_Ask": {"in": ["work2"], "out": "reply", "node": "specialist"},
}


def test_a_name_is_carried_over_several_hops() -> None:
    # work2 is no mint target: only following the name from work through
    # Nu_Hop makes it a carrier, and Nu_Ask a relay.
    bp = nu_variant(**HOP)
    assert names(bp) == Names(("Nu_Start",), ("work", "work2"), ("Nu_Ask",), (RELAY_NOTE,))
    deadlock, *_ = bp.proof.claims
    assert claim_options(bp, deadlock)["carrier_places"] == ["work", "work2"]


@requires_z3
def test_a_name_carried_over_several_hops_is_proven() -> None:
    bp = nu_variant(**HOP)
    for k in (1, None):
        proofs = verify_blueprint(bp, k)
        assert all(p.proven for p in proofs), [p.result.reason or p.result.report for p in proofs]
        assert all("Route B" in p.result.report for p in proofs)


REMINT: dict[str, Any] = {
    "places": {"q1": S, "q2": S, "x": S, "y": S, "joined": S},
    "transitions": {
        "T_Ask": {"in": ["userIn"], "out": {"and": ["q1", "q2"]}, "mint": ["q1", "q2"]},
        "T_A": {"in": ["q1"], "out": "x", "mint": ["x"]},
        "T_B": {"in": ["q2"], "out": "y", "mint": ["y"]},
        "T_Join": {"in": ["x", "y"], "match": {"x": "self", "y": "self"}, "out": "joined"},
        "T_Emit": {"in": ["joined"], "out": "eventOut", "action": "emit"},
    },
    "prove": {
        "options": {"sinks": ["eventOut"]},
        "claims": [
            "deadlock_free",
            {"joined_or_dead_lettered": "x"},
            {"unreachable": ["joined"]},
        ],
    },
}


def test_a_name_a_mint_takes_does_not_pass_through_it() -> None:
    # T_A and T_B each put a fresh name down: q1 and q2 lead to no key.
    bp = parse_blueprint("n", REMINT)
    assert names(bp) == Names(("T_Ask", "T_A", "T_B"), (), ())
    final = run(REMINT, userIn=["go"])
    [x], [y] = final.tokens("x"), final.tokens("y")
    assert x != y and not final.tokens("joined")


@requires_z3
def test_two_fresh_names_never_join() -> None:
    deadlock, stranded, joined = verify_blueprint(parse_blueprint("n", REMINT), 1)
    assert deadlock.violated and stranded.violated, [deadlock.result.reason]
    assert joined.proven
    assert "Route B" in stranded.result.report


def text_key(bp_key: str) -> dict[str, Any]:
    return {
        "in": ["reply", "waiting"],
        "match": {"reply": bp_key, "waiting": "self"},
        "out": "eventOut",
        "priority": 10,
        "action": "emit",
    }


def test_a_key_on_another_field_than_the_name_declares_no_names() -> None:
    bp = nu_variant(Nu_Reply=text_key("text"))
    nu = names(bp)
    assert nu is not None and not nu.declared
    assert (nu.mints, nu.carriers) == ((), ())
    # Two keys read reply, each on its own field: each sees the other's.
    assert nu.notes == (
        "Nu_Reply keys reply by text, but the name reaches it at turn (as Nu_DropLate keys "
        "it): names are not declared, so libpetri cannot use its ν route",
        "Nu_DropLate keys reply by turn, but the name reaches it at text (as Nu_Reply keys "
        "it): names are not declared, so libpetri cannot use its ν route",
    )
    deadlock, *_ = bp.proof.claims
    assert "mint_transitions" not in claim_options(bp, deadlock, 1)
    if lp.z3_available():
        proofs = verify_blueprint(bp, 1)
        assert not any(p.proven or p.violated for p in proofs[:2]), [
            p.result.reason for p in proofs
        ]
    # With one key on reply, the field its name reaches it at is Nu_Start's mint.
    nu = names(nu_variant(Nu_Reply=text_key("text"), Nu_DropLate=None))
    assert nu is not None and not nu.declared
    assert nu.notes == (
        "Nu_Reply keys reply by text, but the name reaches it at turn (minted by Nu_Start): "
        "names are not declared, so libpetri cannot use its ν route",
    )


def test_a_mint_that_takes_a_name_declares_no_names() -> None:
    # Nu_Escalate takes the key waiting and mints retired: libpetri would read
    # it as relaying waiting's name, where the run writes a fresh one.
    escalate = {
        "in": ["waiting"],
        "out": {"and": ["retired", "due"]},
        "timing": {"delayed": 2000},
        "mint": ["retired"],
    }
    bp = nu_variant(Nu_Escalate=escalate)
    nu = names(bp)
    assert nu is not None and not nu.declared and nu.mints == ()
    assert any("Nu_Escalate takes the name on waiting and mints a fresh one" in n for n in nu.notes)
    if lp.z3_available():
        assert not any(p.proven for p in verify_blueprint(bp, 1))


def test_a_mint_whose_fresh_names_reach_no_key_is_no_nu_mint() -> None:
    # Nu_Escalate forwards waiting's name to retired and mints log, which no
    # join keys: libpetri reads it as a forward, and the names stay declared.
    escalate = {
        "in": ["waiting"],
        "out": {"and": ["retired", "due", "log"]},
        "timing": {"delayed": 2000},
        "mint": ["log"],
    }
    bp = nu_variant({"log": S}, Nu_Escalate=escalate)
    nu = names(bp)
    assert nu is not None and nu.declared
    assert (nu.mints, nu.carriers, nu.notes) == (("Nu_Start",), ("work",), (RELAY_NOTE,))
    if lp.z3_available():
        _, reply, retired = verify_blueprint(bp, 1)
        assert reply.proven and retired.proven, [reply.result.reason, retired.result.reason]


def test_an_error_branch_a_join_keys_is_no_place_to_mint_into() -> None:
    # Nu_Start's error branch marks failed with a NodeError, and Nu_Fail keys it:
    # mint: cannot name failed (the loader refuses it), so the note says why.
    start = {
        "in": ["userIn"],
        "out": {"xor": {"default": {"and": ["work", "waiting"]}, "error": "failed"}},
        "node": "open_work",
        "mint": ["work.turn", "waiting"],
    }
    fail = {
        "in": ["failed", "retired"],
        "match": {"failed": "node", "retired": "self"},
        "out": "oops",
    }
    bp = nu_variant(
        {"failed": {"type": "NodeError"}, "oops": {"type": "NodeError"}},
        Nu_Start=start,
        Nu_Fail=fail,
    )
    nu = names(bp)
    assert nu is not None and nu.mints == ()
    [note] = [n for n in nu.notes if n.startswith("Nu_Start is not declared to mint")]
    assert "its error branch or timeout marks failed" in note
    assert "a NodeError holds the node's name" in note
    assert "add failed to its mint:" not in note
    if lp.z3_available():
        proofs = verify_blueprint(bp, 1)
        assert not any(p.proven for p in proofs)
        assert all(note in p.notes for p in proofs)


def fork_join(keyed: bool, **j1: Any) -> Blueprint:
    """T_Fork mints a, b (and w); the join T_J1 of a and b mints z, which T_J2
    keys against w when ``keyed`` and T_Emit answers with otherwise."""
    fork = ["a", "b", "w"] if keyed else ["a", "b"]
    ts: dict[str, Any] = {
        "T_Fork": {"in": ["userIn"], "out": {"and": fork}, "mint": fork},
        "T_J1": {"in": ["a", "b"], "match": {"a": "self", "b": "self"}, "out": "z", **j1},
        "T_Emit": {"in": ["done" if keyed else "z"], "out": "eventOut", "action": "emit"},
    }
    if keyed:
        ts["T_J2"] = {"in": ["z", "w"], "match": {"z": "self", "w": "self"}, "out": "done"}
    stranded = "w" if keyed else "a"
    claims = ["deadlock_free", {"joined_or_dead_lettered": stranded}]
    places = {p: S for p in ("a", "b", "w", "z", "done")}
    return parse_blueprint("n", {"places": places, "transitions": ts, "prove": {"claims": claims}})


JOIN_REMINT = (
    "T_J1 is a join and mints into z, which carries a name a join keys: libpetri gives a "
    "join no mint role and reads that output as a re-mint (NU-010, NU-054), so the claims "
    "stay unknown; mint the new name in a plain transition after the join"
)


def test_a_join_that_mints_into_a_key_is_no_mint() -> None:
    bp = fork_join(True, mint=["z"])
    nu = names(bp)
    assert nu is not None and nu.declared
    assert (nu.mints, nu.notes) == (("T_Fork",), (JOIN_REMINT,))
    if lp.z3_available():
        proofs = verify_blueprint(bp, 1)
        assert [p.result.verdict for p in proofs] == ["unknown", "unknown"]
        assert all(JOIN_REMINT in p.notes for p in proofs)


@requires_z3
def test_a_join_may_mint_into_a_place_no_join_keys() -> None:
    bp = fork_join(False, mint=["z"])
    assert names(bp) == Names(("T_Fork", "T_J1"), (), ())
    for k in (1, None):
        proofs = verify_blueprint(bp, k)
        assert all(p.proven for p in proofs), [p.result.reason or p.result.report for p in proofs]


def test_a_join_that_relays_and_mints_is_a_mint() -> None:
    # T_J1 relays the joined name to r, which T_J2 keys against h, and mints z:
    # its relay target is no place it fails to mint.
    bp = parse_blueprint(
        "n",
        {
            "places": {p: S for p in ("a", "b", "z", "r", "h", "done")},
            "transitions": {
                "T_Fork": {
                    "in": ["userIn"],
                    "out": {"and": ["a", "b", "h"]},
                    "mint": ["a", "b", "h"],
                },
                "T_J1": {
                    "in": ["a", "b"],
                    "match": {"a": "self", "b": "self"},
                    "out": {"and": ["z", "r"]},
                    "relay": {"r": "self"},
                    "mint": ["z"],
                },
                "T_J2": {"in": ["r", "h"], "match": {"r": "self", "h": "self"}, "out": "done"},
                "T_Emit": {"in": ["done"], "out": "eventOut", "action": "emit"},
                "T_Drop": {"in": ["z"], "out": "eventOut", "action": "emit"},
            },
            "prove": {
                "claims": [
                    "deadlock_free",
                    {"joined_or_dead_lettered": "h"},
                    {"joined_or_dead_lettered": "r"},
                ]
            },
        },
    )
    nu = names(bp)
    assert nu is not None and nu.declared
    assert (nu.mints, nu.notes) == (("T_Fork", "T_J1"), ())
    if lp.z3_available():
        # libpetri reads T_J1 as a mint of z beside its relay to r, not as a re-mint.
        for k in (1, None):
            proofs = verify_blueprint(bp, k)
            assert all(p.proven for p in proofs), [
                p.result.reason or p.result.report for p in proofs
            ]


def test_joined_or_dead_lettered_runs_over_two_turns_with_exact_arrivals_and_no_abort() -> None:
    bp = load_bp()
    _, reply, _ = bp.proof.claims
    two = claim_options(bp, reply)
    assert two["initial_marking"][TURNS_LEFT] == 1  # two turns by default, as deadlock_free
    assert "sink_places" not in two  # at rest, with no sinks
    assert TURNS_LEFT not in claim_options(bp, reply, 1)["initial_marking"]
    # Another env place arrives exactly once a turn, and turnAbort never does.
    bp = nu_variant(
        {"sig": {}, "sigged": {}, "aborted": {}},
        env=["sig"],
        claims=[{"joined_or_dead_lettered": "reply"}, {"unreachable": ["aborted"]}],
        Nu_Sig={"in": ["sig"], "out": "sigged"},
        Nu_Abort={"in": ["turnAbort"], "out": "aborted"},
    )
    stranded, safety = bp.proof.claims
    at_rest, safe = claim_options(bp, stranded), claim_options(bp, safety)
    assert at_rest["environment_places"] == ["sig"]
    assert safe["environment_places"] == ["sig", "turnAbort"]
    assert repr(at_rest["environment_mode"]) == repr(lp.arrivals(2, 2))
    assert repr(safe["environment_mode"]) == repr(lp.arrivals(1))


def open_job(node_input: Any = None) -> Any:
    from bp_nu_join.names import Job

    return Job("")


def respond(node_input: Any = None) -> Any:
    from google.adk.models.llm_response import LlmResponse

    return LlmResponse()


JOB_NODES = {"open_job": FunctionNode(func=open_job), "respond": FunctionNode(func=respond)}
STRANDED_WAITING = ["deadlock_free", {"joined_or_dead_lettered": "waiting"}]


def test_an_emit_between_a_mint_and_a_key_declares_no_names() -> None:
    # The Event T_Tick emits has an id field, as the minted Job has: only the
    # emit says the name does not pass.
    data: dict[str, Any] = {
        "places": {"job": {"type": f"{NAMES}.Job"}, "waiting": S, "ev": {"type": "Event"}},
        "transitions": {
            "T_Start": {
                "in": ["userIn"],
                "out": {"and": ["job", "waiting"]},
                "node": "open_job",
                "mint": ["job.id", "waiting"],
            },
            "T_Tick": {"in": ["job"], "out": "ev", "action": "emit"},
            "T_Reply": {
                "in": ["ev", "waiting"],
                "match": {"ev": "id", "waiting": "self"},
                "out": "eventOut",
                "action": "move",
            },
        },
        "prove": {"claims": STRANDED_WAITING},
    }
    bp = parse_blueprint("n", data, nodes=JOB_NODES)
    nu = names(bp)
    assert nu is not None and not nu.declared
    assert any(
        "T_Tick's emit puts a new Event on ev, which holds no name it took" in n for n in nu.notes
    )
    if lp.z3_available():
        assert not any(p.proven for p in verify_blueprint(bp, 1))


def test_a_stock_subnet_between_a_mint_and_a_key_declares_no_names() -> None:
    data: dict[str, Any] = {
        "places": {"resp": {"type": "LlmResponse"}, "waiting": S, "ev": {"type": "Event"}},
        "subnets": {
            "route": {"stock": "router", "bind": {"llmResponse": "resp", "eventOut": "ev"}}
        },
        "transitions": {
            "T_Start": {
                "in": ["userIn"],
                "out": {"and": ["resp", "waiting"]},
                "node": "respond",
                "mint": ["resp.model_version", "waiting"],
            },
            "T_Reply": {
                "in": ["ev", "waiting"],
                "match": {"ev": "invocation_id", "waiting": "self"},
                "out": "eventOut",
                "action": "move",
            },
        },
        "prove": {"claims": STRANDED_WAITING},
    }
    bp = parse_blueprint("n", data, nodes=JOB_NODES)
    nu = names(bp)
    assert nu is not None and not nu.declared
    assert any(
        "route/Router_Route marks ev with a token no blueprint says holds the name" in n
        for n in nu.notes
    )


def relay_chain(relay: bool, hops: int = 2) -> Blueprint:
    """T_Ask mints a, b and h; the join T_J1 of a and b puts r down, ``hops`` moves
    carry it on, and T_J2 joins the last place against h."""
    ts: dict[str, Any] = {
        "T_Ask": {"in": ["userIn"], "out": {"and": ["a", "b", "h"]}, "mint": ["a", "b", "h"]},
        "T_J1": {"in": ["a", "b"], "match": {"a": "self", "b": "self"}, "out": "r"},
    }
    if relay:
        ts["T_J1"]["relay"] = {"r": "self"}
    last = "r"
    for i in range(1, hops + 1):
        ts[f"T_Hop{i}"] = {"in": [last], "out": f"r{i + 1}", "action": "move"}
        last = f"r{i + 1}"
    ts["T_J2"] = {"in": [last, "h"], "match": {last: "self", "h": "self"}, "out": "done"}
    ts["T_Emit"] = {"in": ["done"], "out": "eventOut", "action": "emit"}
    claims = ["deadlock_free", {"joined_or_dead_lettered": last}, {"joined_or_dead_lettered": "h"}]
    places = {p: S for p in ("a", "b", "h", "r", "r2", "r3", "done")}
    return parse_blueprint("n", {"places": places, "transitions": ts, "prove": {"claims": claims}})


def test_a_relayed_name_is_carried_on_to_the_next_join() -> None:
    # r2 is no mint target and no key: only the relay to r makes it a carrier.
    assert names(relay_chain(True)) == Names(("T_Ask",), ("r2",), ())


@requires_z3
def test_a_relayed_name_carried_on_to_the_next_join_is_proven() -> None:
    bp = relay_chain(True)
    for k in (1, None):
        proofs = verify_blueprint(bp, k)
        assert all(p.proven for p in proofs), [p.result.reason or p.result.report for p in proofs]
        assert all("Route B" in p.result.report for p in proofs)


def test_a_join_that_does_not_relay_to_a_key_says_so() -> None:
    hop = (
        "T_J1 puts its token on r, which reaches T_J2's key r3, but does not relay the name "
        "it joined there, so that name is not followed to the key and the claims stay "
        "unknown; add relay: {r: self} to T_J1"
    )
    direct = (
        "T_J1 puts its token on r, which is T_J2's key, but does not relay the name it joined "
        "there, so that name is not followed to the key and the claims stay unknown; "
        "add relay: {r: self} to T_J1"
    )
    for bp, note in ((relay_chain(False), hop), (relay_chain(False, 0), direct)):
        nu = names(bp)
        assert nu is not None and nu.declared
        assert nu.notes == (note,)
        if lp.z3_available():
            proofs = verify_blueprint(bp, 1)
            assert [p.result.verdict for p in proofs] == ["unknown"] * 3
            assert all(note in p.notes for p in proofs)


FAILED = {
    "failed": {"type": "NodeError"},
    "oops": {"type": "NodeError"},
    "oops2": {"type": "NodeError"},
}
FAIL_JOINS = {
    "Nu_Fail": {
        "in": ["failed", "waiting"],
        "match": {"failed": "node", "waiting": "self"},
        "out": "oops",
    },
    "Nu_FailLate": {
        "in": ["failed", "retired"],
        "match": {"failed": "node", "retired": "self"},
        "out": "oops2",
    },
}


def test_a_node_error_holds_no_name_its_run_took() -> None:
    ask = {
        "in": ["work"],
        "out": {"xor": {"default": "reply", "error": "failed"}},
        "node": "specialist",
    }
    bp = nu_variant(FAILED, Nu_Ask=ask, **FAIL_JOINS)
    nu = names(bp)
    assert nu is not None and not nu.declared
    assert (
        "Nu_Fail keys failed by node, but Nu_Ask's error branch marks failed with a NodeError, "
        "which holds the node's name, not the name its run took: names are not declared, so "
        "libpetri cannot use its ν route"
    ) in nu.notes
    if lp.z3_available():
        assert not any(p.proven for p in verify_blueprint(bp, 1))
    # Forwarded by a move first, the NodeError still holds no name.
    ask["out"] = {"xor": {"default": "reply", "error": "err"}}
    hop = {"in": ["err"], "out": "failed", "action": "move"}
    bp = nu_variant({**FAILED, "err": {"type": "NodeError"}}, Nu_Ask=ask, Nu_Hop=hop, **FAIL_JOINS)
    nu = names(bp)
    assert nu is not None and not nu.declared
    assert any("marks err with a NodeError" in n and "reaches failed" in n for n in nu.notes)


def nu_release_variant(**over: Any) -> Blueprint:
    emit = {"out": {"and": ["eventOut", "rel"]}, "action": "emit"}
    return nu_variant(
        {"rel": {}},
        {"release": "rel"},
        Nu_Reply={**text_key("turn"), **emit},
        Nu_FallbackEmit={"in": ["fallback"], **emit},
        **over,
    )


def test_under_a_release_the_mint_is_the_whole_transition() -> None:
    # turn.release splits no node, so the transition itself mints.
    bp = nu_release_variant()
    deadlock, *_ = bp.proof.claims
    assert claim_options(bp, deadlock)["mint_transitions"] == ["Nu_Start"]


@requires_z3
def test_under_a_release_a_late_reply_is_not_stranded() -> None:
    proofs = verify_blueprint(nu_release_variant())
    assert [(p.label, p.proven) for p in proofs] == [
        ("deadlock_free", True),
        ("no reply is stranded", True),
        ("no retired name is stranded", True),
    ], [p.result.reason or p.result.report for p in proofs]
    assert all("Route B" in p.result.report for p in proofs)


@requires_z3
def test_under_a_release_without_the_drop_a_late_reply_is_stranded() -> None:
    proofs = verify_blueprint(nu_release_variant(Nu_DropLate=None))
    _, reply, retired = proofs
    assert reply.violated and retired.violated, [p.result.reason or p.result.report for p in proofs]
    assert all("Route B" in p.result.report for p in (reply, retired))


def load_mount() -> Blueprint:
    node = from_config(str(NU / "mount.yaml"))
    assert isinstance(node, PetriNet)
    return node.blueprint


MOUNT_MINTS = ("Mount_Ask", "first/Pair_Open", "second/Pair_Open")


def test_a_mounted_mint_is_declared_under_its_prefixed_names() -> None:
    bp = load_mount()
    assert {t: frozenset(f.mints) for t, f in _flows(bp).items() if f.mints} == {
        "Mount_Ask": frozenset({"q1", "q2"}),
        "first/Pair_Open": frozenset({"first/tag", "first/held"}),
        "second/Pair_Open": frozenset({"second/tag", "second/held"}),
    }
    # q1 and q2 feed a mint, which puts a fresh name down: they carry none to a key.
    assert names(bp) == Names(MOUNT_MINTS, (), ())
    deadlock = Claim("deadlock_free", "deadlock_free")
    opts = claim_options(bp, deadlock, 1)
    assert opts["mint_transitions"] == list(MOUNT_MINTS)
    assert (opts["fragment_mode"], opts["carrier_places"]) == ("extended", [])
    # No mint is a node, so none is split over two turns: each still mints itself.
    assert claim_options(bp, deadlock)["mint_transitions"] == list(MOUNT_MINTS)


@requires_z3
def test_a_mounted_join_proves_on_its_prefixed_places() -> None:
    bp = load_mount()
    claims = (
        Claim("deadlock_free", "deadlock_free"),
        Claim("joined_or_dead_lettered", "first", ("first/tag",)),
        Claim("joined_or_dead_lettered", "second", ("second/held",)),
    )
    bp = dataclasses.replace(bp, proof=dataclasses.replace(bp.proof, claims=claims))
    for k in (1, None):
        proofs = verify_blueprint(bp, k)
        assert all(p.proven for p in proofs), [p.result.reason or p.result.report for p in proofs]
        assert all("Route B" in p.result.report for p in proofs)


ENV_NOTE = (
    "job is an env: place, and a join keys on the name its tokens carry: verify cannot assume "
    "a name from outside is fresh (VER-006), so every claim stays unknown; mint the name in "
    "the net, and carry the outside value in another field"
)


def test_a_name_from_an_env_place_says_so_and_asks_for_no_mint() -> None:
    # A mint: on J_Start would not help: job itself is keyed, and libpetri refuses it.
    data: dict[str, Any] = {
        "env": ["job"],
        "places": {"job": S, "waiting": S, "done": S},
        "transitions": {
            "J_Start": {"in": ["job"], "out": "waiting"},
            "J_Answer": {"in": ["userIn"], "out": "eventOut", "action": "emit"},
            "J_Join": {"in": ["job", "waiting"], "match": {"job": "self", "waiting": "self"}},
        },
        "prove": {
            "options": {"sinks": ["eventOut", "waiting"]},
            "claims": ["deadlock_free", {"place_bound": {"place": "eventOut", "bound": 1}}],
        },
    }
    bp = parse_blueprint("n", data)
    nu = names(bp)
    assert nu is not None and nu.notes == (ENV_NOTE,)
    if lp.z3_available():
        # Every claim, the safety one too: libpetri refuses a name from outside.
        assert [p.result.verdict for p in verify_blueprint(bp)] == ["unknown", "unknown"]
