"""google-adk 2.13 scheduling changes, run natively and compiled.

- A terminal run with no output clears the node's output (``_workflow.py``,
  ``node_outputs`` is popped); the net's reset arc on ``wf/N/terminalOutput``
  does the same.
- A ``JoinNode`` consumes its arrivals per firing (#7498): in a loop it waits
  for every predecessor's next run instead of firing on a stale one.
"""

from __future__ import annotations

from typing import Any

from google.adk import Event
from google.adk.workflow import JoinNode, Workflow

from adk_libpetri.workflow import PetriWorkflow

from .adk_samples._harness import Run, run, run_both


def rows(r: Run) -> list[tuple[Any, ...]]:
    return [
        (e.author, e.node_info.path if e.node_info else None, e.output, e.error_code)
        for e in r.events
    ]


def inner_cleared() -> Workflow:
    """``finish`` runs twice; the second run has no output."""

    def first(node_input: Any) -> str:
        return "kept"

    def second(node_input: Any) -> str:
        return "cleared"

    def finish(node_input: str) -> str | None:
        return "out" if node_input == "kept" else None

    return Workflow(name="inner", edges=[("START", (first, second), finish)], max_concurrency=1)


def show(node_input: Any) -> str:
    return f"inner gave {node_input!r}"


async def test_a_terminal_run_without_output_clears_it(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native = await run(Workflow(name="root", edges=[("START", inner_cleared(), show)]), ["go"])
    inner = PetriWorkflow.from_workflow(inner_cleared(), orchestrator=orchestrator)
    petri = await run(Workflow(name="root", edges=[("START", inner, show)]), ["go"])
    assert native.final_output == petri.final_output == "inner gave None"
    assert rows(petri) == rows(native)


def join_in_loop() -> Workflow:
    """``again`` loops back to ``a`` until its third run; ``b`` ends a longer
    chain, so ``again`` reaches the join twice before ``b`` does."""
    seen: list[int] = []

    def a(node_input: Any) -> int:
        seen.append(len(seen) + 1)
        return seen[-1]

    def again(node_input: int) -> Any:
        yield Event(output=node_input, route="again" if node_input < 3 else "done")

    def c(node_input: Any) -> str:
        return "c"

    def d(node_input: str) -> str:
        return "d"

    def e(node_input: str) -> str:
        return "e"

    def b(node_input: str) -> str:
        return "b"

    def joined(node_input: dict[str, Any]) -> dict[str, Any]:
        return node_input

    j = JoinNode(name="j")
    return Workflow(
        name="root",
        edges=[
            ("START", (a, c)),
            (a, again),
            (again, {"again": a}),
            (c, d, e, b),
            ((again, b), j),
            (j, joined),
        ],
        max_concurrency=1,
    )


def latest_again_at_join(r: Run) -> tuple[Any, Any]:
    """The join's input for ``again``, and ``again``'s latest output before the join ran."""
    latest = None
    for _, path, output, _ in rows(r):
        if path.startswith("root@1/again@"):
            latest = output
        if path.startswith("root@1/j@"):
            return output["again"], latest
    raise AssertionError("the join never ran")


async def test_a_join_takes_each_predecessors_latest_arrival(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(
        join_in_loop, ["go"], orchestrator, back_edge_budget={("again", "a"): 3}
    )
    # In both, j fires once, on again's latest arrival and b's; an arrival
    # after it fired is left over when the run ends, which ends the turn.
    # Which again run is the latest differs: sibling runs interleave in
    # completion order in the net (the report's "fan-out order").
    for r in (native, petri):
        assert [p for _, p, *_ in rows(r) if p.startswith("root@1/j@")] == ["root@1/j@1"]
        took, latest = latest_again_at_join(r)
        assert took == latest
    assert latest_again_at_join(native) == (2, 2)
    assert {p for _, p, *_ in rows(petri)} == {p for _, p, *_ in rows(native)}
    assert [e.error_code for e in petri.events if e.error_code] == []
