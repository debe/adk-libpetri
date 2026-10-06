"""ADK 2 workflows the compiler is checked against (compiled and run natively)."""

from __future__ import annotations

from typing import Any

from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import DEFAULT_ROUTE, FunctionNode, JoinNode, RetryConfig, Workflow


def node(fn: Any, name: str | None = None, **kw: Any) -> FunctionNode:
    return FunctionNode(func=fn, name=name or fn.__name__, **kw)


def classify(node_input: Any) -> Event:
    text = node_input.parts[0].text if hasattr(node_input, "parts") else str(node_input)
    return Event(output=text, route="bug" if "bug" in text else "other")


def handle_bug(node_input: str) -> str:
    return f"bug:{node_input}"


def handle_other(node_input: str) -> str:
    return f"other:{node_input}"


def upper(node_input: Any) -> str:
    text = node_input.parts[0].text if hasattr(node_input, "parts") else str(node_input)
    return text.upper()


def lower(node_input: Any) -> str:
    text = node_input.parts[0].text if hasattr(node_input, "parts") else str(node_input)
    return text.lower()


def combine(node_input: dict[str, Any]) -> str:
    return "+".join(f"{k}={node_input[k]}" for k in sorted(node_input))


def finish(node_input: Any) -> str:
    return f"done({node_input})"


async def ask(node_input: Any):
    yield RequestInput(message="approve?")


def linear() -> Workflow:
    return Workflow(name="linear", edges=[("START", node(upper), node(finish))])


def router() -> Workflow:
    c = node(classify)
    return Workflow(
        name="router",
        edges=[("START", c), (c, {"bug": node(handle_bug), DEFAULT_ROUTE: node(handle_other)})],
    )


def fan_join() -> Workflow:
    j = JoinNode(name="join")
    up, lo = node(upper), node(lower)
    return Workflow(
        name="fan_join",
        edges=[("START", (up, lo)), (up, j), (lo, j), (j, node(combine))],
    )


_calls = {"n": 0}


def flaky(node_input: Any) -> str:
    _calls["n"] += 1
    if _calls["n"] % 2 == 1:
        raise ValueError("transient")
    return "recovered"


def retrying() -> Workflow:
    return Workflow(
        name="retrying",
        edges=[
            (
                "START",
                node(
                    flaky, retry_config=RetryConfig(max_attempts=3, initial_delay=0.01, jitter=0.0)
                ),
            )
        ],
    )


def hitl() -> Workflow:
    return Workflow(name="hitl", edges=[("START", node(ask), node(finish))])


def counter(node_input: Any) -> Event:
    n = node_input if isinstance(node_input, int) else 0
    return Event(output=n + 1, route="again" if n + 1 < 3 else "stop")


def looping() -> Workflow:
    c = node(counter)
    return Workflow(
        name="looping", edges=[("START", c), (c, {"again": c, DEFAULT_ROUTE: node(finish)})]
    )


def concurrent() -> Workflow:
    up, lo = node(upper), node(lower)
    j = JoinNode(name="join")
    return Workflow(
        name="concurrent",
        max_concurrency=1,
        edges=[("START", (up, lo)), (up, j), (lo, j), (j, node(combine))],
    )
