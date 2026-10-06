"""ADK sample ``workflows/parallel_worker``: an LLM lists topics, two parallel-worker nodes fan out.

Recorded trace (``tests/flower.json``, user turn ``flower``): ``process_input``
writes ``topic=flower`` to state; ``find_related_topics`` answers
``["gardening", "plants", "botany"]``; ``make_upper_case`` runs once per item
(``@1``..``@3``) and outputs the list in upper case; ``explain_topic`` runs
once per item, each answering a ``TopicExplanation`` JSON; ``aggregate``
emits one message joining them, no output.

Both models are ``ScriptedLlm`` fakes answering what the trace recorded.
``explain_topic``'s three calls run concurrently on one fake, so it answers by
the topic in the request, not by call order.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.workflow import FunctionNode, Workflow, node

from adk_libpetri.workflow import WorkflowTranslationError, compile_workflow, verify_workflow
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, run, run_both
from . import agent as sample

RELATED = {
    "flower": ["gardening", "plants", "botany"],
    "renewable energy": ["solar power", "wind power", "energy storage"],
}
EXPLANATIONS = {
    "GARDENING": (
        "Gardening is the practice of growing and cultivating plants, and flowers are a "
        "central element in many gardening practices. Gardeners often plant, nurture, and "
        "arrange flowers for their aesthetic beauty, fragrance, or to attract pollinators, "
        "making flowers an integral part of the gardening world."
    ),
    "PLANTS": (
        "A flower is a reproductive part of many types of plants. Plants are the larger "
        "biological kingdom to which flowers belong, as flowers grow on and are integral "
        "components of flowering plants (angiosperms)."
    ),
    "BOTANY": (
        "Botany is the scientific study of plants, including their structure, growth, "
        "reproduction, metabolism, development, diseases, and chemical properties. Flowers "
        "are the reproductive organs of many plants, specifically angiosperms, and are "
        "therefore a primary subject of study within botany, with botanists analyzing their "
        "morphology, physiology, ecology, and evolutionary significance."
    ),
    "SOLAR POWER": "Solar panels turn sunlight into electricity.",
    "WIND POWER": "Turbines turn wind into electricity.",
    "ENERGY STORAGE": "Batteries keep renewable electricity for later.",
}
LEGACY: dict[str, Any] = {"state": "legacy_read"}
"""Both agents' instructions template ``{topic}`` from session state, which the
compiler rejects by default (commitment 2); the sample compiles only as a
legacy read."""
RECORDED_AGGREGATE = "\n\n---\n\n".join(
    f"{t}: {EXPLANATIONS[t]}" for t in ("GARDENING", "PLANTS", "BOTANY")
)


def _request_text(req: LlmRequest) -> str:
    return "".join(p.text or "" for c in req.contents for p in (c.parts or []))


def _find(req: LlmRequest) -> LlmResponse:
    topic = _request_text(req).split("\n")[-1] if req.contents else ""
    for key, related in RELATED.items():
        if key in topic or key in str(req.config.system_instruction):
            return text(json.dumps(related))
    raise AssertionError(f"unscripted find_related_topics request: {topic!r}")


def _explain(req: LlmRequest) -> LlmResponse:
    asked = _request_text(req)
    topic = next(t for t in EXPLANATIONS if t == asked.strip())
    return text(json.dumps({"topic": topic, "explanation": EXPLANATIONS[topic]}))


@dataclass
class Fakes:
    find: list[ScriptedLlm] = field(default_factory=list)
    explain: list[ScriptedLlm] = field(default_factory=list)


def maker(fakes: Fakes, turns: int = 1) -> Any:
    def make() -> Workflow:
        m = importlib.reload(sample)
        find = ScriptedLlm.of(*([_find] * turns))
        explain = ScriptedLlm.of(*([_explain] * 3 * turns))
        fakes.find.append(find)
        fakes.explain.append(explain)
        graph = m.root_agent.graph
        assert graph is not None
        by_name = {n.name: n for n in graph.nodes}
        by_name["find_related_topics"].model = find
        # The graph holds a _ParallelWorker wrapping a clone of the module's agent.
        inner = by_name["explain_topic"]._node
        assert isinstance(inner, LlmAgent)
        inner.model = explain
        return m.root_agent

    return make


def rows(r: Run) -> list[tuple[Any, ...]]:
    """(author, branch, node path, output, text, state delta, error) per event."""
    out = []
    for e in r.events:
        parts = e.content.parts if e.content and e.content.parts else []
        delta = dict(e.actions.state_delta) if e.actions else {}
        out.append(
            (
                e.author,
                e.branch,
                e.node_info.path if e.node_info else None,
                json.dumps(e.output, sort_keys=True, default=str),
                "".join(p.text or "" for p in parts),
                json.dumps(delta, sort_keys=True),
                e.error_code,
            )
        )
    return out


def requests(fake: ScriptedLlm) -> list[tuple[str, str]]:
    return sorted((str(r.config.system_instruction), _request_text(r)) for r in fake.requests)


async def test_native_run_reproduces_the_recorded_trace() -> None:
    fakes = Fakes()
    r = await run(maker(fakes)(), ["flower"])
    assert r.state == {"topic": "flower"}
    assert r.authors == ["explain_topic", "find_related_topics", "root_agent"]
    outputs = {e.node_info.path: e.output for e in r.events if e.output is not None}
    assert outputs["root_agent@1/find_related_topics@1"] == ["gardening", "plants", "botany"]
    assert outputs["root_agent@1/make_upper_case@1"] == ["GARDENING", "PLANTS", "BOTANY"]
    assert outputs["root_agent@1/make_upper_case@1/make_upper_case@2"] == "PLANTS"
    assert outputs["root_agent@1/explain_topic@1/explain_topic@3"] == {
        "topic": "BOTANY",
        "explanation": EXPLANATIONS["BOTANY"],
    }
    assert [t["topic"] for t in outputs["root_agent@1/explain_topic@1"]] == [
        "GARDENING",
        "PLANTS",
        "BOTANY",
    ]
    assert r.texts[-1] == RECORDED_AGGREGATE
    # The {topic} instruction template read state written by process_input.
    assert {si for si, _ in requests(fakes.explain[0])} == {
        'Explain how the following topic relates the the original topic: "flower".'
    }


async def test_compiled_run_matches_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    fakes = Fakes()
    native, petri, _ = await run_both(maker(fakes), ["flower"], orchestrator, **LEGACY)
    assert petri.final_output == native.final_output
    assert petri.authors == native.authors
    assert petri.texts == native.texts
    assert petri.texts[-1] == RECORDED_AGGREGATE
    assert petri.state == native.state == {"topic": "flower"}
    assert rows(petri) == rows(native)
    # Each model saw the same requests (instructions with state injected, inputs).
    assert requests(fakes.find[1]) == requests(fakes.find[0])
    assert requests(fakes.explain[1]) == requests(fakes.explain[0])


async def test_two_turns_match_native_and_overwrite_the_topic(orchestrator) -> None:  # type: ignore[no-untyped-def]
    fakes = Fakes()
    native, petri, _ = await run_both(
        maker(fakes, turns=2), ["flower", "renewable energy"], orchestrator, **LEGACY
    )
    assert petri.state == native.state == {"topic": "renewable energy"}
    assert petri.texts == native.texts
    assert petri.texts[-1].startswith("SOLAR POWER: ")
    assert requests(fakes.find[1]) == requests(fakes.find[0])
    assert requests(fakes.explain[1]) == requests(fakes.explain[0])


def test_the_sample_is_rejected_by_default_for_its_instruction_templates() -> None:
    with pytest.raises(WorkflowTranslationError) as err:
        compile_workflow(maker(Fakes())())
    rejected = {(f.subject, f.message) for f in err.value.report.rejected}
    reason = (
        "reads session state (instruction {topic}) (commitment 2: the marking is the state); "
        "pass state='legacy_read' to accept"
    )
    assert rejected == {("find_related_topics", reason), ("explain_topic", reason)}


def test_translation_report() -> None:
    report = compile_workflow(maker(Fakes())(), **LEGACY).report
    assert not report.rejected
    # make_upper_case's inner FunctionNode is checked through its _ParallelWorker.
    assert {f.subject for f in report.of("exact")} == {
        "process_input",
        "make_upper_case",
        "aggregate",
    }
    worker = "parallel worker: the per-item fan-out runs inside one transition, by ADK"
    assert {(f.subject, f.message) for f in report.of("opaque")} == {
        ("find_related_topics", "LlmAgent, run by ADK's node runner as one transition"),
        ("make_upper_case", worker),
        ("explain_topic", worker),
        ("explain_topic", "LlmAgent, run by ADK's node runner as one transition"),
    }
    assert {(f.subject, f.message) for f in report.of("approximated")} >= {
        ("find_related_topics", "reads legacy session state (instruction {topic})"),
        ("explain_topic", "reads legacy session state (instruction {topic})"),
    }
    # One straight chain: no fan-out edges, so no fan-out-order approximation.
    assert {f.subject for f in report.of("approximated")} == {
        "find_related_topics",
        "explain_topic",
        "branches",
        "event replay",
    }


def test_report_names_the_instruction_template_state_reads() -> None:
    report = compile_workflow(maker(Fakes())(), **LEGACY).report
    noted = {f.subject for f in report.findings if "state" in f.message}
    assert {"find_related_topics", "explain_topic"} <= noted


def _reads_topic(node_input: str, topic: str) -> str:
    return f"{topic}:{node_input}"


def test_a_plain_function_node_reading_state_is_rejected() -> None:
    wf = Workflow(name="w", edges=[("START", FunctionNode(func=_reads_topic, name="reads"))])
    with pytest.raises(WorkflowTranslationError, match="reads session state"):
        compile_workflow(wf)


def test_a_parallel_worker_reading_state_is_rejected_too() -> None:
    worker = node(_reads_topic, name="reads", parallel_worker=True)
    wf = Workflow(name="w", edges=[("START", worker)])
    with pytest.raises(WorkflowTranslationError, match="reads session state"):
        compile_workflow(wf)


def test_a_parallel_worker_reading_state_compiles_as_a_legacy_read() -> None:
    worker = node(_reads_topic, name="reads", parallel_worker=True)
    report = compile_workflow(Workflow(name="w", edges=[("START", worker)]), **LEGACY).report
    assert ("reads", "reads legacy session state (parameter topic)") in {
        (f.subject, f.message) for f in report.of("approximated")
    }


@requires_z3
def test_every_safety_claim_and_deadlock_freedom_are_proven() -> None:
    proofs = verify_workflow(compile_workflow(maker(Fakes())(), **LEGACY), k=1)
    verdicts = {p.label: p.result.verdict for p in proofs}
    assert {p.kind for p in proofs} == {"safety", "deadlock"}
    # 2 turn claims, aggregate's terminal output, 5 serial nodes, deadlock freedom.
    assert len(verdicts) == 9
    assert "aggregate keeps one output: place_bound(aggregate/terminalOutput, 1)" in verdicts
    assert all(v == "proven" for v in verdicts.values()), verdicts
