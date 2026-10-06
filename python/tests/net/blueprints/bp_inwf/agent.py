"""Nodes for bp_inwf: a Workflow with PetriNet nodes in its edges."""

from __future__ import annotations

from typing import Any

from google.genai import types


def text_of(node_input: types.Content) -> str:
    return "".join(p.text or "" for p in node_input.parts or [])


def shout(node_input: types.Content) -> str:
    return text_of(node_input).upper()


def after(node_input: Any) -> str:
    return f"after[{node_input!r}]"


def after2(node_input: Any) -> str:
    return f"after2[{node_input!r}]"
