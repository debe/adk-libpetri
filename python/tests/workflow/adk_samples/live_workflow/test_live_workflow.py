"""ADK sample ``live/live_workflow``: three task-mode agents in sequence.

``greeter_agent -> dob_verifier_agent -> goals_agent``, each ``mode='task'``:
an agent converses over as many user turns as it needs and completes by
calling ``finish_task``; its result is the next stage's input. While a stage
waits for the user, ADK's ``Workflow`` keeps it WAITING and resumes *that
node* (same run id, same isolation scope) on the next user message.

The sample is meant for a live (voice) session and ships no recorded trace,
only an LLM-simulated evalset. The graph does not depend on audio, so it is
driven here turn-based through ``Runner.run_async`` with a ``ScriptedLlm`` per
agent, following the README's sample inputs and the evalset's
conversation plan.

``compile_workflow`` rejects the sample (the across-turn WAITING resume of a
task-mode agent is not modelled), so only the native run is driven here; it
validates the fakes.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from google.adk.models.llm_response import LlmResponse
from google.adk.workflow import Workflow
from google.genai import types

from adk_libpetri.workflow import PetriWorkflow, WorkflowTranslationError, compile_workflow
from support.fake_llm import ScriptedLlm, call, text

from .._harness import Run, llm_agents, run
from . import agent

GREET = "Hi, this is Sam from the care team. Am I speaking with John Doe?"
ASK_DOB = "Thanks, John. What is your date of birth?"
GOALS = (
    "Your identity is verified. You have an appointment on Tuesday, June 16th at 3 PM "
    "with Dr. Example. Do you have any questions for the visit?"
)
BYE = "Glad I could help. Goodbye."

TURNS = [
    "Hello?",
    "Hi, yes, this is John Doe",
    "My date of birth is July 12th, 1985",
    "No, no other questions. Thanks!",
]


def _text_and_finish(t: str, result: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(text=t),
                types.Part(
                    function_call=types.FunctionCall(name="finish_task", args={"result": result})
                ),
            ],
        )
    )


def scripts() -> dict[str, list[Any]]:
    return {
        "greeter_agent": [text(GREET), call("finish_task", {"result": "John Doe"})],
        "dob_verifier_agent": [
            text(ASK_DOB),
            call("validate_date_of_birth", {"dob": "1985-07-12"}),
            call("finish_task", {"result": "verified"}),
        ],
        "goals_agent": [text(GOALS), _text_and_finish(BYE, "Goodbye.")],
    }


class Factory:
    def __init__(self) -> None:
        self.fakes: list[dict[str, ScriptedLlm]] = []

    def __call__(self) -> Workflow:
        wf = importlib.reload(agent).root_agent
        fakes = {name: ScriptedLlm.of(*s) for name, s in scripts().items()}
        for a in llm_agents(wf):
            a.model = fakes[a.name]
        self.fakes.append(fakes)
        return wf


def turn_authors(r: Run) -> list[list[str]]:
    return [sorted({e.author for e in t if e.author != "user"}) for t in r.turns]


def turn_texts(r: Run) -> list[list[str]]:
    out = []
    for t in r.turns:
        texts = []
        for e in t:
            if e.content and e.content.parts:
                s = "".join(p.text or "" for p in e.content.parts)
                if s:
                    texts.append(s)
        out.append(texts)
    return out


def history(fake: ScriptedLlm, i: int) -> list[str]:
    """The text turns of the ``i``-th request ``fake`` received."""
    return [
        p.text
        for c in fake.requests[i].contents
        for p in (c.parts or [])
        if p.text and not p.function_call
    ]


EXPECTED_TEXTS = [[GREET], [ASK_DOB], [GOALS], [BYE]]
EXPECTED_AUTHORS = [
    ["greeter_agent"],
    ["dob_verifier_agent", "greeter_agent"],
    ["dob_verifier_agent", "goals_agent"],
    ["goals_agent"],
]


async def test_native_run_walks_the_three_stages_turn_by_turn() -> None:
    make = Factory()
    r = await run(make(), TURNS)
    assert turn_texts(r) == EXPECTED_TEXTS
    assert turn_authors(r) == EXPECTED_AUTHORS
    outs = [(e.node_info.path, e.output) for e in r.events if e.output is not None]
    assert outs == [
        ("live_workflow@1/greeter_agent@1", {"result": "John Doe"}),
        ("live_workflow@1/dob_verifier_agent@1", {"result": "verified"}),
        ("live_workflow@1/goals_agent@1", {"result": "Goodbye."}),
    ]
    assert r.final_output == {"result": "Goodbye."}
    assert r.state == {"dob_verified": True}
    # Each stage is one run across turns, and sees its own conversation.
    greeter = make.fakes[0]["greeter_agent"]
    assert history(greeter, 1)[-2:] == [GREET, TURNS[1]]
    dob = make.fakes[0]["dob_verifier_agent"]
    assert history(dob, 1)[-2:] == [ASK_DOB, TURNS[2]]


STAGES = ("greeter_agent", "dob_verifier_agent", "goals_agent")


def test_compile_rejects_the_task_mode_agents() -> None:
    """A ``mode='task'`` agent WAITS for the user across turns inside one
    workflow run and is resumed (same run id, same isolation scope) by ADK.
    The net does not model that resume, so the compiler rejects every stage
    instead of restarting the workflow at START on each turn. There is no
    option that compiles them: the stages run on ADK (or as a ``PetriAgent``)."""
    with pytest.raises(WorkflowTranslationError, match="mode='task'") as err:
        compile_workflow(Factory()())
    rejected = err.value.report.rejected
    assert [f.subject for f in rejected] == list(STAGES)
    assert all(f.message.startswith("mode='task' agent:") for f in rejected)


def test_from_workflow_rejects_the_task_mode_agents(orchestrator) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(WorkflowTranslationError, match="mode='task'"):
        PetriWorkflow.from_workflow(Factory()(), orchestrator=orchestrator)
