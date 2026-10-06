"""ADK nodes the bp_basic blueprints name (``.agent.<fn>``)."""

from __future__ import annotations

from google.adk import Event
from google.genai import types

from adk_libpetri.net import NodeError


def text_of(node_input: types.Content) -> str:
    return "".join(p.text or "" for p in node_input.parts or [])


def shout(node_input: types.Content) -> str:
    return text_of(node_input).upper()


def triage(node_input: str) -> Event:
    return Event(output=node_input, route="urgent" if "!" in node_input else "later")


def handle(node_input: str) -> str:
    if "boom" in node_input:
        raise ValueError(f"cannot handle {node_input!r}")
    return f"handled {node_input}"


def apologise(node_input: NodeError) -> str:
    return f"sorry, {node_input.node} failed: {node_input.message}"


def explode(node_input: types.Content) -> str:
    raise RuntimeError("kaboom")


def count_words(node_input: types.Content) -> int:
    return len(text_of(node_input).split())
