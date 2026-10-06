"""``parse_blueprint``: every arc and timing form, the action plans, and every error.

Pure parsing, no ADK loader: the mapping is what a YAML file's keys hold.
Each error names the key path an author (or an agent) has to fix.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.events.event import Event
from google.adk.workflow import FunctionNode
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._spec import (
    IMMEDIATE,
    VOID,
    And,
    OutPlace,
    Place,
    Port,
    Timeout,
    TransitionSpec,
    Xor,
    all_tokens,
    and_,
    at_least,
    deadline,
    delayed,
    exact,
    exactly,
    one,
    out,
    window,
)
from adk_libpetri.net import BlueprintError, NodeError, parse_blueprint


def _f(node_input: Any) -> Any:
    return node_input


NODES = {"f": FunctionNode(name="f", func=_f), "g": FunctionNode(name="g", func=_f)}


def bp(data: dict[str, Any], **kw: Any) -> Any:
    return parse_blueprint("n", data, nodes=NODES, **kw)


A, B, X, Y = (Place(n) for n in ("a", "b", "x", "y"))


def _unit(*names: str) -> dict[str, Any]:
    return {n: {} for n in names}


S = Place("s", str)


# ----------------------------------------------------------------------------
#  Arcs, timings, places
# ----------------------------------------------------------------------------


def test_every_input_arc_form_and_the_other_arcs() -> None:
    b = bp(
        {
            "places": {
                **_unit("a", "b", "x", "y", "r", "i", "z"),
                "s": {"type": "str"},
                "n": {"type": "int"},
            },
            "transitions": {
                "T_Arcs": {
                    "in": [
                        "a",
                        {"place": "s", "count": 3},
                        {"place": "n", "at_least": 2},
                        {"place": "b", "all": True},
                        {"place": "x", "count": 1},
                    ],
                    "read": ["r"],
                    "inhibit": "i",
                    "reset": ["z"],
                    "priority": -7,
                    "out": "y",
                    "node": "f",
                },
            },
        }
    )
    n = Place("n", int)
    assert b.spec.transition("T_Arcs") == TransitionSpec(
        "T_Arcs",
        (one(A), exactly(3, S), at_least(2, n), all_tokens(B), one(X)),
        out(Y),
        reads=(Place("r"),),
        inhibitors=(Place("i"),),
        resets=(Place("z"),),
        priority=-7,
    )


def test_every_output_form() -> None:
    b = bp(
        {
            "places": _unit("a", "b", "x", "y"),
            "transitions": {
                "T_And": {"in": ["a"], "out": {"and": ["x", {"timeout": 50, "child": "y"}]}},
                "T_Xor": {"in": ["a"], "out": {"xor": ["x", "y"]}, "node": "f"},
                "T_Routes": {
                    "in": ["b"],
                    "out": {"xor": {"left": "x", "right": {"and": ["x", "y"]}, "default": "y"}},
                    "node": "g",
                },
                "T_Late": {"in": ["b"], "out": {"xor": ["x", {"timeout": 100, "child": "y"}]}},
                "T_Timeout": {"in": ["a"], "out": {"timeout": 10, "child": {"and": ["x", "y"]}}},
            },
        }
    )
    t = b.spec.transition
    assert t("T_And").output == And((OutPlace(X), Timeout(50, OutPlace(Y))))
    assert t("T_Xor").output == Xor((OutPlace(X), OutPlace(Y)))
    assert t("T_Routes").output == Xor((OutPlace(X), And((OutPlace(X), OutPlace(Y))), OutPlace(Y)))
    assert t("T_Late").output == Xor((OutPlace(X), Timeout(100, OutPlace(Y))))
    assert t("T_Timeout").output == Timeout(10, and_(X, Y))

    # A plain xor's branches are routed by place name; labels are routes.
    xor_plan = b.plans["T_Xor"]
    assert xor_plan.routes == {"x": 0, "y": 1}
    routes = b.plans["T_Routes"]
    assert routes.routes == {"left": 0, "right": 1}
    assert routes.default == 2
    assert [br.places for br in routes.branches] == [(X,), (X, Y), (Y,)]
    # The timeout branch is the executor's: a move takes the other one.
    late = b.plans["T_Late"]
    assert late.kind == "move"
    assert [br.places for br in late.branches] == [(X,)]
    # A top-level timeout is transparent to the action.
    assert b.plans["T_Timeout"].branches[0].places == (X, Y)


@pytest.mark.parametrize(
    ("timing", "expected"),
    [
        (None, IMMEDIATE),
        ("immediate", IMMEDIATE),
        ({"delayed": 5}, delayed(5)),
        ({"deadline": 9}, deadline(9)),
        ({"exact": 3}, exact(3)),
        ({"window": [1, 4]}, window(1, 4)),
    ],
)
def test_every_timing_form(timing: Any, expected: Any) -> None:
    decl: dict[str, Any] = {"in": ["a"], "out": "x"}
    if timing is not None:
        decl["timing"] = timing
    b = bp({"places": _unit("a", "x"), "transitions": {"T_Tick": decl}})
    assert b.spec.transition("T_Tick").timing == expected


def test_place_types_seeds_and_the_implicit_catalog() -> None:
    b = bp(
        {
            "places": {
                "permit": {"seed": 2},
                "notes": {"type": "str", "seed": ["a", "b"]},
                "content": {"type": "google.genai.types.Content"},
                "alias": {"type": "Content"},
                "err": {"type": "NodeError"},
                "turnPermit": {},
            },
            "transitions": {
                "T_Go": {"in": ["userIn", "permit"], "out": "eventOut", "action": "emit"},
                "T_Keep": {"in": ["notes", "content", "alias", "err"], "node": "f"},
            },
        }
    )
    spec = b.spec
    assert spec.place_named("userIn") == C.USER_IN
    assert spec.place_named("eventOut") == C.EVENT_OUT
    assert spec.place_named("content") == Place("content", types.Content)
    assert spec.place_named("alias") == Place("alias", types.Content)
    assert spec.place_named("err") == Place("err", NodeError)
    assert spec.place_named("permit") == Place("permit", VOID)
    assert b.seeds["permit"] == (None, None)
    assert b.seeds["notes"] == ("a", "b")
    assert b.seeds["turnPermit"] == (None,)  # seeded as PetriRunner seeds it
    assert b.env == ("userIn",)
    assert spec.ports == (Port("userIn", "in", C.USER_IN), Port("eventOut", "out", C.EVENT_OUT))


def test_ports_and_env() -> None:
    b = bp(
        {
            "places": {"q": {"type": "str"}, "r": {"type": "str"}, "tick": {}},
            "env": ["tick"],
            "ports": {"question": {"place": "q", "direction": "in"}, "r": {"direction": "out"}},
            "transitions": {"T_Go": {"in": ["q", "tick"], "out": "r"}},
        }
    )
    assert b.spec.ports == (
        Port("question", "in", Place("q", str)),
        Port("r", "out", Place("r", str)),
    )
    assert b.env == ("tick",)


def test_action_plans() -> None:
    b = bp(
        {
            "places": {
                "s": {"type": "str"},
                "t": {"type": "str"},
                "e": {"type": "Event"},
                **_unit("a", "x"),
            },
            "transitions": {
                "T_Move": {"in": ["s", "a"], "out": {"and": ["t", "x"]}},
                "T_Emit": {"in": ["t"], "out": "e", "action": "emit"},
                "T_Node": {
                    "in": ["e"],
                    "read": ["s"],
                    "out": {"xor": {"ok": "x", "error": "a"}},
                    "node": "f",
                },
            },
        }
    )
    move, emit, node = (b.plans[t] for t in ("T_Move", "T_Emit", "T_Node"))
    assert (move.kind, emit.kind, node.kind) == ("move", "emit", "node")
    assert node.node is NODES["f"]
    assert node.error == 1
    assert node.reads == (Place("s", str),)
    assert emit.branches[0].places == (Place("e", Event),)


# ----------------------------------------------------------------------------
#  Errors: each names its key path
# ----------------------------------------------------------------------------

P = {"places": {**_unit("a", "b", "x", "y"), "s": {"type": "str"}, "t": {"type": "str"}}}


def _t(decl: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {**P, "transitions": {"T_A": decl}, **extra}


ERRORS: list[tuple[str, dict[str, Any], str, str]] = [
    ("unknown top key", {"transition": {}}, "transition", "did you mean 'transitions'"),
    ("no transitions", {"places": {}}, "transitions", "at least one transition"),
    ("unknown place", _t({"in": ["nope"]}), "transitions.T_A.in[0]", "unknown place 'nope'"),
    ("unknown key", _t({"in": ["a"], "outt": "x"}), "transitions.T_A.outt", "did you mean 'out'"),
    (
        "xor, no node",
        _t({"in": ["a"], "out": {"xor": ["x", "y"]}}),
        "transitions.T_A.out",
        "nothing chooses",
    ),
    (
        "nested xor",
        _t({"in": ["a"], "out": {"and": ["x", {"xor": ["a", "b"]}]}, "node": "f"}),
        "transitions.T_A.out.and[1]",
        "a nested xor",
    ),
    (
        "one-branch xor",
        _t({"in": ["a"], "out": {"xor": ["x"]}}),
        "transitions.T_A.out.xor",
        "two branches",
    ),
    (
        "unlabelled branch",
        _t({"in": ["a"], "out": {"xor": ["x", {"and": ["x", "y"]}]}, "node": "f"}),
        "transitions.T_A.out",
        "no route label",
    ),
    (
        "move of two values",
        _t({"in": ["s", "t"], "out": "s"}),
        "transitions.T_A.in",
        "forwards one",
    ),
    ("move of nothing", _t({"in": ["a"], "out": "s"}), "transitions.T_A.in", "forwards one"),
    (
        "move of a list",
        _t({"in": [{"place": "s", "count": 2}], "out": "t"}),
        "transitions.T_A.in",
        "forwards one",
    ),
    (
        "move across types",
        _t({"in": ["s"], "out": "eventOut"}),
        "transitions.T_A.out",
        "takes Event",
    ),
    (
        "emit into str",
        _t({"in": ["s"], "out": "t", "action": "emit"}),
        "transitions.T_A.out",
        "takes str",
    ),
    (
        "error branch type",
        _t({"in": ["s"], "out": {"xor": {"ok": "x", "error": "t"}}, "node": "f"}),
        "transitions.T_A.out.xor.error",
        "NodeError",
    ),
    (
        "xor under timeout",
        _t({"in": ["a"], "out": {"timeout": 5, "child": {"xor": ["x", "y"]}}}),
        "transitions.T_A.out.child",
        "cannot be an xor",
    ),
    (
        "unknown out form",
        _t({"in": ["a"], "out": {"or": ["x"]}}),
        "transitions.T_A.out.or",
        "unknown output form",
    ),
    (
        "window",
        _t({"in": ["a"], "timing": {"window": [5, 1]}}),
        "transitions.T_A.timing.window",
        "before",
    ),
    (
        "timing key",
        _t({"in": ["a"], "timing": {"later": 5}}),
        "transitions.T_A.timing.later",
        "unknown key",
    ),
    ("action", _t({"in": ["a"], "action": "jump"}), "transitions.T_A.action", "unknown action"),
    (
        "node and action",
        _t({"in": ["a"], "node": "f", "action": "emit"}),
        "transitions.T_A",
        "at most one",
    ),
    ("unknown node", _t({"in": ["a"], "node": "h"}), "transitions.T_A.node", "unknown node 'h'"),
    ("no input", _t({"out": "x"}), "transitions.T_A.in", "always enabled"),
    (
        "two arc forms",
        _t({"in": [{"place": "s", "count": 2, "all": True}]}),
        "transitions.T_A.in[0]",
        "one of count",
    ),
    ("count 0", _t({"in": [{"place": "s", "count": 0}]}), "transitions.T_A.in[0].count", ">= 1"),
    ("priority", _t({"in": ["a"], "priority": "high"}), "transitions.T_A.priority", "integer"),
    ("seed", {"places": {"s": {"type": "str", "seed": 2}}}, "places.s.seed", "coloured"),
    ("slash", {"places": {"a/b": {}}}, "places.a/b", "contains '/'"),
    ("type", {"places": {"p": {"type": "Nope"}}}, "places.p.type", "unknown type 'Nope'"),
    ("leading dot", {"places": {"p": {"type": ".agent.T"}}}, "places.p.type", "fully qualified"),
    ("import", {"places": {"p": {"type": "no_such_module.T"}}}, "places.p.type", "cannot import"),
    ("place key", {"places": {"p": {"kind": "str"}}}, "places.p.kind", "unknown key"),
    ("env", _t({"in": ["a"]}, env=["nope"]), "env[0]", "unknown place"),
    (
        "port place",
        _t({"in": ["a"]}, ports={"p": {"place": "q", "direction": "in"}}),
        "ports.p.place",
        "no transition uses",
    ),
    (
        "port direction",
        _t({"in": ["a"]}, ports={"a": {"direction": "both"}}),
        "ports.a.direction",
        "in, out or inout",
    ),
    (
        "claim place",
        _t({"in": ["a"]}, prove={"claims": [{"place_bound": {"place": "q", "bound": 1}}]}),
        "prove.claims[0].place_bound.place",
        "unknown place 'q'",
    ),
    (
        "claim kind",
        _t({"in": ["a"]}, prove={"claims": [{"bounded": {"place": "a"}}]}),
        "prove.claims[0].bounded",
        "unknown claim key",
    ),
    (
        "claim string",
        _t({"in": ["a"]}, prove={"claims": ["deadlock_freedom"]}),
        "prove.claims[0]",
        "a claim is",
    ),
    (
        "mutual exclusion",
        _t({"in": ["a"]}, prove={"claims": [{"mutual_exclusion": ["a"]}]}),
        "prove.claims[0].mutual_exclusion",
        "two places",
    ),
    (
        "modes differ",
        _t(
            {"in": ["a"]},
            prove={
                "options": {"environment": {"a": {"arrivals": 1}, "b": "always"}},
                "claims": ["deadlock_free"],
            },
        ),
        "prove.options.environment",
        "one mode",
    ),
    (
        "env mode",
        _t({"in": ["a"]}, prove={"options": {"environment": {"a": {"arrive": 1}}}}),
        "prove.options.environment.a.arrive",
        "unknown key",
    ),
    (
        "marking",
        _t({"in": ["a"]}, prove={"options": {"initial_marking": {"a": -1}}}),
        "prove.options.initial_marking.a",
        ">= 0",
    ),
    ("on_load", _t({"in": ["a"]}, prove={"on_load": True}), "prove.on_load", "none are listed"),
    (
        "subnet kind",
        _t({"in": ["a"]}, subnets={"x": {"bind": {}}}),
        "subnets.x",
        "either net:",
    ),
    (
        "subnet net",
        _t({"in": ["a"]}, subnets={"x": {"net": "f"}}),
        "subnets.x.net",
        "not a PetriNet",
    ),
    (
        "stock kind",
        _t({"in": ["a"]}, subnets={"x": {"stock": "llm_agnet"}}),
        "subnets.x.stock",
        "did you mean 'llm_agent'",
    ),
    (
        "stock from",
        _t({"in": ["a"]}, subnets={"x": {"stock": "llm_agent", "bind": {}}}),
        "subnets.x",
        "add from:",
    ),
    (
        "stock not an agent",
        _t({"in": ["a"]}, subnets={"x": {"stock": "llm_step", "from": "f"}}),
        "subnets.x.from",
        "not an LlmAgent",
    ),
]


@pytest.mark.parametrize(
    ("data", "path", "message"), [e[1:] for e in ERRORS], ids=[e[0] for e in ERRORS]
)
def test_errors_name_their_key_path(data: dict[str, Any], path: str, message: str) -> None:
    with pytest.raises(BlueprintError) as info:
        bp(data)
    err = info.value
    assert err.path == path, str(err)
    assert message in str(err), str(err)


def test_an_error_names_its_file() -> None:
    with pytest.raises(BlueprintError, match=r"^net\.yaml: transitions\.T_A\.in\[0\]: unknown"):
        parse_blueprint("n", _t({"in": ["nope"]}), source="net.yaml")


def test_a_hint_says_how_to_fix_it() -> None:
    with pytest.raises(BlueprintError) as info:
        bp(_t({"in": ["a"], "out": {"xor": ["x", "y"]}}))
    assert info.value.hint is not None
    assert "name a node" in info.value.hint
