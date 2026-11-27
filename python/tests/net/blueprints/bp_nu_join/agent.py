"""A specialist that holds a slow answer until the test releases it, and the canned reply."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, replace
from typing import Any

from google.genai import types

RELEASE = threading.Event()
"""Set by a test to let a slow answer go, after the escalation fired."""
SEEN: list[str] = []
"""The names the specialist was asked under, in order."""


@dataclass(frozen=True)
class Work:
    text: str
    turn: str = ""


def open_work(node_input: Any = None) -> Work:
    content = node_input if isinstance(node_input, types.Content) else None
    text = "".join(p.text or "" for p in content.parts or []) if content else str(node_input)
    return Work(text)


async def specialist(node_input: Work) -> Work:
    SEEN.append(node_input.turn)
    if "slow" in node_input.text:
        await asyncio.to_thread(RELEASE.wait, 10)
    return replace(node_input, text=f"answer to {node_input.text}")


def canned_reply(node_input: Any = None) -> str:
    return "a specialist will follow up"


def both(node_input: dict[str, str]) -> str:
    """The two mounted instances' names, ``r1 r2``."""
    return f"{node_input['r1']} {node_input['r2']}"
