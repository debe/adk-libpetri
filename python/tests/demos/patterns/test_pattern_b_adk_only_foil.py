"""Pattern B foil: a K-of-N quorum (synthesize once K of N branches answer) in stock ADK 2.11.

Port of ``PatternB_AdkOnlyFoilTest.java``. Each test green-locks a gap in ADK's own
orchestration; if a future ADK release closes one, its assertion flips red.

What ``google.adk.workflow.Workflow`` cannot express, and why (ADK Python 2.11 source):

* **The only join is all-of-N.** ``JoinNode`` (``workflow/_join_node.py``) adds no
  configuration to ``BaseNode``; it only overrides ``_requires_all_predecessors`` to
  ``True``. ``Workflow._buffer_barrier_trigger`` (``workflow/_workflow.py``) fires it
  only when ``all(...)`` predecessors are ``NodeStatus.COMPLETED``. No field sets a
  cardinality, so no K-of-N barrier exists.
* **A hand-rolled quorum still waits for all N.** A plain successor gets one trigger
  per completed predecessor (``_buffer_downstream_triggers``), so a counter in user
  code can synthesize at the K-th trigger. But ``Workflow._run_loop`` only returns once
  ``pending_tasks`` is empty, so the turn still lasts as long as the slowest branch,
  and the N-K late branches are never cancelled.
* **An early-completion escape drops late results.** Collapsing the fan-out into one
  node that takes the first K results and cancels the rest (the Python counterpart of
  Rx ``merge(...).take(K)``) ends the turn early, but the late results do not go
  anywhere. The Petri version routes them to a ``DISCARDED`` place a parent net can
  observe.

The Java foil's two claims hold for ``Workflow`` unchanged.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from google.adk.workflow import BaseNode, FunctionNode, JoinNode, Workflow

from ._harness import Lifecycle, branch, delayed, run_root

K = 3
DELAYS = {"b1": 0.02, "b2": 0.04, "b3": 0.06, "b4": 0.25, "b5": 0.50}
SLOWEST = max(DELAYS.values())
FIRST_LATE = sorted(DELAYS.values())[K]


def _branches(life: Lifecycle) -> list[FunctionNode]:
    return [branch(name, delay, life) for name, delay in DELAYS.items()]


async def test_foil_join_node_waits_for_all_n_not_k() -> None:
    life = Lifecycle()
    branches = _branches(life)
    join = JoinNode(name="join")
    synthesize = FunctionNode(func=lambda node_input: sorted(node_input), name="synthesize")
    workflow = Workflow(
        name="quorum_join",
        edges=[("START", tuple(branches)), *[(b, join) for b in branches], (join, synthesize)],
    )

    run = await run_root(node=workflow)

    # All N branches ran to completion and synthesis saw all N, not the first K.
    assert sorted(life.finished) == sorted(DELAYS)
    assert life.cancelled == []
    assert run.outputs_of("synthesize") == [sorted(DELAYS)]
    # Runtime bounded below by the slowest branch.
    assert run.elapsed >= SLOWEST


def test_foil_join_node_has_no_quorum_parameter() -> None:
    # JoinNode declares no field beyond BaseNode's, so there is nowhere to put K.
    assert set(JoinNode.model_fields) == set(BaseNode.model_fields)
    assert JoinNode(name="join")._requires_all_predecessors is True


async def test_foil_hand_rolled_counter_quorum_still_waits_for_all() -> None:
    # The in-graph workaround: a plain successor triggered once per branch, with a
    # counter in user code that synthesizes on the K-th arrival. The quorum logic lives
    # in a closure that ADK knows nothing about.
    life = Lifecycle()
    branches = _branches(life)
    arrivals: list[Any] = []
    synthesized_at: list[float] = []

    def quorum(node_input: Any) -> Any:
        arrivals.append(node_input)
        if len(arrivals) == K:
            synthesized_at.append(time.monotonic())
            return sorted(arrivals)
        return None  # a None output leaves node_outputs untouched

    quorum_node = FunctionNode(func=quorum, name="quorum")
    workflow = Workflow(
        name="quorum_counter",
        edges=[("START", tuple(branches)), *[(b, quorum_node) for b in branches]],
    )

    start = time.monotonic()
    run = await run_root(node=workflow)

    # The K-th arrival does synthesize early...
    assert len(synthesized_at) == 1
    assert synthesized_at[0] - start < FIRST_LATE
    # ...but the node is still triggered for every late branch,
    assert len(arrivals) == len(DELAYS)
    # and the turn does not end at quorum: nothing cancels b4 and b5.
    assert life.cancelled == []
    assert run.elapsed >= SLOWEST


async def test_foil_take_k_escape_drops_late_results() -> None:
    # The escape: one node runs all N branches as plain coroutines, keeps the first K and
    # cancels the rest. The turn ends at quorum, but the N-K late results are lost: they
    # have nowhere to go, so a slow-but-thorough correction is silently discarded.
    life = Lifecycle()
    bodies = [delayed(name, delay, life) for name, delay in DELAYS.items()]

    async def take_k(node_input: Any) -> list[str]:
        tasks = [asyncio.ensure_future(body(node_input)) for body in bodies]
        results: list[str] = []
        for next_done in asyncio.as_completed(tasks):
            results.append(await next_done)
            if len(results) == K:
                break
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return sorted(results)

    workflow = Workflow(
        name="quorum_escape", edges=[("START", FunctionNode(func=take_k, name="take_k"))]
    )

    run = await run_root(node=workflow)

    assert run.outputs_of("take_k") == [["answer from b1", "answer from b2", "answer from b3"]]
    assert run.elapsed < FIRST_LATE
    # The late branches were cancelled and none of their answers appear anywhere.
    assert sorted(life.cancelled) == ["b4", "b5"]
    assert not any("b4" in str(e.output) or "b5" in str(e.output) for e in run.events if e.output)
