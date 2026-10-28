"""A node slower than its transition's timeout."""

from __future__ import annotations

import asyncio
from typing import Any


async def slow(node_input: Any = None) -> str:
    await asyncio.sleep(0.4)
    return "slow"
