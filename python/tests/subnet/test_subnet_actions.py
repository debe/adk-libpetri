"""Port of Java ``SubnetActionsTest``.

Java's ``bindComposed`` returns a ``PetriNet`` whose transitions expose their
actions; a built libpetri-py net exposes only transition names, so the
binding test checks the names and then runs the net to see each action fire.
"""

from __future__ import annotations

import libpetri as lp
import pytest

from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.subnet import bind_composed, merge

A: Place[str] = Place("a", str)
B: Place[str] = Place("b", str)
C: Place[str] = Place("c", str)


def ONE(ctx: Ctx) -> None:
    ctx.output(B, ctx.input(A) + "+one")


def TWO(ctx: Ctx) -> None:
    ctx.output(C, ctx.input(B) + "+two")


def two_step_net() -> NetSpec:
    return NetSpec(
        "two-step",
        (
            TransitionSpec("first", (one(A),), out(B)),
            TransitionSpec("second", (one(B),), out(C)),
        ),
    )


def test_merge_keeps_every_binding_in_order() -> None:
    merged = merge({"first": ONE}, {"second": TWO})
    assert list(merged.items()) == [("first", ONE), ("second", TWO)]


def test_merge_rejects_a_transition_bound_twice() -> None:
    with pytest.raises(ValueError, match="'first'"):
        merge({"first": ONE}, {"first": TWO})


def test_bind_composed_binds_every_transition() -> None:
    bound = bind_composed(two_step_net(), {"first": ONE}, {"second": TWO})
    assert sorted(t.name for t in bound.transitions) == ["first", "second"]
    # Each transition runs its own bound action, not a passthrough.
    marking = lp.run_sync(bound, initial={A.name: ["x"]})
    assert marking.tokens(C.name) == ("x+one+two",)


def test_bind_composed_rejects_an_unbound_transition() -> None:
    """The failure mode this exists for: an unchecked bind would silently bind
    the missing transition to passthrough()."""
    with pytest.raises(ValueError, match=r"missing keys \['second'\]"):
        bind_composed(two_step_net(), {"first": ONE})


def test_bind_composed_rejects_a_key_that_matches_no_transition() -> None:
    with pytest.raises(ValueError, match=r"extra keys \['thrid'\]"):
        bind_composed(two_step_net(), {"first": ONE, "second": TWO}, {"thrid": ONE})
