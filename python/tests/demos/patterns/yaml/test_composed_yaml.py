"""``composed_agent.yaml``: Pattern A's twin mounted twice, plus a stock ``llm_agent``.

The user's message forks to two instances of ``yaml_race/root_agent.yaml``
(``first/...`` and ``second/...``); a node joins both winners into one
question for a stock ``llm_agent`` subnet configured from ``summarizer``, an
``LlmAgent`` YAML whose model a test swaps for a ``ScriptedLlm``. The result
is one flat net: ``prove:`` checks it as a whole, and it runs one turn.
"""

from __future__ import annotations

import json
from typing import Any

from google.adk.agents.config_agent_utils import from_config
from google.adk.agents.llm_agent import LlmAgent

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.net import PetriNet
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from ._drive import HERE, agent_module, run_turn_then_drain

COMPOSED = HERE / "composed_agent.yaml"
RACE = HERE / "yaml_race" / "root_agent.yaml"
BOUND = {"first": {"q1": "userIn", "a1": "eventOut"}, "second": {"q2": "userIn", "a2": "eventOut"}}


def composed() -> PetriNet:
    node = from_config(str(COMPOSED))
    assert isinstance(node, PetriNet)
    return node


def _as_race(transitions: list[dict[str, Any]], inst: str) -> str:
    """``inst``'s transitions, its prefix stripped and its bound places renamed back."""
    s = json.dumps(sorted(transitions, key=lambda t: t["name"]), sort_keys=True)
    for parent, port in BOUND[inst].items():
        s = s.replace(f'"{parent}"', f'"{port}"')
    return s.replace(f'"{inst}/', '"')


def test_each_race_instance_is_the_race_twin_under_its_prefix() -> None:
    node = composed()
    spec = node.spec
    race = from_config(str(RACE))
    assert isinstance(race, PetriNet)
    want = json.dumps(
        sorted(race.spec.fingerprint()["transitions"], key=lambda t: t["name"]), sort_keys=True
    )
    fp = spec.fingerprint()
    for inst in ("first", "second"):
        mine = [t for t in fp["transitions"] if t["name"].startswith(f"{inst}/")]
        assert _as_race(mine, inst) == want
        assert spec.subnet_of(f"{inst}/Race_CommitA") == inst
        assert spec.has_place(f"{inst}/racePermit")
    assert spec.subnet_of("assistant/LlmAgent_StartTurn") == "assistant"
    assert node.blueprint.seeds["assistant/turnPermit"] == (None,)
    # The two races share nothing but the fork's message: no place of one is the other's.
    places = {p["name"] for p in fp["places"]}
    assert {p for p in places if p.startswith("first/")} == {
        p.replace("second/", "first/", 1) for p in places if p.startswith("second/")
    }


@requires_z3
def test_the_composed_net_proves_as_a_whole() -> None:
    proofs = composed().verify()
    assert [p.label for p in proofs] == [
        "place_bound(first/raceWon, 1)",
        "place_bound(second/raceWon, 1)",
        "place_bound(eventOut, 1)",
        "deadlock_free",
    ]
    failed = {p.label: f"{p.result.verdict}\n{p.result.report}" for p in proofs if not p.proven}
    assert not failed, failed


async def test_one_turn_runs_both_races_then_the_assistant(orch: OrchestratorLoop) -> None:
    race = agent_module("yaml_race")
    race.FINISHED.clear()
    llm = ScriptedLlm.of(text("Both branches said fast."))

    def scripted(node: PetriNet) -> None:
        # The session's runner resolves the LlmAgent's model when it starts.
        [summarizer] = [
            n
            for item in node.nodes
            if isinstance(item, list | tuple)
            for n in item
            if isinstance(n, LlmAgent)
        ]
        summarizer.model = llm

    run = await run_turn_then_drain(orch, COMPOSED, text="which branch wins?", configure=scripted)

    event, at = run.answer()
    assert event.content is not None and event.content.parts
    assert event.content.parts[0].text == "Both branches said fast."
    # The assistant got both races' winners, and answered before either slow loser.
    [request] = llm.requests
    asked = [p.text for c in request.contents for p in c.parts or [] if p.text]
    assert asked[-1] == "Summarize: answer from fast | answer from fast"
    assert at < race.SLOW, f"answer after {at:.3f}s; both races are first-wins"

    # Drained: each race committed once, and each drained its two losers.
    assert len(run.egress) == 1
    assert sorted(race.FINISHED) == sorted(["fast", "medium", "slow"] * 2)
    for inst in ("first", "second"):
        assert run.final.count(f"{inst}/raceWon") == 1
        assert run.final.count(f"{inst}/racePermit") == 0
        assert sorted(r.branch_id for r in run.final.tokens(f"{inst}/raceDiscarded")) == [
            "medium",
            "slow",
        ]
    assert run.final.count("assistant/turnPermit") == 1  # the assistant is at rest
