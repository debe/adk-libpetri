"""ADK sample ``workflows/route``: an LLM classifier routes to one of three handlers.

Recorded trace: ``tests/who_are_you.json`` (route ``question``). The other two
routes are driven with the same input and a different classifier answer.

``route_on_category(category: InputCategory)`` binds ``category`` from
``ctx.state`` (ADK's default ``parameter_binding="state"``; ``classify_input``
writes it through ``output_key``), so the compiler rejects the sample by
default (as it does the three agents, whose instructions read ``{input}``)
and compiles it under ``state="legacy_read"``.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from google.adk.workflow import Workflow

from adk_libpetri.workflow import WorkflowTranslationError, compile_workflow, verify_workflow
from support.fake_llm import ScriptedLlm, text
from support.smt_proofs import requires_z3

from .._harness import Run, llm_agents, run, run_both
from . import agent as sample

USER = "who are you"
ANSWER = "I am a large language model, trained by Google."
COMMENT = "A fine statement indeed."
OPTS: dict[str, Any] = {"state": "legacy_read"}
ROOT = "root_agent@1"


def make(category: str, fakes: list[dict[str, ScriptedLlm]] | None = None) -> Workflow:
    """A fresh workflow (reloaded module) whose classifier answers ``category``."""
    wf = importlib.reload(sample).root_agent
    models = {
        "classify_input": ScriptedLlm.of(text(f'{{"category": "{category}"}}')),
        "answer_question": ScriptedLlm.of(text(ANSWER)),
        "comment_on_statement": ScriptedLlm.of(text(COMMENT)),
    }
    for a in llm_agents(wf):
        a.model = models[a.name]
    if fakes is not None:
        fakes.append(models)
    return wf


def _text(e: Any) -> str:
    if not e.content or not e.content.parts:
        return ""
    return "".join(p.text or "" for p in e.content.parts)


def signature(r: Run) -> list[tuple[Any, ...]]:
    """What each session event says: author, path, text, route, state delta, output."""
    return [
        (
            e.author,
            e.node_info.path,
            _text(e),
            e.actions.route,
            dict(e.actions.state_delta),
            e.output,
            tuple(e.node_info.output_for or ()),
        )
        for e in r.events
    ]


def workflow_output(r: Run) -> Any:
    """The workflow's own output: the event ADK marks as output for ``root_agent@1``.

    In both runs that is the terminal node's own event (``use_as_output``):
    an LlmAgent's message (``message_as_output``) or a FunctionNode's output.
    """
    for e in reversed(r.events):
        if ROOT in (e.node_info.output_for or []):
            if e.output is not None:
                return e.output
            if e.node_info.message_as_output:
                return _text(e)
    return None


# -- a. the fakes reproduce ADK's recorded trace ------------------------------


async def test_native_run_reproduces_recorded_trace() -> None:
    native = await run(make("question"), [USER])
    assert [(e.author, e.node_info.path) for e in native.events] == [
        ("root_agent", "root_agent@1/process_input@1"),
        ("classify_input", "root_agent@1/classify_input@1"),
        ("root_agent", "root_agent@1/route_on_category@1"),
        ("answer_question", "root_agent@1/answer_question@1"),
    ]
    assert native.events[0].actions.state_delta == {"input": USER}
    assert native.events[1].output == {"category": "question"}
    assert native.events[2].actions.route == "question"
    assert native.events[3].node_info.output_for == [
        "root_agent@1/answer_question@1",
        ROOT,
    ]
    assert native.texts[-1] == ANSWER
    assert native.state == {"input": USER, "category": {"category": "question"}}
    assert workflow_output(native) == ANSWER


# -- b. compiled run vs native run ------------------------------------------------


@pytest.mark.parametrize(
    ("category", "author", "reply"),
    [
        ("question", "answer_question", ANSWER),
        ("statement", "comment_on_statement", COMMENT),
        ("other", "root_agent", "Sorry I can only answer questions or comment on statements."),
    ],
)
async def test_compiled_run_matches_native(orchestrator, category, author, reply) -> None:  # type: ignore[no-untyped-def]
    fakes: list[dict[str, ScriptedLlm]] = []
    native, petri, _ = await run_both(lambda: make(category, fakes), [USER], orchestrator, **OPTS)
    assert native.texts[-1] == petri.texts[-1] == reply
    assert native.events[-1].author == author
    assert native.texts == petri.texts
    assert native.authors == petri.authors
    assert native.state == petri.state == {"input": USER, "category": {"category": category}}
    assert workflow_output(petri) == workflow_output(native)
    # Event for event, ``output_for`` included: the terminal node's own event
    # is the workflow's output event, and the compiled node adds none.
    assert signature(petri) == signature(native)
    assert not any(e.node_info.path == ROOT for e in petri.events)
    # The classifier's instruction was rendered from session state in both runs.
    for models in fakes:
        (req,) = models["classify_input"].requests
        assert USER in str(req.config.system_instruction)


async def test_compiled_run_without_terminal_output_has_the_same_events(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(lambda: make("other"), [USER], orchestrator, **OPTS)
    assert signature(petri) == signature(native)
    assert native.final_output == petri.final_output == {"category": "other"}


async def test_terminal_message_is_the_workflow_output_without_an_extra_event(  # type: ignore[no-untyped-def]
    orchestrator,
) -> None:
    native, petri, _ = await run_both(lambda: make("question"), [USER], orchestrator, **OPTS)
    assert signature(petri) == signature(native)
    assert petri.events[-1].node_info.output_for == [
        "root_agent@1/answer_question@1",
        ROOT,
    ]
    assert workflow_output(petri) == workflow_output(native) == ANSWER


# -- c. report and proofs --------------------------------------------------------------


def test_default_compile_rejects_the_state_reads() -> None:
    with pytest.raises(WorkflowTranslationError) as info:
        compile_workflow(make("question"))
    message = str(info.value)
    assert "route_on_category: reads session state (parameter category)" in message
    for agent in ("classify_input", "answer_question", "comment_on_statement"):
        assert f"{agent}: reads session state (instruction {{input}})" in message
    assert {f.subject for f in info.value.report.rejected} == {
        "classify_input",
        "route_on_category",
        "answer_question",
        "comment_on_statement",
    }


def test_report_under_legacy_read() -> None:
    cw = compile_workflow(make("question"), **OPTS)
    assert not cw.report.rejected
    approximated = {(f.subject, f.message) for f in cw.report.of("approximated")}
    assert ("route_on_category", "reads legacy session state (parameter category)") in approximated
    for agent in ("classify_input", "answer_question", "comment_on_statement"):
        assert (agent, "reads legacy session state (instruction {input})") in approximated
    # One classifier, one router, one handler per branch: no fan-out.
    assert "fan-out order" not in {f.subject for f in cw.report.findings}
    opaque = {f.subject for f in cw.report.of("opaque")}
    assert opaque == {"classify_input", "answer_question", "comment_on_statement"}
    exact = {(f.subject, f.message) for f in cw.report.of("exact")}
    assert {"process_input", "route_on_category", "handle_other", "terminal output"} <= {
        s for s, _ in exact
    }
    assert any(s == "route_on_category" and "wf/route_on_category/unmatched" in m for s, m in exact)
    assert not any("cycle" in f.message for f in cw.report.findings)
    assert [p.name for p in cw.unmatched_places()] == ["wf/route_on_category/unmatched"]
    assert cw.terminal_nodes == ["answer_question", "comment_on_statement", "handle_other"]


TERMINALS = ("answer_question", "comment_on_statement", "handle_other")
NODES = ("process_input", "classify_input", "route_on_category", *TERMINALS)


@requires_z3
def test_proofs() -> None:
    cw = compile_workflow(make("question"), **OPTS)
    proofs = verify_workflow(cw, k=1)
    by_kind: dict[str, dict[str, str]] = {}
    for p in proofs:
        by_kind.setdefault(p.kind, {})[p.label] = p.result.verdict
    # The routes come from ``Literal["question", "statement", "other"]``, so the
    # unmatched branch is dead at run time, but the net does not see the type:
    # the lint cannot rule out the no-match sink.
    assert by_kind["route coverage"] == {
        "route coverage: unreachable(['wf/route_on_category/unmatched'])": "violated"
    }
    assert by_kind["deadlock"] == {"deadlock_free": "proven"}
    assert set(by_kind["safety"]) == {
        "one turn at a time: place_bound(turnActive, 1)",
        "permit never doubles: place_bound(turnPermit, 1)",
        *(f"{n} keeps one output: place_bound({n}/terminalOutput, 1)" for n in TERMINALS),
        "at most one terminal node outputs (ADK raises otherwise): unreachable(terminalConflict)",
        *(f"{n} runs serially: place_bound({n}/idle, 1)" for n in NODES),
    }
    assert all(v == "proven" for v in by_kind["safety"].values()), by_kind["safety"]
