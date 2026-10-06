"""Shared driver: run one ADK workflow natively and compiled, compare the runs."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.llm_agent import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow
from google.genai import types

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.workflow import CompiledWorkflow, PetriWorkflow, compile_workflow

Turn = str | types.Content | Callable[[list[Any]], types.Content]
"""A user turn: text, a ready message, or a function of the events so far
(for answering a ``RequestInput`` interrupt with its id)."""


def msg(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def function_response(call_id: str, name: str, response: dict[str, Any]) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(id=call_id, name=name, response=response)
            )
        ],
    )


@dataclass
class Run:
    events: list[Any] = field(default_factory=list)
    turns: list[list[Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def final_output(self) -> Any:
        outs = [e.output for e in self.events if e.output is not None]
        return outs[-1] if outs else None

    @property
    def authors(self) -> list[str]:
        return sorted({e.author for e in self.events if e.author and e.author != "user"})

    @property
    def texts(self) -> list[str]:
        out = []
        for e in self.events:
            if e.content and e.content.parts and not e.partial:
                t = "".join(p.text or "" for p in e.content.parts)
                if t:
                    out.append(t)
        return out


async def run(node: Any, turns: Sequence[Turn], app_name: str = "app") -> Run:
    """Drive ``turns`` through one session of ``InMemoryRunner(node=node)``."""
    runner = InMemoryRunner(node=node, app_name=app_name)
    session = await runner.session_service.create_session(app_name=app_name, user_id="u")
    result = Run()
    for turn in turns:
        if callable(turn):
            message = turn(result.events)
        elif isinstance(turn, str):
            message = msg(turn)
        else:
            message = turn
        events = [
            e
            async for e in runner.run_async(user_id="u", session_id=session.id, new_message=message)
        ]
        result.turns.append(events)
        result.events.extend(events)
    final = await runner.session_service.get_session(
        app_name=app_name, user_id="u", session_id=session.id
    )
    assert final is not None
    result.state = dict(final.state)
    return result


async def run_both(
    make: Callable[[], Workflow],
    turns: Sequence[Turn],
    orchestrator: OrchestratorLoop,
    **compile_opts: Any,
) -> tuple[Run, Run, CompiledWorkflow]:
    """Run a fresh ``make()`` natively, then a fresh one compiled.

    ``make`` must build new nodes and new fake models each call, so the two
    runs share no scripted state.
    """
    native = await run(make(), turns, "native")
    compiled = compile_workflow(make(), **compile_opts)
    node = PetriWorkflow.from_compiled(compiled, orchestrator=orchestrator)
    petri = await run(node, turns, "compiled")
    return native, petri, compiled


def llm_agents(workflow: Workflow) -> Iterable[LlmAgent]:
    """Every ``LlmAgent`` node of ``workflow``, nested workflows included."""
    graph = workflow.graph
    assert graph is not None
    for n in graph.nodes:
        if isinstance(n, LlmAgent):
            yield n
        elif isinstance(n, Workflow):
            yield from llm_agents(n)
