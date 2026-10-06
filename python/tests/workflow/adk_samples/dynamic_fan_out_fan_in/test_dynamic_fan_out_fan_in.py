"""ADK sample ``workflows/dynamic_fan_out_fan_in``: fan-out through ``ctx.run_node``.

The graph is one node, ``START -> orchestrator``. The orchestrator splits the
user's comma-separated topics, runs the ``generator`` agent once per topic with
``ctx.run_node(..., use_sub_branch=True)``, gathers the headlines and yields a
Markdown table. The sample has no recorded trace; its README gives two inputs,
both used here, one turn each on one session.

No event carries ``output``: both orchestrator events are ``Event(message=...)``
(content only), so the workflow's final output is ``None`` in ADK too.

The fan-out is invisible to the net: the compiled net has one node transition,
and the generator runs are scheduled by ADK's dynamic scheduler inside that
transition, as in the native run. The report lists the orchestrator as an
``exact`` FunctionNode run by ADK's node runner, and adds an ``opaque`` finding
because it takes ``ctx`` (its ``ctx.run_node`` children are unmodelled).
Node run ids are per workflow run, so every turn restarts at
``dynamic_fan_out_fan_in@1/orchestrator@1`` as in ADK.
"""

from __future__ import annotations

import importlib
from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.adk.workflow import Workflow

from adk_libpetri.workflow import compile_workflow, verify_workflow
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, run, run_both

TURNS = ["AI, Cloud Computing, Quantum Computing", "Python, Go, Rust, TypeScript"]


def _headline(req: LlmRequest) -> Any:
    topic = "".join(p.text or "" for p in req.contents[-1].parts or [])
    return text(f"Breaking: {topic} changes everything")


class Sample:
    """Fresh nodes and a fresh fake per run (``run_both`` calls it twice)."""

    def __init__(self) -> None:
        self.fakes: list[ScriptedLlm] = []

    def __call__(self) -> Workflow:
        from . import agent

        mod = importlib.reload(agent)
        # The generator runs concurrently per topic, so it answers by topic,
        # not by call order.
        fake = ScriptedLlm.of(*([_headline] * 16))
        mod.generator.model = fake
        self.fakes.append(fake)
        return mod.root_agent


def _table(topics: list[str]) -> str:
    rows = "".join(f"| {t} | Breaking: {t} changes everything |\n" for t in topics)
    return "### Aggregated Headlines\n\n| Topic | Headline |\n| :--- | :--- |\n" + rows


def _expected_texts() -> list[str]:
    out: list[str] = []
    for turn in TURNS:
        topics = [t.strip() for t in turn.split(",")]
        out.append(f"Processing {len(topics)} topics in parallel.")
        out += sorted(f"Breaking: {t} changes everything" for t in topics)
        out.append(_table(topics))
    return out


def _shape(r: Run, turns: slice = slice(None)) -> list[tuple[Any, ...]]:
    """Each event as (author, node path, branch, output, text), per turn; the
    concurrent generator events sorted within their turn."""
    shapes: list[tuple[Any, ...]] = []
    for turn in r.turns[turns]:
        rows = []
        for e in turn:
            t = "".join(p.text or "" for p in (e.content.parts if e.content else None) or [])
            path = e.node_info.path if e.node_info else None
            rows.append((e.author, path, e.branch, e.output, t))
        shapes += sorted(rows, key=lambda row: (row[0] == "generator", str(row[1])))
    return shapes


def _texts_sorted_per_turn(r: Run) -> list[str]:
    out: list[str] = []
    for turn in r.turns:
        texts = []
        for e in turn:
            t = "".join(p.text or "" for p in (e.content.parts if e.content else None) or [])
            if t:
                texts.append((e.author, t))
        head = [t for a, t in texts if a != "generator"]
        out += [head[0], *sorted(t for a, t in texts if a == "generator"), *head[1:]]
    return out


async def test_native_run_fans_out_one_generator_per_topic() -> None:
    sample = Sample()
    native = await run(sample(), TURNS)
    assert native.final_output is None  # Event(message=...) is content, not output
    assert native.authors == ["dynamic_fan_out_fan_in", "generator"]
    assert _texts_sorted_per_turn(native) == _expected_texts()
    assert len(sample.fakes[0].requests) == 3 + 4
    gen_paths = sorted(
        e.node_info.path for e in native.turns[0] if e.author == "generator" and e.node_info
    )
    assert gen_paths == [
        f"dynamic_fan_out_fan_in@1/orchestrator@1/generator@{i}" for i in (1, 2, 3)
    ]
    # use_sub_branch=True: one branch per dynamic generator run.
    assert sorted(e.branch for e in native.turns[0] if e.author == "generator") == [
        "generator@1",
        "generator@2",
        "generator@3",
    ]


async def test_compiled_run_matches_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    sample = Sample()
    native, petri, _ = await run_both(sample, TURNS, orchestrator)
    assert petri.final_output == native.final_output is None
    assert petri.authors == native.authors
    assert _texts_sorted_per_turn(petri) == _texts_sorted_per_turn(native) == _expected_texts()
    assert petri.state == native.state
    # Same events, same node paths and sub-branches, every turn.
    assert _shape(petri) == _shape(native)
    assert len(petri.events) == len(native.events)
    # Every generator request carried only its own topic (sub-branch isolation).
    for fake in sample.fakes:
        asked = sorted(
            "".join(p.text or "" for p in r.contents[-1].parts or []) for r in fake.requests
        )
        assert asked == sorted(t.strip() for turn in TURNS for t in turn.split(","))


def test_translation_report() -> None:
    compiled = compile_workflow(Sample()())
    report = compiled.report
    assert not report.rejected
    assert compiled.node_names == ["orchestrator"]
    exact = [(f.subject, f.message) for f in report.of("exact")]
    assert exact == [("orchestrator", "FunctionNode, run by ADK's node runner")]
    assert {f.subject for f in report.of("approximated")} == {"branches", "event replay"}
    # The dynamic generator runs are not graph nodes, but a ctx-taking
    # FunctionNode is flagged: its ctx.run_node children are ADK-scheduled.
    assert [(f.subject, f.message) for f in report.of("opaque")] == [
        (
            "orchestrator",
            "takes ctx: children it runs via ctx.run_node are ADK-scheduled inside"
            " this transition, and the proofs do not see them",
        )
    ]
    assert all(not t.name.startswith("Wf_generator") for t in compiled.spec.transitions)


@requires_z3
def test_proofs() -> None:
    proofs = {p.label: p for p in verify_workflow(compile_workflow(Sample()()), k=1)}
    assert set(proofs) == {
        "one turn at a time: place_bound(turnActive, 1)",
        "permit never doubles: place_bound(turnPermit, 1)",
        "orchestrator keeps one output: place_bound(orchestrator/terminalOutput, 1)",
        "orchestrator runs serially: place_bound(orchestrator/idle, 1)",
        "deadlock_free",
    }
    assert all(p.proven for p in proofs.values()), {k: p.result.verdict for k, p in proofs.items()}
    assert {p.label: p.kind for p in proofs.values()} == {
        label: ("deadlock" if label == "deadlock_free" else "safety") for label in proofs
    }


def test_report_names_the_dynamic_children_as_opaque() -> None:
    """A FunctionNode that takes ``ctx`` can schedule nodes the net never sees
    (here, N concurrent generator runs). The report should say so, as it does
    for a nested ``Workflow`` ("runs as one node; its inner graph is
    ADK-scheduled")."""
    report = compile_workflow(Sample()()).report
    assert any(f.subject == "orchestrator" for f in report.of("opaque"))


async def test_compiled_node_paths_restart_every_turn(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """Native turn 2 runs ``dynamic_fan_out_fan_in@1/orchestrator@1`` again (a new
    invocation, a new workflow run). The compiled net resets its node run ids at
    ``Wf_Start``, so turn 2 runs ``orchestrator@1`` and its generators under
    ``orchestrator@1/generator@N`` too."""
    native, petri, _ = await run_both(Sample(), TURNS, orchestrator)
    assert _shape(petri, slice(1, 2)) == _shape(native, slice(1, 2))
    gen_paths = sorted(
        e.node_info.path for e in petri.turns[1] if e.author == "generator" and e.node_info
    )
    assert gen_paths == [
        f"dynamic_fan_out_fan_in@1/orchestrator@1/generator@{i}" for i in (1, 2, 3, 4)
    ]
