"""ν-joins in a blueprint: ``match:`` joins tokens by name, ``mint:`` makes the names.

``bp_nu_join`` is the timed escalation with names: each turn mints one, a
reply joins its own turn's clock by it, and a reply after the escalation
joins the retired name and is dropped.
"""

from __future__ import annotations

from typing import Any, cast

import libpetri as lp
from google.adk.agents.config_agent_utils import from_config

from adk_libpetri.net import NodeError, PetriNet, parse_blueprint
from adk_libpetri.net.blueprint import NetScope, _take

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
