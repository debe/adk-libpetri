"""Top level of the nested blueprints (package ``bp_nested``)."""

from __future__ import annotations

from dataclasses import dataclass

from bp_mid.agent import MidNote
from google.genai import types


@dataclass(frozen=True)
class TopNote:
    text: str


def text_of(node_input: types.Content) -> str:
    return "".join(p.text or "" for p in node_input.parts or [])


def wrap_top(node_input: MidNote) -> TopNote:
    return TopNote(f"top[{node_input.text}]")


def render(node_input: TopNote) -> str:
    return node_input.text
