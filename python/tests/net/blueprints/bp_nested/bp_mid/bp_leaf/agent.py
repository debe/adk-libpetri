"""Leaf level of the nested blueprints (package ``bp_leaf``)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeafNote:
    text: str


def wrap_leaf(node_input: str) -> LeafNote:
    return LeafNote(f"leaf[{node_input}]")
