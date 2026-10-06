"""ADK nodes the bp_turns blueprints name: the turn protocol across turns."""

from __future__ import annotations

import asyncio
from typing import Any

from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.genai import types


def text_of(node_input: types.Content) -> str:
    return "".join(p.text or "" for p in node_input.parts or [])


def later_fn(node_input: Any = None) -> str:
    return "later"


async def slow(node_input: Any = None) -> str:
    await asyncio.sleep(0.2)
    return "slow"


def boom(node_input: Any = None) -> str:
    raise RuntimeError("boom")


def shout(node_input: types.Content) -> str:
    return text_of(node_input).upper()


def to_content(node_input: types.Content) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text_of(node_input))])


async def slow_answer(node_input: types.Content) -> str:
    await asyncio.sleep(0.6)
    return "slow " + text_of(node_input)


def fast_answer(node_input: types.Content) -> str:
    return "fast " + text_of(node_input)


async def tag(node_input: Any) -> str:
    text = node_input if isinstance(node_input, str) else text_of(node_input)
    await asyncio.sleep(0.3 if text.startswith("slow") else 0.05)
    return f"tag[{text}]"


def read(node_input: types.Content) -> str:
    return text_of(node_input)


def pre_b(node_input: str) -> str:
    return f"b:{node_input}"


def join(node_input: dict[str, Any]) -> str:
    return f"join[{node_input['ra']} | {node_input['rb']}]"


def upper(node_input: types.Content) -> str:
    return text_of(node_input).upper()


def lower(node_input: types.Content) -> str:
    return "lower:" + text_of(node_input).lower()


def after(node_input: Any) -> str:
    return f"after[{node_input!r}]"


def draft(node_input: types.Content) -> str:
    return f"draft of {text_of(node_input)}"


def decide(node_input: dict[str, Any]) -> Event:
    route = "approve" if node_input["approval"] else "reject"
    return Event(output=node_input["drafted"], actions=EventActions(route=route))


def escalate_msg(node_input: Any = None) -> str:
    return "escalated"
