"""Checked action binding for subnet specs (Java ``SubnetActions``).

libpetri's own ``bind_actions`` on a ``SubnetDef`` binds a silent
``passthrough()`` for a transition the map misses and ignores a key that
names no transition. These helpers reject both, and :func:`merge` rejects a
transition bound by two maps, which a plain ``dict | dict`` would let the
later map win.
"""

from __future__ import annotations

from collections.abc import Mapping

import libpetri as lp

from .._spec import Action, NetSpec

ActionMap = Mapping[str, Action]


def bind(spec: NetSpec, *maps: ActionMap) -> dict[str, Action]:
    """Merge ``maps`` and check the union names exactly ``spec``'s transitions."""
    merged = merge(*maps)
    spec.check_bindings(merged)
    return merged


def merge(*maps: ActionMap) -> dict[str, Action]:
    """Merge binding maps in order, rejecting a transition bound by more than one."""
    merged: dict[str, Action] = {}
    for m in maps:
        for k, v in m.items():
            if k in merged:
                raise ValueError(f"Transition '{k}' is bound by more than one binding map.")
            merged[k] = v
    return merged


def bind_composed(spec: NetSpec, *maps: ActionMap) -> lp.BuiltNet:
    """Merge ``maps``, check them against ``spec`` and build the runnable net."""
    return spec.build(merge(*maps))
