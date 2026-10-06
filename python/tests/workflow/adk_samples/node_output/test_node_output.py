"""ADK sample ``workflows/node_output``: how outputs pass along edges.

A raw string, an explicit ``Event(output=...)``, an ``output_schema`` agent's
dict, and a function that coerces that dict back into ``TopicDetails``. The
recorded trace (``tests/go.json``) is one turn, ``go``.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

from google.adk.workflow import Workflow

from adk_libpetri.workflow import compile_workflow, verify_workflow
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, llm_agents, run, run_both

TOPIC = {
    "title": "The Impulse to Go: Decoding Humanity's Perpetual Motion",
    "description": (
        "Investigating the fundamental human drive to 'go' - exploring its manifestations from "
        "ancient migrations and pioneering expeditions to the relentless pursuit of progress in "
        "science, technology, and personal growth, and what happens when we pause."
    ),
    "category": "Human Behavior & Future Studies",
}
MODEL_TEXT = json.dumps(TOPIC)
WORKFLOW = "root_agent@1"
SECOND = {"title": "Gardens", "description": "Start small.", "category": "Hobbies"}


def consumed(topic: dict[str, str]) -> str:
    return (
        "Received Pydantic Model!\n"
        f"Title: {topic['title']}\n"
        f"Description: {topic['description']}\n"
        f"Category: {topic['category']}"
    )


def _text(c: Any) -> str:
    return "".join(p.text or "" for p in c.parts or [])


class Sample:
    def __init__(self) -> None:
        self.fakes: list[ScriptedLlm] = []

    def __call__(self) -> Workflow:
        from . import agent

        mod = importlib.reload(agent)
        (llm,) = llm_agents(mod.root_agent)
        llm.model = fake = ScriptedLlm.of(text(MODEL_TEXT), text(json.dumps(SECOND)))
        self.fakes.append(fake)
        return mod.root_agent


def asked(fake: ScriptedLlm) -> list[list[str]]:
    return [[_text(c) for c in r.contents] for r in fake.requests]


def shape(r: Run) -> list[tuple[Any, ...]]:
    return [
        (e.author, e.node_info.path, tuple(e.node_info.output_for or ()), e.output)
        for e in r.events
    ]


async def test_native_run_reproduces_adk_trace() -> None:
    make = Sample()
    r = await run(make(), ["go"])
    assert [e.output for e in r.events] == [
        "Processed input: go",
        "Event wrapped output: Processed input: go",
        TOPIC,
        consumed(TOPIC),
    ]
    assert [e.author for e in r.events] == [
        "root_agent",
        "root_agent",
        "generate_pydantic_output",
        "root_agent",
    ]
    assert r.texts == [MODEL_TEXT]
    assert r.events[-1].node_info.output_for == [
        "root_agent@1/consume_pydantic_output@1",
        "root_agent@1",
    ]
    assert asked(make.fakes[0]) == [["Event wrapped output: Processed input: go"]]


async def test_compiled_run_matches_native_outputs_authors_and_texts(orchestrator) -> None:  # type: ignore[no-untyped-def]
    make = Sample()
    native, petri, _ = await run_both(make, ["go", "gardening tips for beginners"], orchestrator)
    assert petri.final_output == native.final_output == consumed(SECOND)
    assert [t[-1].output for t in petri.turns] == [t[-1].output for t in native.turns]
    assert petri.authors == native.authors
    assert petri.texts == native.texts
    assert asked(make.fakes[1]) == asked(make.fakes[0])
    assert petri.state == native.state


async def test_compiled_node_outputs_match_native_per_node(orchestrator) -> None:  # type: ignore[no-untyped-def]
    # Every node's own output event, str / Event / schema dict / coerced model.
    native, petri, _ = await run_both(Sample(), ["go"], orchestrator)

    def per_node(r: Run) -> list[tuple[str, Any]]:
        return [(e.node_info.path, e.output) for e in r.events]

    assert per_node(petri) == per_node(native)


async def test_compiled_event_stream_matches_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    # consume_pydantic_output runs with use_as_output, so its event is also
    # the workflow's output and PetriWorkflow yields no event of its own.
    native, petri, _ = await run_both(Sample(), ["go", "again"], orchestrator)
    assert shape(petri) == shape(native)
    assert all(e.node_info.path != WORKFLOW for e in petri.events)
    for turn in petri.turns:
        assert turn[-1].node_info.output_for == [
            f"{WORKFLOW}/consume_pydantic_output@1",
            WORKFLOW,
        ]


async def test_compiled_node_paths_restart_each_turn_like_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(Sample(), ["go", "again"], orchestrator)

    def paths(r: Run) -> list[str]:
        return [e.node_info.path for e in r.events]

    assert (
        paths(petri)
        == paths(native)
        == 2
        * [
            f"{WORKFLOW}/generate_string_output@1",
            f"{WORKFLOW}/generate_event_output@1",
            f"{WORKFLOW}/generate_pydantic_output@1",
            f"{WORKFLOW}/consume_pydantic_output@1",
        ]
    )


def test_report_lists_three_exact_function_nodes_and_one_opaque_agent() -> None:
    cw = compile_workflow(Sample()())
    report = cw.report
    assert not report.rejected
    assert {f.subject for f in report.of("exact")} == {
        "generate_string_output",
        "generate_event_output",
        "consume_pydantic_output",
    }
    assert {f.subject for f in report.of("opaque")} == {"generate_pydantic_output"}
    assert {f.subject for f in report.of("approximated")} == {"branches", "event replay"}


@requires_z3
def test_every_workflow_claim_is_proven() -> None:
    cw = compile_workflow(Sample()())
    proofs = {p.label: p for p in verify_workflow(cw, k=1)}
    nodes = [
        "generate_string_output",
        "generate_event_output",
        "generate_pydantic_output",
        "consume_pydantic_output",
    ]
    assert set(proofs) == {
        "one turn at a time: place_bound(turnActive, 1)",
        "permit never doubles: place_bound(turnPermit, 1)",
        "consume_pydantic_output keeps one output: "
        "place_bound(consume_pydantic_output/terminalOutput, 1)",
        *(f"{n} runs serially: place_bound({n}/idle, 1)" for n in nodes),
        "deadlock_free",
    }
    assert {label for label, p in proofs.items() if not p.proven} == set()
    assert {label for label, p in proofs.items() if p.kind != "safety"} == {"deadlock_free"}
    assert proofs["deadlock_free"].kind == "deadlock"
