"""Nodes and types for bp_inline: inline nodes, and a child YAML one directory down."""

from __future__ import annotations

from dataclasses import dataclass

from google.genai import types

from support.fake_llm import ScriptedLlm, text


@dataclass(frozen=True)
class Note:
    text: str


LLM = ScriptedLlm.of(text("llm says hi"), text("llm says hi again"), text("third"))


def text_of(node_input: types.Content) -> str:
    return "".join(p.text or "" for p in node_input.parts or [])


def shout(node_input: types.Content) -> str:
    return text_of(node_input).upper()


def to_note(node_input: str) -> Note:
    return Note(f"note[{node_input}]")


def unwrap(node_input: Note) -> str:
    return node_input.text
