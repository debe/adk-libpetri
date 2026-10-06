"""``@experimental``: marks a surface that may change in any release (SSE, BIDI, from_workflow).

A marker only, as in Java: no runtime warning.
"""

from __future__ import annotations

import contextlib
from typing import TypeVar

T = TypeVar("T")


def experimental(obj: T) -> T:
    with contextlib.suppress(AttributeError, TypeError):
        obj.__experimental__ = True  # type: ignore[attr-defined]
    return obj


def is_experimental(obj: object) -> bool:
    return bool(getattr(obj, "__experimental__", False))
