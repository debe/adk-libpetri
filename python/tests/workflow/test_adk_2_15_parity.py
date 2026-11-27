"""google-adk 2.15 scheduling changes, run natively and compiled.

- A list of routes that matches two edges between the same two nodes triggers
  the target once (``WorkflowGraph.get_next_pending_nodes`` deduplicates, as a
  single edge with all those routes would). Before 2.15 ADK ran the target
  once per matched edge; the compiled net always ran it once.
"""

from __future__ import annotations

from typing import Any

from google.adk import Event
from google.adk.workflow import Workflow

from .adk_samples._harness import Run, run_both


def paths(r: Run) -> list[str]:
    return [e.node_info.path for e in r.events if e.node_info and e.node_info.path]


def two_routed_edges_to_one_target() -> Workflow:
    """``pick`` emits both routes; each labels its own ``pick -> b`` edge."""

    def pick(node_input: Any) -> Any:
        yield Event(output="x", route=["left", "right"])

    def b(node_input: Any) -> str:
        return f"b {node_input}"

    return Workflow(
        name="root",
        edges=[
            ("START", pick),
            (pick, {"left": b}),
            (pick, {"right": b}),
        ],
    )


async def test_a_list_route_matching_two_edges_runs_the_target_once(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(two_routed_edges_to_one_target, ["go"], orchestrator)
    for r in (native, petri):
        assert [p for p in paths(r) if p.startswith("root@1/b@")] == ["root@1/b@1"]
        assert r.final_output == "b x"
    assert paths(petri) == paths(native)
