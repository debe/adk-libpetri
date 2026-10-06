"""Small helpers shared by the stock subnets."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from typing import Any

from .._aio import on_loop

IdSupplier = Callable[[], str]


def random_id() -> str:
    return str(uuid.uuid4())


def exception_type(err: BaseException) -> str:
    """Qualified exception class name (Java's ``getClass().getName()``)."""
    t = type(err)
    return t.__qualname__ if t.__module__ == "builtins" else f"{t.__module__}.{t.__qualname__}"


async def call_user(fn: Callable[..., Any], *args: Any) -> Any:
    """Call a user callback from inside an action; a coroutine result runs on the loop."""
    result = fn(*args)
    if inspect.iscoroutine(result):
        return await on_loop(result)
    return result
