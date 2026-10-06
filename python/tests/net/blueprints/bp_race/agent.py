"""The race's two branches: stand-ins for a fast and a slow model call."""

from __future__ import annotations

import asyncio
from typing import Any

FAST, SLOW = 0.02, 0.25
FINISHED: list[str] = []


async def fast(node_input: Any = None) -> str:
    await asyncio.sleep(FAST)
    FINISHED.append("fast")
    return "answer from fast"


async def slow(node_input: Any = None) -> str:
    await asyncio.sleep(SLOW)
    FINISHED.append("slow")
    return "answer from slow"
