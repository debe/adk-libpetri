"""ADK sample ``workflows/dynamic_nodes``: a ``while True`` loop over ``ctx.run_node``.

The graph is one node, ``START -> orchestrate``. The node writes
``state["topic"]``, then loops: ``generate_headline`` (its instruction reads
``{topic}`` and ``{feedback?}`` from session state), ``evaluate_headline``
(``output_schema=Feedback``, ``output_key="feedback"``), until the grade is
``tech-related``; the accepted headline is the node's (and the workflow's)
output.

The recorded trace (``tests/flower.json``) is one turn, ``flower``: one
``unrelated`` round, then an accepted headline. The model texts below are the
trace's. A second turn (``quantum mechanics``, a README sample input, accepted
first time) checks that the compiled net serves later turns of the session.

What the compiler sees: one FunctionNode whose parameters are ``ctx`` and
``node_input``. Its body writes session state (``Event(state=...)``) but never
reads ``ctx.state``, so there is no state rejection; the children that read
state through instruction templates are ``ctx.run_node`` children, not graph
nodes. The report says so with an ``opaque`` finding (the node takes ``ctx``)
next to the ``exact`` one. The children and the unbounded loop stay ADK's.

The terminal node runs with ``use_as_output``, so its own output event names
the workflow in ``output_for`` and ``PetriWorkflow`` emits nothing more, as in
ADK.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

from google.adk.workflow import Workflow

from adk_libpetri.workflow import compile_workflow, verify_workflow
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, run, run_both

# From tests/flower.json (events e-3..e-6).
H1 = "A World of Petals"
F1 = {
    "grade": "unrelated",
    "feedback": (
        "This headline sounds like it's about botany, gardening, or a natural theme. To make "
        "it more tech-focused, consider incorporating terms like 'AI', 'software', 'virtual "
        "reality', 'digital', 'engineering', 'innovation', 'data', or 'development'. For "
        "example, 'The AI-Powered World of Digital Petals' or 'Engineering a Virtual Reality "
        "of Petals'."
    ),
}
H2 = "**Digital Petals: Engineering AI-Enhanced Blooms**"
F2 = {
    "grade": "tech-related",
    "feedback": (
        "This headline is clearly tech-related, specifically mentioning 'Engineering' and "
        "'AI-Enhanced', which are direct ties to technology and software engineering."
    ),
}
# Second turn (not recorded): accepted on the first round.
H3 = "Quantum Software Engineering Goes Mainstream"
F3 = {"grade": "tech-related", "feedback": "Software engineering is named directly."}

TURNS = ["flower", "quantum mechanics"]


def _json(d: dict[str, Any]) -> str:
    return json.dumps({"grade": d["grade"], "feedback": d["feedback"]})


class Sample:
    """Fresh nodes and fresh fakes per run (``run_both`` calls it twice)."""

    def __init__(self) -> None:
        self.fakes: list[dict[str, ScriptedLlm]] = []

    def __call__(self) -> Workflow:
        from . import agent

        mod = importlib.reload(agent)
        # Both agents are run with ctx.run_node, not graph nodes, so
        # llm_agents() does not see them: assign the fakes directly.
        gen = ScriptedLlm.of(text(H1), text(H2), text(H3))
        ev = ScriptedLlm.of(text(_json(F1)), text(_json(F2)), text(_json(F3)))
        mod.generate_headline.model = gen
        mod.evaluate_headline.model = ev
        self.fakes.append({"generate_headline": gen, "evaluate_headline": ev})
        return mod.root_agent


def _instructions(fake: ScriptedLlm) -> list[str]:
    return [str(r.config.system_instruction) if r.config else "" for r in fake.requests]


def _output_events(r: Run) -> list[tuple[str, str, list[str] | None, Any]]:
    return [
        (e.author, e.node_info.path, e.node_info.output_for, e.output)
        for e in r.events
        if e.output is not None
    ]


async def test_native_run_reproduces_the_recorded_trace() -> None:
    sample = Sample()
    native = await run(sample(), TURNS[:1])
    assert native.final_output == H2
    assert native.authors == ["evaluate_headline", "generate_headline", "root_agent"]
    assert native.texts == [H1, _json(F1), H2, _json(F2)]
    assert native.state == {"topic": "flower", "feedback": F2}
    paths = [e.node_info.path for e in native.events if e.node_info]
    assert paths == [
        "root_agent@1/orchestrate@1",
        "root_agent@1/orchestrate@1/generate_headline@1",
        "root_agent@1/orchestrate@1/evaluate_headline@1",
        "root_agent@1/orchestrate@1/generate_headline@2",
        "root_agent@1/orchestrate@1/evaluate_headline@2",
        "root_agent@1/orchestrate@1",
    ]
    # The trace's last event: the output, attributed to the node and the workflow.
    assert _output_events(native)[-1] == (
        "root_agent",
        "root_agent@1/orchestrate@1",
        ["root_agent@1/orchestrate@1", "root_agent@1"],
        H2,
    )
    # The second round's prompt carries the first round's feedback from state.
    first, second = _instructions(sample.fakes[0]["generate_headline"])
    assert 'topic "flower"' in first
    assert "The feedback: \n" in first
    assert repr(F1) in second


async def test_compiled_run_matches_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    sample = Sample()
    native, petri, _ = await run_both(sample, TURNS, orchestrator)
    assert native.final_output == H3
    assert petri.final_output == native.final_output
    assert [[e.output for e in t if e.output is not None][-1] for t in petri.turns] == [H2, H3]
    assert petri.authors == native.authors
    assert petri.texts == native.texts == [H1, _json(F1), H2, _json(F2), H3, _json(F3)]
    assert petri.state == native.state == {"topic": "quantum mechanics", "feedback": F3}
    n_fakes, p_fakes = sample.fakes
    for name in ("generate_headline", "evaluate_headline"):
        assert _instructions(p_fakes[name]) == _instructions(n_fakes[name])
    # Session state deltas in the same order.
    deltas = [
        [e.actions.state_delta for e in r.events if e.actions and e.actions.state_delta]
        for r in (native, petri)
    ]
    assert deltas[0] == deltas[1]


async def test_compiled_run_emits_the_output_once_like_adk(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """ADK runs a terminal node with ``use_as_output=True`` (``_workflow.py:675``),
    so the node's own output event names the workflow in ``output_for`` and the
    workflow emits nothing more (``_output_delegated``, ``_workflow.py:274``).
    The compiled net runs the terminal node the same way, and ``PetriWorkflow``
    yields no event of its own: one output event per turn, at the node path."""
    native, petri, _ = await run_both(Sample(), TURNS, orchestrator)
    assert _output_events(petri) == _output_events(native)
    # The workflow's outputs: one per turn, on the node's own event.
    assert [o for o in _output_events(petri) if "root_agent@1" in (o[2] or [])] == [
        (
            "root_agent",
            "root_agent@1/orchestrate@1",
            ["root_agent@1/orchestrate@1", "root_agent@1"],
            h,
        )
        for h in (H2, H3)
    ]
    # Node paths restart every turn (run ids are per workflow run).
    assert [e.node_info.path for e in petri.events if e.node_info] == [
        e.node_info.path for e in native.events if e.node_info
    ]


def test_translation_report() -> None:
    compiled = compile_workflow(Sample()())
    report = compiled.report
    assert not report.rejected
    assert compiled.node_names == ["orchestrate"]
    # No state rejection: orchestrate's parameters are ``ctx`` and
    # ``node_input`` and its body never reads ``ctx.state`` (it only writes).
    # Its children's state-templated instructions are ctx.run_node children,
    # covered by the opaque finding, not by the state rule.
    assert [(f.subject, f.message) for f in report.of("exact")] == [
        ("orchestrate", "FunctionNode, run by ADK's node runner")
    ]
    assert {f.subject for f in report.of("approximated")} == {"branches", "event replay"}
    assert [(f.subject, f.message) for f in report.of("opaque")] == [
        (
            "orchestrate",
            "takes ctx: children it runs via ctx.run_node are ADK-scheduled inside"
            " this transition, and the proofs do not see them",
        )
    ]
    assert not any("session state" in f.message for f in report.findings)
    # state="legacy_read" changes nothing: no graph node reads state.
    legacy = compile_workflow(Sample()(), state="legacy_read").report
    assert [str(f) for f in legacy.findings] == [str(f) for f in report.findings]


@requires_z3
def test_proofs() -> None:
    proofs = {p.label: p for p in verify_workflow(compile_workflow(Sample()()), k=1)}
    assert set(proofs) == {
        "one turn at a time: place_bound(turnActive, 1)",
        "permit never doubles: place_bound(turnPermit, 1)",
        "orchestrate keeps one output: place_bound(orchestrate/terminalOutput, 1)",
        "orchestrate runs serially: place_bound(orchestrate/idle, 1)",
        "deadlock_free",
    }
    # Proven of the one-transition net. The while-True loop inside the
    # transition is not modelled, so deadlock_free says nothing about it.
    assert all(p.proven for p in proofs.values()), {k: p.result.verdict for k, p in proofs.items()}
    assert proofs["deadlock_free"].kind == "deadlock"
    assert {p.kind for label, p in proofs.items() if label != "deadlock_free"} == {"safety"}
