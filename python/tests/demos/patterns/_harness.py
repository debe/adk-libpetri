"""Shared fixtures for the ADK-only foils: run a root node, time it, log branch lifecycles."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.adk.workflow import BaseNode, FunctionNode
from google.genai import types

APP = "foil"
USER = "u"


@dataclass
class Lifecycle:
    """What each branch did: started, finished, or was cancelled mid-sleep."""

    started: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)


@dataclass
class Run:
    elapsed: float
    events: list[Event]

    def outputs_of(self, node_name: str) -> list[Any]:
        """Outputs of every run of a static node, in emission order."""
        return [
            e.output
            for e in self.events
            if e.output is not None
            and e.node_info is not None
            and e.node_info.path is not None
            and e.node_info.path.rsplit("/", 1)[-1].split("@", 1)[0] == node_name
        ]


def delayed(
    name: str, delay_s: float, life: Lifecycle, answer: str | None = None
) -> Callable[[Any], Coroutine[Any, Any, str]]:
    """A branch body: sleeps `delay_s` (the stand-in for an LLM call) and returns an answer."""

    async def body(node_input: Any = None) -> str:
        life.started.append(name)
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            life.cancelled.append(name)
            raise
        life.finished.append(name)
        return answer if answer is not None else f"answer from {name}"

    body.__name__ = name
    return body


def branch(name: str, delay_s: float, life: Lifecycle, answer: str | None = None) -> FunctionNode:
    return FunctionNode(func=delayed(name, delay_s, life, answer), name=name)


async def run_root(*, node: BaseNode | None = None, agent: Any = None, text: str = "go") -> Run:
    """Drive `node` (a Workflow) or `agent` (a BaseAgent) through a stock InMemoryRunner."""
    runner = InMemoryRunner(node=node, agent=agent, app_name=APP)
    session = await runner.session_service.create_session(app_name=APP, user_id=USER)
    message = types.Content(role="user", parts=[types.Part(text=text)])
    events: list[Event] = []
    start = time.monotonic()
    async for event in runner.run_async(user_id=USER, session_id=session.id, new_message=message):
        events.append(event)
    return Run(elapsed=time.monotonic() - start, events=events)
