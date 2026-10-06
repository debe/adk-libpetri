"""ADK sample ``workflows/agent_in_workflow``: a task-mode agent, a routed retry, tool confirmation.

Recorded traces: ``tests/*.json``, one parametrized case each. ``intake_agent``
(``mode="task"``) chats until it calls ``finish_task``; ``check_identity``
routes ``"retry"`` back to it for any name but Jane Doe; ``generate_instruction``
calls ``find_orders``, which needs an ``adk_request_confirmation`` round trip
(a long-running interrupt).

The compiler rejects the sample by design: a ``mode='task'`` agent waits for
the user across turns inside one workflow run (ADK keeps it WAITING and resumes
it with its history), which the net does not model. The native tests stay:
they validate the fakes against ADK's recorded traces. The compiled side pins
the rejection and the rest of its report.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from google.adk.workflow import Workflow

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.workflow import PetriWorkflow, WorkflowTranslationError, compile_workflow
from support.fake_llm import ScriptedLlm, call, text

from .._harness import Turn, function_response, llm_agents, run
from . import agent

JANE = {"name": "Jane Doe", "phone_number": "555-1234"}
JOHN = {"name": "John Doe", "phone_number": "555-1234"}
ORDERS = ["CBC (Complete Blood Count)", "Lipid Panel"]
HELLO_JANE = "Hello Jane Doe! Let me look up your orders."


def confirm(ok: bool) -> Callable[[list[Any]], Any]:
    """The user's answer to the last ``adk_request_confirmation``."""

    def answer(events: list[Any]) -> Any:
        for e in reversed(events):
            for p in e.content.parts if e.content and e.content.parts else []:
                fc = p.function_call
                if fc and fc.name == "adk_request_confirmation":
                    return function_response(fc.id, fc.name, {"confirmed": ok, "payload": {}})
        raise AssertionError("no adk_request_confirmation to answer")

    return answer


def finish(identity: dict[str, str]) -> Any:
    return call("finish_task", identity)


@dataclass(frozen=True)
class Trace:
    intake: tuple[Any, ...]
    instruction: str
    turns: tuple[Turn, ...]
    approved: bool = True
    retried: bool = False

    @property
    def generate(self) -> tuple[Any, ...]:
        return (call("find_orders"), text(self.instruction))


TRACES = {
    "go_approve": Trace(
        intake=(
            text("Hello! I'm your medical lab intake assistant. May I have your full name?"),
            text("Thank you, Jane Doe. May I please have your phone number now?"),
            finish(JANE),
        ),
        instruction="The patient has the following orders: CBC (Complete Blood Count) and "
        "Lipid Panel. Please fast for 8-12 hours prior to your appointment.",
        turns=("go", "Jane Doe", "555-1234", confirm(True)),
    ),
    "go_decline": Trace(
        intake=(
            text("Hello! Could I please get your full name and phone number?"),
            text("Thanks, Jane. Could I please get your phone number?"),
            finish(JANE),
        ),
        instruction="I'm sorry, but I was unable to retrieve the patient's orders. "
        "The tool call was rejected.",
        turns=("go", "Jane Doe", "555-1234", confirm(False)),
        approved=False,
    ),
    "jane_doe": Trace(
        intake=(text("Hi Jane, what is your phone number?"), finish(JANE)),
        instruction="Here are the orders found: CBC (Complete Blood Count) and Lipid Panel.",
        turns=("Hi, my name is Jane Doe", "555-1234", confirm(True)),
    ),
    "jane_doe_and_phone_number": Trace(
        intake=(finish(JANE),),
        instruction="The patient has orders for a CBC (Complete Blood Count) and a Lipid Panel.",
        turns=("I am Jane Doe, my phone number is 555-1234", confirm(True)),
    ),
    "phone_number": Trace(
        intake=(text("Thanks! What is your full name?"), finish(JANE)),
        instruction="The patient has the following orders: CBC (Complete Blood Count) and "
        "Lipid Panel.",
        turns=("My phone number is 555-1234", "Jane Doe", confirm(True)),
    ),
    "wrong_name": Trace(
        intake=(
            text("Thanks, John. What's your phone number?"),
            finish(JOHN),
            text(
                "Could not find matching records for John Doe. Let's try again. "
                "What other name can I use?"
            ),
            finish(JANE),
        ),
        instruction="Here are your orders:\n* CBC (Complete Blood Count)\n* Lipid Panel",
        turns=("My name is John Doe", "555-1234", "Jane Doe, 555-1234", confirm(True)),
        retried=True,
    ),
}


def maker(trace: Trace) -> Callable[[], Workflow]:
    def make() -> Workflow:
        wf = importlib.reload(agent).root_agent
        for a in llm_agents(wf):
            script = trace.intake if a.name == "intake_agent" else trace.generate
            a.model = ScriptedLlm.of(*script)
        return wf

    return make


def _parts(e: Any) -> list[Any]:
    return list(e.content.parts) if e.content and e.content.parts else []


def _requests(wf: Workflow) -> dict[str, list[list[tuple[str | None, str]]]]:
    """Per agent, each LLM request's contents as (role, text-or-call/response names)."""

    def part(p: Any) -> str:
        if p.text:
            return p.text
        if p.function_call:
            return f"call:{p.function_call.name}"
        if p.function_response:
            return f"response:{p.function_response.name}:{p.function_response.response}"
        return "?"

    return {
        a.name: [
            [(c.role, part(p)) for c in req.contents for p in c.parts or []]
            for req in a.model.requests  # type: ignore[union-attr]
        ]
        for a in llm_agents(wf)
    }


# ----------------------------------------------------------------------------
#  a. the fakes reproduce ADK's recorded traces
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TRACES))
async def test_native_run_reproduces_the_recorded_trace(name: str) -> None:
    trace = TRACES[name]
    r = await run(maker(trace)(), trace.turns)
    assert len(r.turns) == len(trace.turns)
    outputs = [e.output for e in r.events if e.output is not None]
    assert outputs == ([JOHN, JANE] if trace.retried else [JANE])
    expected_check = [HELLO_JANE]
    if trace.retried:
        expected_check.insert(0, "Could not find matching records for John Doe. Let's try again.")
    assert [t for e in r.events if e.author == "task_in_workflow" for t in r_texts(e)] == (
        expected_check
    )
    assert r.texts[-1] == trace.instruction
    # Every chat turn but the last two ends on intake_agent's question.
    for turn in r.turns[: len(trace.turns) - 2]:
        assert turn[-1].author == "intake_agent" and r_texts(turn[-1])
    # The turn before the last ends waiting on the confirmation.
    confirm_turn = r.turns[-2]
    assert any(e.long_running_tool_ids for e in confirm_turn)
    tool_result = [
        p.function_response.response
        for e in r.turns[-1]
        for p in _parts(e)
        if p.function_response and p.function_response.name == "find_orders"
    ]
    assert tool_result == (
        [{"result": ORDERS}] if trace.approved else [{"error": "This tool call is rejected."}]
    )
    assert r.authors == ["generate_instruction", "intake_agent", "task_in_workflow"]
    retry_routes = [e.actions.route for e in r.events if e.actions and e.actions.route]
    assert retry_routes == (["retry"] if trace.retried else [])


def r_texts(e: Any) -> list[str]:
    return [p.text for p in _parts(e) if p.text]


async def test_native_task_agent_sees_its_own_history() -> None:
    """Natively the second intake request carries the whole exchange: ADK resumes
    the WAITING task agent in the same run (what the net does not model)."""
    wf = maker(TRACES["jane_doe"])()
    await run(wf, TRACES["jane_doe"].turns)
    # (ADK's own history drops the very first user message; the model's question is kept.)
    assert _requests(wf)["intake_agent"][1] == [
        ("user", "555-1234"),
        ("model", "Hi Jane, what is your phone number?"),
        ("user", "555-1234"),
    ]


# ----------------------------------------------------------------------------
#  b. compiled: rejected by design (mode='task')
# ----------------------------------------------------------------------------

BUDGET = {("check_identity", "intake_agent"): 3}
OPTIONS = {
    "default": {},
    "interruptible": {"interruptible": ["generate_instruction"]},
    "budget3": {"back_edge_budget": BUDGET},
    "legacy_state": {"state": "legacy_read", "multi_route": "first"},
}


@pytest.mark.parametrize("opts", list(OPTIONS.values()), ids=list(OPTIONS))
def test_task_agent_is_rejected(opts: dict[str, Any]) -> None:
    """No compile option makes a task-mode agent compile."""
    with pytest.raises(WorkflowTranslationError, match="mode='task'") as err:
        compile_workflow(maker(TRACES["jane_doe"])(), **opts)
    assert [f.subject for f in err.value.report.rejected] == ["intake_agent"]


def test_task_agent_is_rejected_by_petri_workflow(orchestrator: OrchestratorLoop) -> None:
    with pytest.raises(WorkflowTranslationError, match="mode='task'"):
        PetriWorkflow.from_workflow(maker(TRACES["jane_doe"])(), orchestrator=orchestrator)


def _report(**opts: Any) -> dict[tuple[str, str], str]:
    with pytest.raises(WorkflowTranslationError) as err:
        compile_workflow(maker(TRACES["jane_doe"])(), **opts)
    return {(f.severity, f.subject): f.message for f in err.value.report.findings}


def test_report_of_the_rejected_sample() -> None:
    report = _report()
    assert report[("rejected", "intake_agent")].startswith(
        "mode='task' agent: it waits for the user across turns"
    )
    # The confirming tool makes generate_instruction interruptible without naming it.
    assert report[("exact", "generate_instruction")] == (
        "tools ['find_orders'] require confirmation: compiled interruptible"
    )
    assert report[("exact", "check_identity")] == "FunctionNode, run by ADK's node runner"
    assert report[("approximated", "intake_agent")].startswith("wait_for_output")
    cycle = report[("approximated", "intake_agent->check_identity->intake_agent")]
    assert cycle.startswith("unbudgeted cycle")


def test_report_with_a_back_edge_budget() -> None:
    report = _report(back_edge_budget=BUDGET)
    assert report[("exact", "intake_agent->check_identity->intake_agent")] == (
        "cycle bounded by a back-edge budget"
    )
