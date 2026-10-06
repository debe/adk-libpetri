"""Pattern C's cheap and slow paths, and the validation that routes the cheap result."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions

THRESHOLD = 50


@dataclass
class Config:
    """What a test sets before its turn: each path's score and delay."""

    cheap_score: int = 100
    slow_score: int = 100
    cheap_delay: float = 0.03
    slow_delay: float = 0.25


CONFIG = Config()


@dataclass(frozen=True)
class BranchResult:
    branch_id: str
    text: str
    score: int


async def cheap(node_input: Any = None) -> Any:
    await asyncio.sleep(CONFIG.cheap_delay)  # the stand-in for an LLM call
    return BranchResult("cheap", "answer from cheap", CONFIG.cheap_score)


async def slow(node_input: Any = None) -> Any:
    await asyncio.sleep(CONFIG.slow_delay)
    return BranchResult("slow", "answer from slow", CONFIG.slow_score)


def validate(node_input: Any) -> Any:
    """The only place that decides pass versus fail: the route picks the xor branch."""
    route = "pass" if node_input.score >= THRESHOLD else "fail"
    return Event(output=node_input, actions=EventActions(route=route))
