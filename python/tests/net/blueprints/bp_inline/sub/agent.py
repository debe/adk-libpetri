"""A node for bp_inline/sub/child.yaml."""

from __future__ import annotations


def tag(node_input: str) -> str:
    return f"tag[{node_input}]"
