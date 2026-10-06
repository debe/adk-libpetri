"""Pattern B's five branches and the synthesis over the quorum's K results."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

DELAYS = {"b1": 0.03, "b2": 0.06, "b3": 0.09, "b4": 0.40, "b5": 0.80}


@dataclass(frozen=True)
class BranchResult:
    branch_id: str
    text: str


async def _branch(name: str) -> BranchResult:
    await asyncio.sleep(DELAYS[name])  # the stand-in for an LLM call
    return BranchResult(name, f"answer from {name}")


async def b1(node_input: Any = None) -> Any:
    return await _branch("b1")


async def b2(node_input: Any = None) -> Any:
    return await _branch("b2")


async def b3(node_input: Any = None) -> Any:
    return await _branch("b3")


async def b4(node_input: Any = None) -> Any:
    return await _branch("b4")


async def b5(node_input: Any = None) -> Any:
    return await _branch("b5")


def synthesize(node_input: Any) -> Any:
    """The exactly(3) arc hands over K results; the node only shapes them."""
    return "synth(" + ",".join(r.branch_id for r in node_input) + ")"
