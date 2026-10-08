"""Blueprints loaded by ADK's own YAML loader and run under a stock ``InMemoryRunner``.

``from_config`` is what ``adk web`` and ``adk run`` call; it resolves
``nodes:`` (``.agent.fn`` refs in each file's package) and builds the
``PetriNet``. The node then serves each session from one long-lived net.
"""

from __future__ import annotations

import json

import pytest
from google.adk.agents.config_agent_utils import from_config
from google.adk.workflow import FunctionNode, Workflow

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import NetSpec, Place, TransitionSpec, and_, one, out
from adk_libpetri.net import BlueprintError, PetriNet
from adk_libpetri.runner import SessionExecutorRegistry

from ._harness import answers, runner_of, session, settled, text_of
from .conftest import BLUEPRINTS, Serve

BASIC = BLUEPRINTS / "bp_basic"
RACE = BLUEPRINTS / "bp_race"


def load(path: object, serve: Serve) -> PetriNet:
    node = from_config(str(path))
    assert isinstance(node, PetriNet)
    return serve(node)


def test_adks_loader_builds_the_blueprint() -> None:
    node = from_config(str(BASIC / "root.yaml"))
    assert isinstance(node, PetriNet)
    assert node.spec.transition_names == ("Echo_Shout", "Echo_Emit")
    assert node.blueprint.source == str(BASIC / "root.yaml")
    assert node.blueprint.plans["Echo_Shout"].node.name == "shout"
    assert node.blueprint.env == ("userIn",)


def test_petri_net_from_config_takes_the_serving_options(
    orchestrator: OrchestratorLoop,
) -> None:
    registry = SessionExecutorRegistry.strong_owned()
    node = PetriNet.from_config(
        str(BASIC / "root.yaml"), orchestrator=orchestrator, registry=registry
    )
    assert node.orchestrator is orchestrator
    assert node.registry is registry
    with pytest.raises(TypeError, match="not a PetriNet"):
        PetriNet.from_config(str(BLUEPRINTS / "bp_llm" / "helper.yaml"))


async def test_a_minimal_net_runs_a_node_and_emits(
    serve: Serve,
) -> None:
    node = load(BASIC / "root.yaml", serve)
    s = await session(node)
    turn = await s.say("hello")
    assert turn.error is None
    assert turn.texts == ["HELLO"]
    [answer] = answers(turn.events, "echo_net")
    assert answer.author == "echo_net"
    # Under the transition that put it on eventOut: ADK's dev UI lights it.
    assert answer.node_info.path == "echo_net@1/Echo_Emit@1"
    assert answer.node_info.output_for == ["echo_net@1/Echo_Emit@1", "echo_net@1"]
    [shout] = [e for e in turn.events if e.node_info.path == "echo_net@1/shout@1"]
    assert shout.output == "HELLO"
    # One net per session, across turns.
    runner = runner_of(node, s)
    again = await s.say("again")
    assert again.texts == ["AGAIN"]
    assert runner_of(node, s) is runner


async def test_route_labelled_xor_error_branch_move_and_emit(
    serve: Serve,
) -> None:
    node = load(BASIC / "triage.yaml", serve)
    s = await session(node)

    later = await s.say("later please")  # default route -> move
    assert later.by("triage_net")[-1].node_info.path.startswith("triage_net@1/Triage_")
    assert text_of(later.events[-1]) == "later please"

    urgent = await s.say("now!")  # route urgent -> handle succeeds
    assert text_of(urgent.events[-1]) == "handled now!"

    failed = await s.say("now! boom")  # handle fails -> its error branch
    assert failed.error is None
    paths = [e.node_info.path for e in failed.events]
    assert "triage_net@1/handle@1" in paths
    [err] = [e for e in failed.events if e.error_code]
    assert err.error_code == "ValueError"
    assert text_of(failed.events[-1]) == "sorry, handle failed: cannot handle 'now! boom'"


async def test_a_failing_node_without_an_error_branch_fails_like_a_workflow(
    serve: Serve,
) -> None:
    from bp_basic.agent import explode

    native = await session(
        Workflow(name="failing_net", edges=[("START", FunctionNode(name="explode", func=explode))])
    )
    want = await native.say("x")

    node = load(BASIC / "failing.yaml", serve)
    s = await session(node)
    got = await s.say("x")

    def errors(events: list) -> list[tuple[str, str | None, str | None]]:
        return [(e.node_info.path, e.error_code, e.error_message) for e in events if e.error_code]

    assert type(got.error) is type(want.error) is RuntimeError
    assert str(got.error) == str(want.error) == "kaboom"
    assert errors(await s.stored_events()) == errors(await native.stored_events())


async def test_a_plain_value_on_event_out_is_the_nodes_output(
    serve: Serve,
) -> None:
    node = load(BASIC / "value.yaml", serve)
    s = await session(node)
    turn = await s.say("one two three")
    [final] = answers(turn.events, "value_net")
    assert final.output == 3
    # A text part too: the dev UI shows the answer as a message, not as JSON.
    assert text_of(final) == "```json\n3\n```"


# ----------------------------------------------------------------------------
#  The race: structure equal to a hand-written net, and its run
# ----------------------------------------------------------------------------


def hand_written_race() -> NetSpec:
    trigger = {x: Place(f"trigger{x}") for x in "AB"}
    done = {x: Place(f"branch{x}Done", str) for x in "AB"}
    permit, won, discarded = Place("racePermit"), Place("raceWon"), Place("raceDiscarded", str)
    ts = [
        TransitionSpec(
            "Race_Start",
            (one(C.USER_IN),),
            and_(*trigger.values(), permit),
            resets=(permit, won, *trigger.values(), *done.values(), discarded),
        )
    ]
    for x in "AB":
        ts += [
            TransitionSpec(
                f"Race_RunBranch{x}", (one(trigger[x]),), out(done[x]), inhibitors=(won,)
            ),
            TransitionSpec(
                f"Race_Commit{x}",
                (one(done[x]), one(permit)),
                and_(C.EVENT_OUT, won),
                priority=10,
            ),
            TransitionSpec(
                f"Race_Discard{x}", (one(done[x]),), out(discarded), reads=(won,), priority=-10
            ),
        ]
    return NetSpec("speculative_race", tuple(ts))


def test_the_race_blueprint_is_the_hand_written_net() -> None:
    node = from_config(str(RACE / "race.yaml"))
    assert isinstance(node, PetriNet)
    want = hand_written_race().fingerprint()
    got = node.spec.fingerprint()
    # The hand-written spec declares no ports; a top-level blueprint gets the turn's.
    assert got.pop("ports") == [
        {"direction": "out", "name": "eventOut", "place": "eventOut"},
        {"direction": "in", "name": "userIn", "place": "userIn"},
    ]
    want.pop("ports")
    assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)


async def test_the_race_answers_with_the_fast_branch_and_drains_the_slow_one(
    serve: Serve,
) -> None:
    from bp_race import agent

    agent.FINISHED.clear()
    node = load(RACE / "race.yaml", serve)
    s = await session(node)
    turn = await s.say("which branch wins?")
    assert turn.error is None
    [answer] = answers(turn.events, "speculative_race")
    assert text_of(answer) == "answer from fast"
    # The answer streams out before the slow branch is done ...
    assert turn.first_event_after is not None
    first_answer = turn.events.index(answer)
    assert "speculative_race@1/slow@1" not in [e.node_info.path for e in turn.events[:first_answer]]
    # ... and the invocation lasts until the loser's run is over (it runs inside it).
    assert agent.FINISHED == ["fast", "slow"]
    snap = await settled(
        runner_of(node, s), lambda m: m.marking.count("raceDiscarded") and not m.action_in_flight
    )
    assert list(snap.marking.tokens("raceDiscarded")) == ["answer from slow"]
    assert snap.marking.count("raceWon") == 1
    assert snap.marking.count("racePermit") == 0
    assert not snap.action_in_flight


def test_a_typo_in_a_yaml_key_fails_the_load(tmp_path: object) -> None:
    from pathlib import Path

    d = Path(str(tmp_path)) / "bp_typo"
    d.mkdir()
    (d / "net.yaml").write_text(
        "agent_class: adk_libpetri.net.PetriNet\nname: typo\ntransitionz: {}\n"
    )
    with pytest.raises(BlueprintError, match="did you mean 'transitions'"):
        from_config(str(d / "net.yaml"))
