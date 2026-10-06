"""ADK sample ``workflows/use_as_output``: a node delegates its output to a dynamic child.

``orchestrate`` runs the ``summarizer`` agent through
``ctx.run_node(..., use_as_output=True)``; ``finalize`` receives the summary.
The recorded trace (``tests/go.json``) is one turn, ``go``: the summarizer asks
for text (its event is the output of both itself and ``orchestrate``), and
``finalize`` emits ``final: <that text>`` as the workflow's output.
"""

from __future__ import annotations

import importlib
from typing import Any

from google.adk.workflow import Workflow

from adk_libpetri.workflow import compile_workflow, verify_workflow
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, run, run_both

SUMMARY = "Please provide the text you would like me to summarize!"
SECOND = "A fox jumped over a dog."
SCRIPT = [SUMMARY, SECOND]


def _text(c: Any) -> str:
    return "".join(p.text or "" for p in c.parts or [])


class Sample:
    """Fresh nodes and a fresh summarizer fake per run; keeps the fakes."""

    def __init__(self) -> None:
        self.fakes: list[ScriptedLlm] = []

    def __call__(self) -> Workflow:
        from . import agent

        mod = importlib.reload(agent)
        # The summarizer is not a graph node (orchestrate runs it dynamically).
        mod.summarizer.model = fake = ScriptedLlm.of(*(text(t) for t in SCRIPT))
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
    assert r.texts == [SUMMARY]
    assert r.final_output == f"final: {SUMMARY}"
    assert r.authors == ["root_agent", "summarizer"]
    assert shape(r) == [
        (
            "summarizer",
            "root_agent@1/orchestrate@1/summarizer@1",
            ("root_agent@1/orchestrate@1/summarizer@1", "root_agent@1/orchestrate@1"),
            None,
        ),
        (
            "root_agent",
            "root_agent@1/finalize@1",
            ("root_agent@1/finalize@1", "root_agent@1"),
            f"final: {SUMMARY}",
        ),
    ]
    assert asked(make.fakes[0]) == [["go"]]


async def test_compiled_run_matches_native_output_authors_and_texts(orchestrator) -> None:  # type: ignore[no-untyped-def]
    make = Sample()
    native, petri, _ = await run_both(make, ["go", "again"], orchestrator)
    assert [t[-1].output for t in petri.turns] == [t[-1].output for t in native.turns]
    assert petri.final_output == native.final_output == f"final: {SECOND}"
    assert petri.authors == native.authors
    assert petri.texts == native.texts == [SUMMARY, SECOND]
    assert asked(make.fakes[1]) == asked(make.fakes[0])
    assert petri.state == native.state


async def test_dynamic_child_delegation_is_preserved_in_the_compiled_run(orchestrator) -> None:  # type: ignore[no-untyped-def]
    # The delegation happens inside the opaque FunctionNode run, so ADK's own
    # node runner stamps the child's event exactly as natively.
    native, petri, _ = await run_both(Sample(), ["go"], orchestrator)
    assert shape(petri)[0] == shape(native)[0]


async def test_compiled_event_stream_matches_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    # finalize runs with use_as_output, so its event is also the workflow's
    # output and PetriWorkflow yields no event of its own.
    native, petri, _ = await run_both(Sample(), ["go", "again"], orchestrator)
    assert shape(petri) == shape(native)
    assert [shape(Run(events=t)) for t in petri.turns] == [
        shape(Run(events=t)) for t in native.turns
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
            "root_agent@1/orchestrate@1/summarizer@1",
            "root_agent@1/finalize@1",
        ]
    )


def test_report_compiles_both_function_nodes_exactly() -> None:
    cw = compile_workflow(Sample()())
    report = cw.report
    assert not report.rejected
    assert {f.subject for f in report.of("exact")} == {"orchestrate", "finalize"}
    # orchestrate takes ctx: the summarizer it runs via ctx.run_node is
    # ADK-scheduled inside its transition, outside the proofs.
    (ctx_finding,) = report.of("opaque")
    assert ctx_finding.subject == "orchestrate"
    assert "ctx.run_node" in ctx_finding.message
    assert {f.subject for f in report.of("approximated")} == {"branches", "event replay"}
    assert cw.node_names == ["orchestrate", "finalize"]


@requires_z3
def test_every_workflow_claim_is_proven() -> None:
    cw = compile_workflow(Sample()())
    proofs = {p.label: p for p in verify_workflow(cw, k=1)}
    assert set(proofs) == {
        "one turn at a time: place_bound(turnActive, 1)",
        "permit never doubles: place_bound(turnPermit, 1)",
        "finalize keeps one output: place_bound(finalize/terminalOutput, 1)",
        "orchestrate runs serially: place_bound(orchestrate/idle, 1)",
        "finalize runs serially: place_bound(finalize/idle, 1)",
        "deadlock_free",
    }
    assert {label for label, p in proofs.items() if not p.proven} == set()
    assert {label for label, p in proofs.items() if p.kind != "safety"} == {"deadlock_free"}
    assert proofs["deadlock_free"].kind == "deadlock"
