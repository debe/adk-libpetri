"""Pattern A's three branches: stand-ins for a fast, a medium and a slow model call."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

FAST, MEDIUM, SLOW = 0.02, 0.12, 0.30
FINISHED: list[str] = []
"""Branch names in completion order; a test clears it before its turn."""


@dataclass(frozen=True)
class BranchResult:
    branch_id: str
    text: str


async def _branch(name: str, delay: float) -> BranchResult:
    await asyncio.sleep(delay)
    FINISHED.append(name)
    return BranchResult(name, f"answer from {name}")


async def fast(node_input: Any = None) -> Any:
    return await _branch("fast", FAST)


async def medium(node_input: Any = None) -> Any:
    return await _branch("medium", MEDIUM)


async def slow(node_input: Any = None) -> Any:
    return await _branch("slow", SLOW)
