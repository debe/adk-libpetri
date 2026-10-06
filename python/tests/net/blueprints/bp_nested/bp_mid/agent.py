"""Middle level of the nested blueprints (package ``bp_mid``)."""

from __future__ import annotations

from dataclasses import dataclass

from bp_leaf.agent import LeafNote


@dataclass(frozen=True)
class MidNote:
    text: str


def wrap_mid(node_input: LeafNote) -> MidNote:
    return MidNote(f"mid[{node_input.text}]")
