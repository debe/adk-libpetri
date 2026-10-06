"""ADK sample ``workflows/loop_config``: a YAML-defined headline feedback loop.

``root_agent.yaml`` wires ``process_input -> generate_headline ->
evaluate_headline -> route_headline`` and routes ``unrelated`` back to
``generate_headline``. The ``tech-related`` route has no edge: ADK ends the
branch there (it logs "none were matched ... The branch will end"), so the
loop's exit is an unmatched route.

Recorded traces: ``tests/flower.json`` (one ``unrelated`` round, then
``tech-related``) and ``tests/computer.json`` (``tech-related`` at once). Both
LLM agents get a ``ScriptedLlm`` replaying the recorded model texts.

The loader resolves ``.agent.process_input`` and ``loop_config.agent.Feedback``
against ``sys.path`` (ADK runs it from the directory that holds the sample
folders), so the fixture below puts ``adk_samples/`` on ``sys.path`` instead
of editing the YAML.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from google.adk.agents.config_agent_utils import from_config
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow

from adk_libpetri.workflow import (
    LoopBudgetExhausted,
    PetriWorkflow,
    WorkflowTranslationError,
    compile_workflow,
    verify_workflow,
)
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, llm_agents, msg, run, run_both

HERE = Path(__file__).parent
ROOT_YAML = HERE / "root_agent.yaml"
BACK_EDGE = ("route_headline", "generate_headline")

FLOWER_H1 = '"Petal Power: The Timeless Allure of Flowers"'
FLOWER_F1 = {
    "grade": "unrelated",
    "feedback": (
        "To make this headline more tech-focused, consider incorporating elements like AI "
        "for plant recognition, robotics in gardening, biotechnology in horticulture, or "
        "data analytics related to flower cultivation and sales. For example, 'AI-Driven "
        "Botany: The Tech Unlocking Petal Power' or 'Smart Gardens: Engineering the "
        "Timeless Allure of Flowers'."
    ),
}
FLOWER_H2 = "AI-Powered Petals: The Tech Revolution Blooming in Modern Floriculture"
FLOWER_F2 = {
    "grade": "tech-related",
    "feedback": (
        "This headline is strongly tech-related, explicitly mentioning 'AI-Powered' and "
        "'Tech Revolution'. It effectively combines a traditional field (floriculture) "
        "with advanced technology."
    ),
}
COMPUTER_H1 = "Computers: Shaping Our World"
COMPUTER_F1 = {
    "grade": "tech-related",
    "feedback": "This headline is clearly tech-related as it directly discusses computers.",
}

TRACES: dict[str, tuple[str, list[str], list[dict[str, str]]]] = {
    "flower": ("flower", [FLOWER_H1, FLOWER_H2], [FLOWER_F1, FLOWER_F2]),
    "computer": ("computer", [COMPUTER_H1], [COMPUTER_F1]),
}


@pytest.fixture(autouse=True)
def _samples_on_sys_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.syspath_prepend(str(HERE.parent))
    yield


class Factory:
    """``make()`` for ``run_both``: a fresh YAML-loaded workflow and fresh fakes."""

    def __init__(self, headlines: list[str], feedbacks: list[dict[str, str]]) -> None:
        self.headlines = headlines
        self.feedbacks = feedbacks
        self.fakes: list[dict[str, ScriptedLlm]] = []

    def __call__(self) -> Workflow:
        wf = from_config(str(ROOT_YAML))
        assert isinstance(wf, Workflow)
        fakes = {
            "generate_headline": ScriptedLlm.of(*(text(h) for h in self.headlines)),
            "evaluate_headline": ScriptedLlm.of(*(text(json.dumps(f)) for f in self.feedbacks)),
        }
        for a in llm_agents(wf):
            a.model = fakes[a.name]
        self.fakes.append(fakes)
        return wf


def factory(trace: str) -> tuple[str, Factory]:
    user, headlines, feedbacks = TRACES[trace]
    return user, Factory(headlines, feedbacks)


def routes(r: Run) -> list[Any]:
    return [e.actions.route for e in r.events if e.actions and e.actions.route is not None]


def paths(r: Run) -> list[str]:
    return [e.node_info.path for e in r.events if e.node_info and e.node_info.path]


def deltas(r: Run) -> list[dict[str, Any]]:
    return [dict(e.actions.state_delta) for e in r.events if e.actions and e.actions.state_delta]


def instructions(fake: ScriptedLlm) -> list[str]:
    return [str(req.config.system_instruction) for req in fake.requests]


def expected_texts(trace: str) -> list[str]:
    _, headlines, feedbacks = TRACES[trace]
    out: list[str] = []
    for h, f in zip(headlines, feedbacks, strict=True):
        out += [h, json.dumps(f)]
    return out


@pytest.mark.parametrize("trace", sorted(TRACES))
async def test_native_run_reproduces_the_recorded_trace(trace: str) -> None:
    user, make = factory(trace)
    _, _, feedbacks = TRACES[trace]
    r = await run(make(), [user])
    assert r.authors == ["evaluate_headline", "generate_headline", "root_agent"]
    assert r.texts == expected_texts(trace)
    assert routes(r) == [f["grade"] for f in feedbacks]
    assert r.final_output == feedbacks[-1]
    assert r.state == {"topic": user, "feedback": feedbacks[-1]}
    rounds = len(feedbacks)
    assert paths(r) == ["root_agent@1/process_input@1"] + [
        f"root_agent@1/{n}@{i}"
        for i in range(1, rounds + 1)
        for n in ("generate_headline", "evaluate_headline", "route_headline")
    ]
    # The second headline prompt carries the evaluator's feedback from state.
    gen = make.fakes[0]["generate_headline"]
    assert all(f'"{user}"' in i for i in instructions(gen))
    if rounds > 1:
        assert FLOWER_F1["feedback"] in instructions(gen)[1]


LEGACY: dict[str, Any] = {"state": "legacy_read"}
"""``generate_headline``'s instruction reads ``{topic}`` and ``{feedback?}`` from
session state; the compiled runs accept that legacy read explicitly."""


def test_default_compile_rejects_the_state_reading_instruction() -> None:
    """``generate_headline``'s instruction injects ``{topic}`` and ``{feedback?}``
    from session state: the same legacy read the compiler rejects for a
    FunctionNode parameter (commitment 2)."""
    _, make = factory("flower")
    with pytest.raises(WorkflowTranslationError, match="generate_headline") as err:
        compile_workflow(make())
    (finding,) = err.value.report.rejected
    assert finding.subject == "generate_headline"
    assert finding.message.startswith(
        "reads session state (instruction {topic}, instruction {feedback})"
    )
    assert "state='legacy_read'" in finding.message


@pytest.mark.parametrize("trace", sorted(TRACES))
async def test_compiled_run_matches_native(trace: str, orchestrator) -> None:  # type: ignore[no-untyped-def]
    user, make = factory(trace)
    native, petri, _ = await run_both(make, [user], orchestrator, **LEGACY)
    assert petri.final_output == native.final_output == TRACES[trace][2][-1]
    assert petri.authors == native.authors
    assert petri.texts == native.texts == expected_texts(trace)
    assert routes(petri) == routes(native)
    assert paths(petri) == paths(native)
    assert deltas(petri) == deltas(native)
    assert petri.state == native.state
    n_fakes, p_fakes = make.fakes
    for name in ("generate_headline", "evaluate_headline"):
        assert instructions(p_fakes[name]) == instructions(n_fakes[name])


@pytest.mark.parametrize("trace", sorted(TRACES))
async def test_compiled_run_with_back_edge_budget_matches_native(trace: str, orchestrator) -> None:  # type: ignore[no-untyped-def]
    user, make = factory(trace)
    native, petri, cw = await run_both(
        make, [user], orchestrator, back_edge_budget={BACK_EDGE: 3}, **LEGACY
    )
    assert cw.budgets == {BACK_EDGE: 3}
    assert petri.final_output == native.final_output
    assert petri.texts == native.texts
    assert routes(petri) == routes(native)
    assert paths(petri) == paths(native)
    assert petri.state == native.state


async def test_exhausted_back_edge_budget_raises_from_the_runner(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """An evaluator that never says ``tech-related`` loops forever under ADK;
    the budgeted compile fails the run after K back edges. The net's failure
    is raised from the workflow node, so ``Runner.run_async`` raises it and
    ADK's node runner records it as the run's error event."""
    never = {"grade": "unrelated", "feedback": "more tech"}
    make = Factory(["h1", "h2", "h3"], [never, never, never])
    node = PetriWorkflow.from_workflow(
        make(), orchestrator=orchestrator, back_edge_budget={BACK_EDGE: 2}, **LEGACY
    )
    runner = InMemoryRunner(node=node, app_name="compiled")
    session = await runner.session_service.create_session(app_name="compiled", user_id="u")
    events: list[Any] = []
    with pytest.raises(LoopBudgetExhausted, match="exhausted its budget of 2"):
        async for e in runner.run_async(user_id="u", session_id=session.id, new_message=msg("x")):
            events.append(e)
    texts = [
        "".join(p.text or "" for p in e.content.parts)
        for e in events
        if e.content and e.content.parts and not e.partial
    ]
    assert [t for t in texts if t][0::2] == ["h1", "h2", "h3"]
    assert len(make.fakes[0]["generate_headline"].requests) == 3
    stored = await runner.session_service.get_session(
        app_name="compiled", user_id="u", session_id=session.id
    )
    assert stored is not None
    errors = [(e.author, e.error_code) for e in stored.events if e.error_code]
    assert errors == [("root_agent", "LoopBudgetExhausted")]


def test_report_lists_the_unbudgeted_cycle_as_approximated() -> None:
    _, make = factory("flower")
    cw = compile_workflow(make(), **LEGACY)
    assert not cw.report.rejected
    approx = {(f.subject, f.message) for f in cw.report.of("approximated")}
    cycle = "generate_headline->evaluate_headline->route_headline->generate_headline"
    assert any(s == cycle and "unbudgeted cycle" in m for s, m in approx)
    assert (
        "generate_headline",
        "reads legacy session state (instruction {topic}, instruction {feedback})",
    ) in approx
    assert {f.subject for f in cw.report.of("exact")} >= {"process_input", "route_headline"}
    assert any(
        f.subject == "route_headline" and "wf/route_headline/unmatched" in f.message
        for f in cw.report.of("exact")
    )
    assert {f.subject for f in cw.report.of("opaque")} == {
        "generate_headline",
        "evaluate_headline",
    }
    assert [p.name for p in cw.unmatched_places()] == ["wf/route_headline/unmatched"]


def test_report_with_back_edge_budget_lists_the_cycle_as_bounded() -> None:
    _, make = factory("flower")
    cw = compile_workflow(make(), back_edge_budget={BACK_EDGE: 3}, **LEGACY)
    exact = {(f.subject, f.message) for f in cw.report.of("exact")}
    assert (
        "route_headline->generate_headline",
        "back edge bounded by a budget of 3 per workflow run",
    ) in exact
    assert any("cycle bounded by a back-edge budget" in m for _, m in exact)
    assert not any("unbudgeted" in f.message for f in cw.report.findings)


UNMATCHED = "route coverage: unreachable(['wf/route_headline/unmatched'])"


@requires_z3
@pytest.mark.parametrize(
    "opts",
    [{}, {"back_edge_budget": {BACK_EDGE: 3}}],
    ids=["unbudgeted", "budgeted"],
)
def test_proofs(opts: dict[str, Any]) -> None:
    """Every safety claim and deadlock freedom are proven. The route-coverage
    lint is violated by design: ``tech-related`` has no edge and is how the
    loop exits. The workflow has no terminal node (``route_headline`` has an
    out-edge), so there is no terminal-output claim."""
    _, make = factory("flower")
    proofs = {p.label: p for p in verify_workflow(compile_workflow(make(), **opts, **LEGACY), k=1)}
    coverage = proofs.pop(UNMATCHED)
    assert coverage.kind == "route coverage"
    assert coverage.result.is_violated()
    assert {label: (p.kind, p.proven) for label, p in proofs.items()} == {
        "one turn at a time: place_bound(turnActive, 1)": ("safety", True),
        "permit never doubles: place_bound(turnPermit, 1)": ("safety", True),
        "process_input runs serially: place_bound(process_input/idle, 1)": ("safety", True),
        "generate_headline runs serially: place_bound(generate_headline/idle, 1)": (
            "safety",
            True,
        ),
        "evaluate_headline runs serially: place_bound(evaluate_headline/idle, 1)": (
            "safety",
            True,
        ),
        "route_headline runs serially: place_bound(route_headline/idle, 1)": ("safety", True),
        "deadlock_free": ("deadlock", True),
    }
