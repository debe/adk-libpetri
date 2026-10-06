"""ADK sample ``workflows/sequence``: two LLM agents in an unconditional chain.

The recorded trace (``tests/go.json`` in the sample) is one turn, ``go``: the
fruit agent says ``Apple``, the benefit agent answers with a fibre fact, and
the benefit agent's event is the workflow's output (``outputFor`` names
``root_agent@1``). No event carries an ``output`` field: both agents emit
``messageAsOutput``.
"""

from __future__ import annotations

import importlib
from typing import Any

from google.adk.workflow import Workflow

from adk_libpetri.workflow import compile_workflow, verify_workflow
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, llm_agents, run, run_both

FRUIT = "Apple"
BENEFIT = (
    "Apples are a good source of **dietary fiber**, particularly soluble fiber like pectin. "
    "This can help with digestion, promote a feeling of fullness (aiding in weight "
    "management), and may contribute to lowering cholesterol levels."
)
SCRIPT = {
    "generate_fruit_agent": [FRUIT, "Banana"],
    "generate_benefit_agent": [BENEFIT, "Bananas are rich in potassium."],
}


def _text(c: Any) -> str:
    return "".join(p.text or "" for p in c.parts or [])


class Sample:
    """Builds fresh nodes and fresh fakes per run; keeps the fakes of each run."""

    def __init__(self) -> None:
        self.fakes: list[dict[str, ScriptedLlm]] = []

    def __call__(self) -> Workflow:
        from . import agent

        mod = importlib.reload(agent)
        fakes = {}
        for a in llm_agents(mod.root_agent):
            a.model = fakes[a.name] = ScriptedLlm.of(*(text(t) for t in SCRIPT[a.name]))
        self.fakes.append(fakes)
        return mod.root_agent


def requests(fakes: dict[str, ScriptedLlm]) -> dict[str, list[list[str]]]:
    """What each agent was asked: the text of every content of every request."""
    return {name: [[_text(c) for c in r.contents] for r in f.requests] for name, f in fakes.items()}


def shape(r: Run) -> list[tuple[Any, ...]]:
    return [
        (e.author, e.node_info.path, tuple(e.node_info.output_for or ()), e.output)
        for e in r.events
    ]


async def test_native_run_reproduces_adk_trace() -> None:
    make = Sample()
    r = await run(make(), ["go"])
    assert r.texts == [FRUIT, BENEFIT]
    assert r.authors == ["generate_benefit_agent", "generate_fruit_agent"]
    assert [e.node_info.path for e in r.events] == [
        "root_agent@1/generate_fruit_agent@1",
        "root_agent@1/generate_benefit_agent@1",
    ]
    assert r.events[-1].node_info.output_for == [
        "root_agent@1/generate_benefit_agent@1",
        "root_agent@1",
    ]
    assert all(e.node_info.message_as_output for e in r.events)
    assert r.final_output is None  # message_as_output: no event carries `output`
    # The fruit is the benefit agent's input.
    assert requests(make.fakes[0])["generate_benefit_agent"] == [[FRUIT]]


async def test_compiled_run_matches_native_texts_and_model_inputs(orchestrator) -> None:  # type: ignore[no-untyped-def]
    make = Sample()
    native, petri, _ = await run_both(make, ["go", "again"], orchestrator)
    assert petri.texts == native.texts == [FRUIT, BENEFIT, *(t[1] for t in SCRIPT.values())]
    assert requests(make.fakes[1]) == requests(make.fakes[0])
    assert petri.state == native.state


async def test_compiled_event_stream_matches_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(Sample(), ["go", "again"], orchestrator)
    assert petri.authors == native.authors
    assert petri.final_output == native.final_output is None
    assert shape(petri) == shape(native)
    # The terminal agent runs with use_as_output: its event is the workflow's.
    for turn in petri.turns:
        assert turn[-1].node_info.output_for == [
            "root_agent@1/generate_benefit_agent@1",
            "root_agent@1",
        ]
    # PetriWorkflow yields no event of its own.
    assert all(e.author != "root_agent" for e in petri.events)


async def test_compiled_node_paths_restart_each_turn_like_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(Sample(), ["go", "again"], orchestrator)

    def paths(r: Run) -> list[str]:
        return [e.node_info.path for e in r.events]

    assert (
        paths(petri)
        == paths(native)
        == 2
        * [
            "root_agent@1/generate_fruit_agent@1",
            "root_agent@1/generate_benefit_agent@1",
        ]
    )


def test_report_runs_both_agents_opaque_and_rejects_nothing() -> None:
    cw = compile_workflow(Sample()())
    report = cw.report
    assert not report.rejected
    assert {f.subject for f in report.of("opaque")} == {
        "generate_fruit_agent",
        "generate_benefit_agent",
    }
    assert {f.subject for f in report.of("approximated")} == {"branches", "event replay"}
    assert report.of("exact") == []
    assert cw.node_names == ["generate_fruit_agent", "generate_benefit_agent"]


@requires_z3
def test_every_workflow_claim_is_proven() -> None:
    cw = compile_workflow(Sample()())
    proofs = {p.label: p for p in verify_workflow(cw, k=1)}
    assert set(proofs) == {
        "one turn at a time: place_bound(turnActive, 1)",
        "permit never doubles: place_bound(turnPermit, 1)",
        "generate_benefit_agent keeps one output: "
        "place_bound(generate_benefit_agent/terminalOutput, 1)",
        "generate_fruit_agent runs serially: place_bound(generate_fruit_agent/idle, 1)",
        "generate_benefit_agent runs serially: place_bound(generate_benefit_agent/idle, 1)",
        "deadlock_free",
    }
    assert {label for label, p in proofs.items() if not p.proven} == set()
    assert {label for label, p in proofs.items() if p.kind != "safety"} == {"deadlock_free"}
    assert proofs["deadlock_free"].kind == "deadlock"
