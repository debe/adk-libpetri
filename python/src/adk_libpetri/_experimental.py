"""``@experimental``: marks a surface that may change in any release (SSE, BIDI, from_workflow).

A marker only, as in Java: no runtime warning.
"""

from __future__ import annotations

import contextlib
import weakref
from typing import TypeVar

T = TypeVar("T")

# Protocol classes are marked here, not by attribute: Python 3.11 reads a
# runtime-checkable protocol's members from its __dict__ at isinstance time,
# so a marker attribute would become a member nothing implements.
_PROTOCOLS: weakref.WeakSet[type] = weakref.WeakSet()


def experimental(obj: T) -> T:
    if isinstance(obj, type) and getattr(obj, "_is_protocol", False):
        _PROTOCOLS.add(obj)
        return obj
    with contextlib.suppress(AttributeError, TypeError):
        obj.__experimental__ = True  # type: ignore[attr-defined]
    return obj


def is_experimental(obj: object) -> bool:
    if isinstance(obj, type) and obj in _PROTOCOLS:
        return True
    return bool(getattr(obj, "__experimental__", False))
