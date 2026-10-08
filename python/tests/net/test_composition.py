"""Subnets: blueprints mounted by YAML ref (``net:``) and stock subnets (``stock:``).

A mounted subnet's bound ports fuse with the parent's places; every other
place and transition is prefixed with the instance name, so one blueprint
can be mounted twice. Its seeds and actions carry over. The result is one
flat ``NetSpec``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from google.adk.agents.config_agent_utils import from_config
from google.adk.workflow import FunctionNode

from adk_libpetri._spec import Place
from adk_libpetri.net import BlueprintError, PetriNet, parse_blueprint
from adk_libpetri.subnet import llm_agent
from support.fake_llm import ScriptedLlm, call, text

from ._harness import answers, runner_of, session, text_of
from .conftest import BLUEPRINTS, Serve


def load(path: object, serve: Serve) -> PetriNet:
    node = from_config(str(path))
    assert isinstance(node, PetriNet)
    return serve(node)


# ----------------------------------------------------------------------------
#  A child blueprint mounted twice
# ----------------------------------------------------------------------------


async def test_a_child_blueprint_mounted_twice(
    serve: Serve,
) -> None:
    node = load(BLUEPRINTS / "bp_compose" / "twice.yaml", serve)
    spec = node.spec
    for inst in ("first", "second"):
        assert f"{inst}/Race_Start" in spec.transition_names
        assert spec.subnet_of(f"{inst}/Race_RunA") == inst
        assert spec.has_place(f"{inst}/won")
        assert not spec.has_place(f"{inst}/question")  # bound: it is q1 / q2
    start = spec.transition("first/Race_Start")
    assert [i.place.name for i in start.inputs] == ["q1"]
    commit = spec.transition("second/Race_CommitB")
    assert commit.output is not None
    assert {p.name for p in commit.places()} == {"second/b", "second/permit", "r2", "second/won"}
    # Auto-declared from the port it is bound to.
    assert spec.place_named("r1") == Place("r1", str)

    s = await session(node)
    turn = await s.say("q")
    assert turn.error is None
    [answer] = answers(turn.events, "double_race")
    assert text_of(answer) == "quick(q) + quick(q)"
    paths = {e.node_info.path for e in turn.events}
    # Both instances ran both branches; the careful losers drained in the turn.
    # A mounted function node runs as <mount>·<node>: its path names the mount.
    runs = {f"double_race@1/{m}·{n}@1" for m in ("first", "second") for n in ("quick", "careful")}
    assert runs <= paths
    snap = await runner_of(node, s).snapshot()
    assert snap.marking.count("first/won") == snap.marking.count("second/won") == 1


# ----------------------------------------------------------------------------
#  Nested YAML refs, each file's leading-dot refs in its own package
# ----------------------------------------------------------------------------


async def test_nested_yaml_refs_two_levels_deep(
    serve: Serve,
) -> None:
    from bp_leaf.agent import LeafNote
    from bp_mid.agent import MidNote
    from bp_nested.agent import TopNote

    node = load(BLUEPRINTS / "bp_nested" / "top.yaml", serve)
    spec = node.spec
    assert "middle/inner/Leaf_Wrap" in spec.transition_names
    assert "middle/Mid_Wrap" in spec.transition_names
    assert spec.place_named("middle/inner/note") is None  # bound to mid's leaf_note
    assert spec.place_named("middle/leaf_note") == Place("middle/leaf_note", LeafNote)
    assert spec.place_named("mid_out") == Place("mid_out", MidNote)
    assert spec.place_named("note") == Place("note", TopNote)
    assert spec.subnet_of("middle/inner/Leaf_Wrap") == "middle"

    s = await session(node)
    turn = await s.say("x")
    assert turn.error is None
    assert text_of(turn.events[-1]) == "top[mid[leaf[x]]]"
    assert "nested_net@1/middle·inner·wrap_leaf@1" in {e.node_info.path for e in turn.events}


# ----------------------------------------------------------------------------
#  Stock llm_agent, configured from an LlmAgent YAML
# ----------------------------------------------------------------------------


def _strip(fp: dict[str, Any], prefix: str) -> dict[str, Any]:
    fp = dict(fp)
    fp.pop("name")
    fp.pop("ports")
    fp = json.loads(json.dumps(fp).replace(f'"{prefix}/', '"'))
    return {k: sorted(v, key=lambda d: d["name"]) for k, v in fp.items()}


def test_stock_llm_agent_is_the_stock_subnet_under_a_prefix() -> None:
    node = from_config(str(BLUEPRINTS / "bp_llm" / "root.yaml"))
    assert isinstance(node, PetriNet)
    got = node.spec.fingerprint()
    # userIn, eventOut and turnAbort are bound to the parent's places of those
    # names; every other place and transition is llm_agent.DEF's under assistant/.
    assert _strip(got, "assistant") == _strip(llm_agent.DEF.fingerprint(), "assistant")
    assert "assistant/LlmAgent_reaskBudget" in {p["name"] for p in got["places"]}
    assert node.blueprint.seeds["assistant/turnPermit"] == (None,)
    assert "turnAbort" not in node.blueprint.env  # the runner declares it, as for PetriAgent


async def test_stock_llm_agent_runs_the_agents_model_and_tools(
    serve: Serve,
) -> None:
    node = load(BLUEPRINTS / "bp_llm" / "root.yaml", serve)
    [helper] = node.nodes[0]
    llm = ScriptedLlm.of(call("get_weather", {"city": "Paris"}), text("Sunny in Paris."))
    helper.model = llm  # the session's runner resolves the model when it starts

    s = await session(node)
    turn = await s.say("weather in Paris?")
    assert turn.error is None
    assert text_of(turn.events[-1]) == "Sunny in Paris."
    first, second = llm.requests
    assert first.config.system_instruction is not None
    assert "weather questions" in text_of_content(first.config.system_instruction)
    assert "get_weather" in first.tools_dict
    responses = [
        p.function_response for c in second.contents for p in c.parts or [] if p.function_response
    ]
    assert responses[0] is not None
    assert responses[0].response == {"city": "Paris", "forecast": "sunny"}


async def test_a_failing_stock_subnet_is_the_error_events_author(
    serve: Serve,
) -> None:
    """Not the net: ADK's dev UI lights the drawn node an event's author names, and the
    net's name is on its answer and on every event of its nodes."""
    node = load(BLUEPRINTS / "bp_llm" / "root.yaml", serve)
    [helper] = node.nodes[0]
    helper.model = ScriptedLlm.of(
        ValueError("No API key was provided. Please pass a valid API key.")
    )
    s = await session(node)
    turn = await s.say("weather in Paris?")
    assert turn.error is not None
    [err] = [e for e in await s.stored_events() if e.error_code]
    assert err.author == "assistant" and err.author != node.name
    assert "GOOGLE_API_KEY" in str(err.error_message)  # the fix, in the chat


def text_of_content(c: Any) -> str:
    return "".join(p.text or "" for p in c.parts or [])


# ----------------------------------------------------------------------------
#  Errors
# ----------------------------------------------------------------------------


def child() -> PetriNet:
    return PetriNet(
        name="child",
        places={"q": {"type": "str"}, "r": {"type": "str"}, "permit": {"seed": 1}, "tick": {}},
        env=["tick"],
        ports={"q": {"direction": "in"}, "r": {"direction": "out"}},
        transitions={"Child_Go": {"in": ["q", "permit", "tick"], "out": "r"}},
    )


def mount(bind: dict[str, Any], **places: Any) -> Any:
    return parse_blueprint(
        "parent",
        {
            "places": {"question": {"type": "str"}, **places},
            "subnets": {"c": {"net": "child", "bind": bind}},
            "transitions": {"Parent_Ask": {"in": ["userIn"], "out": "question", "node": "read"}},
        },
        nodes={"child": child(), "read": FunctionNode(name="read", func=_read)},
    )


def _read(node_input: Any) -> str:
    return str(node_input)


def test_a_mounted_childs_seeds_and_env_carry_over_with_its_prefix() -> None:
    b = mount({"q": "question", "r": "answer"})
    assert b.seeds["c/permit"] == (None,)
    assert "c/tick" in b.env
    assert b.spec.transition("c/Child_Go").inputs[0].place == Place("question", str)


def test_a_bound_places_seed_is_the_parents() -> None:
    b = mount({"q": "question", "r": "answer"})
    assert "question" not in b.seeds


@pytest.mark.parametrize(
    ("bind", "places", "path", "message"),
    [
        ({"r": "answer"}, {}, "subnets.c.bind", "in-port(s) ['q'] are not bound"),
        ({"q": "question", "query": "x"}, {}, "subnets.c.bind.query", "unknown port 'query'"),
        ({"q": "n"}, {"n": {"type": "int"}}, "subnets.c.bind.q", "carries str"),
        ({"q": "question", "r": 3}, {}, "subnets.c.bind.r", "place name"),
    ],
    ids=["unbound in-port", "unknown port", "type conflict", "not a place"],
)
def test_binding_errors(
    bind: dict[str, Any], places: dict[str, Any], path: str, message: str
) -> None:
    with pytest.raises(BlueprintError) as info:
        mount(bind, **places)
    assert info.value.path == path, str(info.value)
    assert message in str(info.value)


def test_a_ref_cycle_is_rejected_with_its_chain() -> None:
    with pytest.raises(BlueprintError, match=r"ref cycle: a\.yaml -> b\.yaml -> a\.yaml") as info:
        from_config(str(BLUEPRINTS / "bp_cycle" / "a.yaml"))
    assert info.value.path == "nodes"


def _forecast(city: str) -> dict[str, str]:
    """The forecast for a city."""
    return {"city": city, "forecast": "sunny"}


def test_every_stock_subnet_mounts_from_an_llm_agent() -> None:
    from google.adk.agents.llm_agent import LlmAgent

    from adk_libpetri.net.blueprint import NetScope

    agent = LlmAgent(
        name="helper", model=ScriptedLlm.of(), instruction="Be brief.", tools=[_forecast]
    )
    b = parse_blueprint(
        "stocks",
        {
            "subnets": {
                "step": {
                    "stock": "llm_step",
                    "from": "helper",
                    "bind": {"llmRequest": "req", "llmResponse": "resp"},
                },
                "route": {
                    "stock": "router",
                    "bind": {"llmResponse": "resp", "toolCalls": "calls", "eventOut": "eventOut"},
                },
                "tools": {
                    "stock": "tool_dispatch",
                    "from": "helper",
                    "bind": {"toolCalls": "calls"},
                },
            },
        },
        nodes={"helper": agent},
    )
    names = set(b.spec.transition_names)
    assert {"step/LlmStep_LlmCall", "route/Router_Route", "tools/ToolDispatch_Dispatch"} <= names
    assert b.spec.has_place("tools/toolResults")  # an unbound out-port stays the instance's
    assert b.spec.place_named("calls") is not None
    acts = b.actions(NetScope())  # every transition bound, the stock actions under their prefix
    assert set(acts) == names


def test_a_subnet_instance_may_not_share_a_places_name() -> None:
    """The drawing titles a collapsed subnet by its prefix: one name, one thing."""
    with pytest.raises(BlueprintError) as info:
        mount({"q": "question", "r": "answer"}, answer={"type": "str"}, c={})
    assert info.value.path == "subnets.c"
    assert "has the name of a place" in str(info.value)
