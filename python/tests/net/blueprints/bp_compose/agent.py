"""Nodes for the composed blueprints."""

from __future__ import annotations

import asyncio
from typing import Any

from google.genai import types


def text_of(node_input: types.Content) -> str:
    return "".join(p.text or "" for p in node_input.parts or [])


async def quick(node_input: str) -> str:
    await asyncio.sleep(0.01)
    return f"quick({node_input})"


async def careful(node_input: str) -> str:
    await asyncio.sleep(0.15)
    return f"careful({node_input})"


def join(node_input: dict[str, Any]) -> str:
    return f"{node_input['r1']} + {node_input['r2']}"
