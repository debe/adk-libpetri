"""``turn: {release: place}`` (ADR 0010): the format, the released event, overlapping
turns and the proofs."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
import yaml

from adk_libpetri.net import (
    RELEASED,
    BlueprintError,
    PetriNet,
    is_released,
    parse_blueprint,
    verify_blueprint,
)
from adk_libpetri.net.proofs import TURN_NEXT, TURNS_LEFT, TURNS_RELEASED, turn_spec

from ._harness import Session, session, text_of
from .conftest import BLUEPRINTS, Serve

RELEASE = BLUEPRINTS / "bp_turns" / "release.yaml"


def net(turn: Any, **transitions: Any) -> dict[str, Any]:
    return {
        "places": {"done": {}, "typed": {"type": "str"}, "seeded": {"seed": 1}},
        "turn": turn,
        "transitions": {
            "T_Answer": {"in": ["userIn"], "out": {"and": ["eventOut", "done"]}, "action": "emit"},
            **transitions,
        },
    }


def test_a_blueprint_names_its_release_place() -> None:
    assert parse_blueprint("n", net({"release": "done"})).release == "done"
    assert parse_blueprint("n", net(None)).release is None
    assert parse_blueprint("n", {k: v for k, v in net(None).items() if k != "turn"}).release is None


@pytest.mark.parametrize(
    ("data", "path", "says"),
    [
        (net({"release": "nowhere"}), "turn.release", "not a place"),
        (net({"release": "typed"}), "turn.release", "has type"),
        (net({"release": "seeded"}), "turn.release", "seeded"),
        (net({"release": "eventOut"}), "turn.release", "eventOut"),
        (net({"release": "userIn"}), "turn.release", "userIn"),
        (net({"after": "done"}), "turn.after", "unknown key"),
        (
            net({"release": "done"}, T_Peek={"in": ["typed"], "read": ["done"], "out": "typed"}),
            "transitions.T_Peek.read",
            "releases the turn",
        ),
        (
            net({"release": "done"}, T_Clear={"in": ["typed"], "reset": ["done"], "out": "typed"}),
            "transitions.T_Clear.reset",
            "releases the turn",
        ),
        (
            {**net({"release": "idle"}), "places": {"done": {}, "idle": {}}},
            "turn.release",
            "no transition puts a token",
        ),
    ],
)
def test_a_release_place_the_turn_cannot_rely_on_is_refused(
    data: dict[str, Any], path: str, says: str
) -> None:
    with pytest.raises(BlueprintError) as err:
        parse_blueprint("n", data)
    assert err.value.path == path
    assert says in str(err.value)


def test_is_released_reads_the_events_metadata() -> None:
    from google.adk.events.event import Event

    assert is_released(Event(author="n", custom_metadata=dict(RELEASED)))
    assert not is_released(Event(author="n"))
    assert not is_released(Event(author="n", custom_metadata={"adk_libpetri": "other"}))


async def test_the_turn_yields_the_released_event_before_its_answer(serve: Serve) -> None:
    node = serve(PetriNet.from_config(str(RELEASE)))
    s = await session(node)
    for word in ("one", "two"):
        turn = await s.say(word)
        assert turn.error is None, turn.error
        released = [i for i, e in enumerate(turn.events) if is_released(e)]
        answered = [i for i, e in enumerate(turn.events) if e.output == f"guarded[{word.upper()}]"]
        assert len(released) == 1 and answered, turn.events
        assert released[0] < answered[0]
        assert turn.events[released[0]].content is None
    # The session keeps each release, as it keeps each answer.
    assert sum(is_released(e) for e in await s.stored_events()) == 2


async def test_a_net_without_turn_yields_no_released_event(serve: Serve) -> None:
    node = serve(PetriNet.from_config(str(BLUEPRINTS / "bp_turns" / "after_turn.yaml")))
    s = await session(node)
    turn = await s.say("one")
    assert turn.error is None and not any(is_released(e) for e in turn.events)


def test_the_turn_model_takes_the_release_and_splits_nothing() -> None:
    bp = PetriNet.from_config(str(RELEASE)).blueprint
    spec = turn_spec(bp)
    assert spec is not None
    nxt = next(t for t in spec.transitions if t.name == TURN_NEXT)
    assert {i.place.name for i in nxt.inputs} == {TURNS_LEFT, "turnReleased"}
    assert not nxt.reads and not nxt.inhibitors
    added = {p.name for p in spec.places} - {p.name for p in bp.spec.places}
    assert added == {TURNS_LEFT, TURNS_RELEASED}
    assert {t.name for t in spec.transitions} == {*bp.spec.transition_names, TURN_NEXT}


def test_the_release_net_proves_over_two_overlapping_turns() -> None:
    proofs = PetriNet.from_config(str(RELEASE)).verify(k=2)
    assert [(p.label, p.result.verdict) for p in proofs if not p.proven] == []
    assert all("2 turns, each input after the previous release" in p.scope for p in proofs)


def test_a_turn_that_may_never_release_keeps_the_next_from_coming() -> None:
    data = yaml.safe_load(RELEASE.read_text())
    # A second way to fork the response, with no release: the free choice
    # the verifier explores, and the next turn never comes.
    data["transitions"]["Rel_Quiet"] = {"in": ["answer"], "out": {"and": ["draft", "permit"]}}
    data["prove"] = {"claims": ["deadlock_free"]}
    bp = parse_blueprint("quiet", body(data), nodes=nodes_of(data))
    [proof] = verify_blueprint(bp)
    assert proof.violated, proof.result.verdict


def test_overlap_is_what_a_claim_over_two_turns_sees() -> None:
    # Serially a turn's draft is guarded before the next turn starts; with a
    # release the next turn may fork its own draft first.
    claim = {"claims": [{"place_bound": {"place": "draft", "bound": 1}}]}
    for name, proven in (("serial_tail.yaml", True), ("release.yaml", False)):
        data = yaml.safe_load((BLUEPRINTS / "bp_turns" / name).read_text())
        data["prove"] = claim
        bp = parse_blueprint(name, body(data), nodes=nodes_of(data))
        [proof] = verify_blueprint(bp, k=2)
        assert proof.proven is proven, (name, proof.result.verdict)


def body(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in ("agent_class", "name", "nodes")}


def nodes_of(data: dict[str, Any]) -> dict[str, Any]:
    from google.adk.workflow import FunctionNode

    from .blueprints.bp_turns import agent

    return {
        ref[0].rsplit(".", 1)[1]: FunctionNode(func=getattr(agent, ref[0].rsplit(".", 1)[1]))
        for ref in data["nodes"]
    }


class Live:
    """One turn as a server sees it: events as they come, the release as a signal."""

    def __init__(self, s: Session, text: str) -> None:
        from google.genai import types

        self.events: list[Any] = []
        self.released = asyncio.Event()
        self.released_at: float | None = None
        self.done_at: float | None = None
        message = types.Content(role="user", parts=[types.Part(text=text)])

        async def run() -> None:
            async for e in s.runner.run_async(
                user_id="u", session_id=s.session_id, new_message=message
            ):
                self.events.append(e)
                if is_released(e):
                    self.released_at = time.monotonic()
                    self.released.set()
            self.done_at = time.monotonic()

        self.task = asyncio.create_task(run())

    def answer(self) -> list[Any]:
        return [e.output for e in self.events if e.output is not None]


async def test_the_next_turn_runs_while_the_last_ones_tail_is_in_flight(serve: Serve) -> None:
    node = serve(PetriNet.from_config(str(RELEASE)))
    s = await session(node)
    one = Live(s, "one")
    await asyncio.wait_for(one.released.wait(), 5)
    # The guard of turn one (0.4 s) is still running: turn two starts now.
    two = Live(s, "two")
    await asyncio.wait_for(two.released.wait(), 5)
    assert one.done_at is None, "turn two was released before turn one's tail ended"
    await asyncio.wait_for(asyncio.gather(one.task, two.task), 5)
    assert one.answer()[-1] == "guarded[ONE]"
    assert two.answer()[-1] == "guarded[TWO]"
    assert one.done_at is not None and two.done_at is not None
    assert one.done_at < two.done_at
    # Turn two's release came while turn one's tail ran (it ends 0.4 s after its release).
    assert two.released_at is not None and two.released_at < one.done_at
    assert [text_of(e) for e in await s.stored_events() if e.author == "user"] == ["one", "two"]


async def test_without_a_release_place_the_next_turn_waits_for_the_tail(serve: Serve) -> None:
    node = serve(PetriNet.from_config(str(BLUEPRINTS / "bp_turns" / "serial_tail.yaml")))
    s = await session(node)
    one = Live(s, "one")
    await asyncio.sleep(0.15)  # turn one is in its tail
    two = Live(s, "two")
    await asyncio.wait_for(asyncio.gather(one.task, two.task), 5)
    assert not any(is_released(e) for e in one.events + two.events)
    assert one.done_at is not None and two.done_at is not None
    # Turn two's own guard ran after turn one had ended.
    assert two.done_at - one.done_at >= 0.35
