"""Which transition answered: the egress tap names each ``eventOut`` token."""

from __future__ import annotations

from typing import Any

from google.adk.agents.config_agent_utils import from_config
from google.adk.events.event import Event

from adk_libpetri.net import PetriNet
from adk_libpetri.net.blueprint import NetScope
from adk_libpetri.net.node import _answering, _EgressTap, timeout_places

from ._harness import session
from .conftest import BLUEPRINTS, Serve


class _Ev:
    def __init__(
        self, type: str, place: str | None = None, transition: str | None = None, token: Any = None
    ) -> None:
        self.type = type
        self.place_name = place
        self.transition_name = transition
        self.token = token


def _tap() -> tuple[_EgressTap, NetScope]:
    scope = NetScope()
    return _EgressTap(None, scope, {"To_Slow": frozenset({"eventOut"})}), scope


def _out(token: Any) -> _Ev:
    return _Ev("TokenAdded", place="eventOut", token=token)


async def test_a_firing_names_the_tokens_it_put() -> None:
    tap, scope = _tap()
    tap.append(_Ev("TokenRemoved", place="done"))
    tap.append(_out("a"))
    tap.append(_Ev("TokenAdded", place="won"))
    tap.append(_Ev("TransitionCompleted", transition="Race_CommitA"))
    assert scope.answered_by == [("Race_CommitA", "a")]
    assert await _answering(scope, "a") == "Race_CommitA"


async def test_a_timeout_branch_is_named_by_its_action_timed_out() -> None:
    """No ``TransitionCompleted`` follows a timeout branch: the next completion is not its."""
    tap, scope = _tap()
    tap.append(_Ev("ActionTimedOut", transition="To_Slow"))
    tap.append(_out("t"))
    # The next firing's tokens may follow with no event between.
    tap.append(_Ev("TokenAdded", place="other"))
    tap.append(_out("m"))
    tap.append(_Ev("TransitionCompleted", transition="To_Medium"))
    assert scope.answered_by == [("To_Slow", "t"), ("To_Medium", "m")]


async def test_tokens_no_event_names_stay_unnamed() -> None:
    tap, scope = _tap()
    tap.append(_out("x"))
    tap.append(_Ev("TransitionFailed", transition="T_Boom"))
    tap.append(_Ev("TransitionCompleted", transition="T_Later"))
    assert scope.answered_by == [(None, "x")]
    assert await _answering(scope, "x") is None


async def test_the_answer_is_the_token_taken_not_the_first_one_put() -> None:
    """A partial (or a token left from before the turn) comes first; the answer is named
    by the token the turn took, which it hands on as a copy keeping its id."""
    tap, scope = _tap()
    partial = Event(author="n", partial=True)
    final = Event(author="n")
    tap.append(_out(partial))
    tap.append(_Ev("TransitionCompleted", transition="Stream_Chunk"))
    tap.append(_out(final))
    tap.append(_Ev("TransitionCompleted", transition="Stream_Final"))
    assert await _answering(scope, final.model_copy()) == "Stream_Final"


async def test_a_subnets_transition_answers_as_its_mount() -> None:
    tap, scope = _tap()
    tap.append(_out("a"))
    tap.append(_Ev("TransitionCompleted", transition="assistant/LlmAgent_EmitAnswer"))
    assert await _answering(scope, "a") == "assistant"


async def test_a_timeout_branchs_answer_is_emitted_under_its_transition(serve: Serve) -> None:
    node = from_config(str(BLUEPRINTS / "bp_timeout" / "root.yaml"))
    assert isinstance(node, PetriNet)
    s = await session(serve(node))
    turn = await s.say("go")
    assert turn.error is None
    # The timeout puts a token with no value: the answer has no output, only its path.
    paths = [e.node_info.path for e in turn.events]
    paths = [p for p in paths if p.startswith("timeout_net@1/To_")]
    assert paths == ["timeout_net@1/To_Slow@1"]


def test_timeout_places_are_read_from_the_spec() -> None:
    node = from_config(str(BLUEPRINTS / "bp_timeout" / "root.yaml"))
    assert isinstance(node, PetriNet)
    assert timeout_places(node.spec) == {"To_Slow": frozenset({"eventOut"})}
