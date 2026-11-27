"""google-adk 2.14 scheduling changes, run natively and compiled.

- A ``JoinNode`` on a cycle keeps the arrival of a predecessor it cannot reach
  (``Workflow._buffer_barrier_trigger``): a setup step before the loop runs
  once, and the join still fires on every iteration. START, which never runs,
  always counts as arrived. The net reads those arrivals instead of consuming
  them.
- A parallel worker's ``retry_config`` applies to each item (``_ParallelWorker``
  moves it onto the item node, and the node runner never retries a worker for
  an item's failure, ``_delegates_item_retry``). The compiler leaves a
  parallel worker's retries to ADK, inside its transition, with no retry loop
  in the net.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, ClassVar

import pytest
from google.adk import Event
from google.adk.workflow import JoinNode, Node, RetryConfig, Workflow, node

from adk_libpetri.workflow import PetriWorkflow, compile_workflow, verify_workflow
from support.smt_proofs import requires_z3

from .adk_samples._harness import Run, run, run_both


def paths(r: Run) -> list[str]:
    return [e.node_info.path for e in r.events if e.node_info and e.node_info.path]


def errors(r: Run) -> list[str]:
    return [e.error_code for e in r.events if e.error_code]


# -- a join on a cycle ---------------------------------------------------------


def join_with_setup() -> Workflow:
    """``setup`` runs once, before the loop; ``check`` loops back to ``a``
    until its third run; ``j`` joins ``setup`` and ``a``."""
    count = {"n": 0}

    def setup(node_input: Any) -> str:
        return "cfg"

    def a(node_input: Any) -> int:
        count["n"] += 1
        return count["n"]

    def check(node_input: dict[str, Any]) -> Any:
        n = node_input["a"]
        yield Event(output=(node_input["setup"], n), route="again" if n < 3 else "done")

    def end(node_input: Any) -> str:
        return f"end {node_input}"

    j = JoinNode(name="j")
    return Workflow(
        name="root",
        edges=[
            ("START", setup, j),
            ("START", a, j),
            (j, check),
            (check, {"again": a, "done": end}),
        ],
        max_concurrency=1,
    )


def join_with_start() -> Workflow:
    """START is a predecessor of ``j``, which is on the ``a -> j -> check -> a`` loop."""
    count = {"n": 0}

    def a(node_input: Any) -> int:
        count["n"] += 1
        return count["n"]

    def check(node_input: dict[str, Any]) -> Any:
        n = node_input["a"]
        yield Event(output=n, route="again" if n < 3 else "done")

    def end(node_input: Any) -> str:
        return f"end {node_input}"

    j = JoinNode(name="j")
    return Workflow(
        name="root",
        edges=[
            ("START", a, j),
            ("START", j),
            (j, check),
            (check, {"again": a, "done": end}),
        ],
        max_concurrency=1,
    )


def join_after_self_loop() -> Workflow:
    """``j`` is on no cycle; ``a`` loops on itself and reaches ``j`` on each run,
    and START, its other predecessor, always counts as arrived."""
    count = {"n": 0}

    def a(node_input: Any) -> Any:
        count["n"] += 1
        yield Event(output=count["n"], route="again" if count["n"] < 3 else "done")

    def show(node_input: dict[str, Any]) -> str:
        return f"show {node_input['a']}"

    j = JoinNode(name="j")
    return Workflow(
        name="root",
        edges=[("START", a), ("START", j), (a, {"again": a}), (a, j), (j, show)],
        max_concurrency=1,
    )


async def test_a_join_on_a_cycle_keeps_a_setup_steps_arrival(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(
        join_with_setup, ["go"], orchestrator, back_edge_budget={("check", "a"): 3}
    )
    joins = ["root@1/j@1", "root@1/j@2", "root@1/j@3"]
    for r in (native, petri):
        assert [p for p in paths(r) if p.startswith("root@1/j@")] == joins
        assert r.final_output == "end ('cfg', 3)"
    assert paths(petri) == paths(native)
    assert errors(petri) == errors(native) == []


async def test_start_counts_as_arrived_at_a_join_on_a_cycle(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(
        join_with_start, ["go"], orchestrator, back_edge_budget={("check", "a"): 3}
    )
    for r in (native, petri):
        assert [p for p in paths(r) if p.startswith("root@1/j@")] == [
            "root@1/j@1",
            "root@1/j@2",
            "root@1/j@3",
        ]
        assert r.final_output == "end 3"
    assert paths(petri) == paths(native)
    assert errors(petri) == errors(native) == []


async def test_start_counts_as_arrived_at_a_join_off_a_cycle(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(
        join_after_self_loop, ["go"], orchestrator, back_edge_budget={("a", "a"): 3}
    )
    for r in (native, petri):
        assert sorted(p for p in paths(r) if p.startswith("root@1/j@")) == [
            "root@1/j@1",
            "root@1/j@2",
            "root@1/j@3",
        ]
    # j fires once per run of a; which run's show ends last is scheduling
    # order (the report's "fan-out order"), so compare the runs as sets.
    assert sorted(paths(petri)) == sorted(paths(native))
    assert errors(petri) == errors(native) == []


def test_the_net_reads_a_kept_arrival_and_consumes_the_rest() -> None:
    cw = compile_workflow(join_with_setup(), back_edge_budget={("check", "a"): 3})
    run = cw.spec.transition("Wf_j_Run")
    assert "wf/j/from/a" in {i.place.name for i in run.inputs}
    assert {p.name for p in run.reads} == {"wf/j/from/setup"}
    assert (
        "j",
        "join over ['setup', 'a'] (consume semantics except ['setup'], whose arrival "
        "it keeps; latest arrival per predecessor); its input dict is in edge order, where "
        "ADK's follows set iteration",
    ) in {(f.subject, f.message) for f in cw.report.findings}
    # Off a cycle every predecessor but START is consumed.
    run = compile_workflow(
        join_after_self_loop(), back_edge_budget={("a", "a"): 3}
    ).spec.transition("Wf_j_Run")
    assert "wf/j/from/a" in {i.place.name for i in run.inputs}
    assert {p.name for p in run.reads} == {"wf/j/from/__START__"}


def test_a_join_whose_only_predecessor_is_start_fires_once() -> None:
    """A read arc alone would fire it forever: it consumes START's arrival."""

    def show(node_input: Any) -> Any:
        return node_input

    j = JoinNode(name="j")
    run = compile_workflow(Workflow(name="root", edges=[("START", j), (j, show)])).spec.transition(
        "Wf_j_Run"
    )
    assert "wf/j/from/__START__" in {i.place.name for i in run.inputs}
    assert not run.reads


@requires_z3
@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    ("make", "budget"),
    [
        (join_with_setup, {("check", "a"): 3}),
        (join_with_start, {("check", "a"): 3}),
        (join_after_self_loop, {("a", "a"): 3}),
    ],
)
def test_a_join_with_kept_arrivals_proves_safety_and_deadlock_freedom(make, budget) -> None:  # type: ignore[no-untyped-def]
    cw = compile_workflow(make(), back_edge_budget=budget)
    verdicts = {
        p.label: p.result.verdict
        for p in verify_workflow(cw, k=2)
        if p.kind in ("safety", "deadlock")
    }
    assert all(v == "proven" for v in verdicts.values()), verdicts


# -- per-item retry of a parallel worker ---------------------------------------

RETRY = RetryConfig(max_attempts=3, initial_delay=0.01, jitter=0.0)


def flaky_worker(fail_times: int, tries: dict[int, int] | None = None) -> Workflow:
    """``work`` runs per item, retried per item; item 2 fails ``fail_times`` times."""
    tries = {} if tries is None else tries

    def items(node_input: Any) -> list[int]:
        return [1, 2, 3]

    @node(parallel_worker=True, retry_config=RETRY)
    def work(node_input: int) -> int:
        tries[node_input] = tries.get(node_input, 0) + 1
        if node_input == 2 and tries[node_input] <= fail_times:
            raise ValueError("flaky item 2")
        return node_input * 10

    def done(node_input: Any) -> Any:
        return {"got": node_input, "tries": dict(sorted(tries.items()))}

    return Workflow(name="root", edges=[("START", items, work, done)])


class FlakyItems(Node):
    """A ``Node`` subclass run per item: its ``retry_config`` stays on the node,
    and ADK retries each item with it (the item node is a copy of the node)."""

    tries: ClassVar[dict[int, int]] = {}
    fail_times: ClassVar[int] = 1

    async def run_node_impl(self, *, ctx: Any, node_input: Any) -> AsyncGenerator[Any, None]:
        t = type(self).tries
        t[node_input] = t.get(node_input, 0) + 1
        if node_input == 2 and t[node_input] <= type(self).fail_times:
            raise ValueError("flaky item 2")
        yield node_input * 10


def flaky_node_subclass(fail_times: int, tries: dict[int, int] | None = None) -> Workflow:
    FlakyItems.tries = {} if tries is None else tries
    FlakyItems.fail_times = fail_times

    def items(node_input: Any) -> list[int]:
        return [1, 2, 3]

    def done(node_input: Any) -> Any:
        return {"got": node_input, "tries": dict(sorted(FlakyItems.tries.items()))}

    work = FlakyItems(name="work", parallel_worker=True, retry_config=RETRY)
    return Workflow(name="root", edges=[("START", items, work, done)])


@pytest.mark.parametrize("make", [flaky_worker, flaky_node_subclass])
async def test_a_failed_item_is_retried_in_place(orchestrator, make) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(lambda: make(1), ["go"], orchestrator)
    want = {"got": [10, 20, 30], "tries": {1: 1, 2: 2, 3: 1}}
    assert native.final_output == petri.final_output == want
    assert sorted(paths(petri)) == sorted(paths(native))
    assert errors(petri) == errors(native) == ["ValueError"]


@pytest.mark.parametrize("make", [flaky_worker, flaky_node_subclass])
async def test_an_item_out_of_retries_fails_the_node_without_a_whole_node_retry(  # type: ignore[no-untyped-def]
    orchestrator, make
) -> None:
    native: dict[int, int] = {}
    with pytest.raises(Exception, match="flaky item 2"):
        await run(make(99, native), ["go"])
    petri: dict[int, int] = {}
    node = PetriWorkflow.from_workflow(make(99, petri), orchestrator=orchestrator)
    with pytest.raises(Exception, match="flaky item 2"):
        await run(node, ["go"])
    # Item 2 spends its three attempts; no other item runs again.
    assert native == petri == {1: 1, 2: 3, 3: 1}


def test_a_parallel_worker_keeps_its_retries_out_of_the_net() -> None:
    for make in (flaky_worker, flaky_node_subclass):
        cw = compile_workflow(make(1))
        assert not [t for t in cw.spec.transition_names if t.startswith("Wf_work_Retry")]
        msgs = [f.message for f in cw.report.findings if f.subject == "work"]
        assert any("each failed item in place (3 attempts)" in m for m in msgs), msgs
