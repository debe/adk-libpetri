"""Pattern A foil: a speculative race (first answer wins, at most one commit) in stock ADK 2.11.

Port of ``PatternA_AdkOnlyFoilTest.java``. Each test green-locks a gap in ADK's own
orchestration; if a future ADK release closes one, its assertion flips red and the
pattern catalog gets updated.

What ``google.adk.workflow.Workflow`` cannot express, and why (ADK Python 2.11 source):

* **No first-wins join.** ``JoinNode._requires_all_predecessors`` is ``True``
  (``workflow/_join_node.py``), and ``Workflow._buffer_barrier_trigger``
  (``workflow/_workflow.py``) only buffers the join's trigger once *every*
  predecessor is ``NodeStatus.COMPLETED``. There is no "any of" or "first of" barrier.
* **A plain successor fires once per branch.** For a node without that flag,
  ``Workflow._buffer_downstream_triggers`` appends one ``Trigger`` per completed
  predecessor, and ``_schedule_ready_nodes`` runs them one after another. A
  "commit" node fed by three racers commits three times, so at-most-once commit
  cannot be expressed in the graph. The workflow's result is then the *last* commit,
  because ``_handle_completion`` overwrites ``node_outputs[name]`` on every run and
  ``_finalize`` reads that slot.
* **Losers are never cancelled.** ``Workflow._run_loop`` keeps calling
  ``asyncio.wait(..., FIRST_COMPLETED)`` until ``pending_tasks`` is empty. It cancels
  pending siblings on only two paths: a node error (``error_shut_down``, the
  workflow fails) or the invocation-wide abort signal (``InvocationContext.abort``;
  the workflow returns before ``_finalize``, so it yields no output). Both throw
  away the winner's result along with the losers.
* **Escalation does not reach a Workflow.** Nothing under ``google/adk/workflow/``
  reads ``event.actions.escalate``. Only the deprecated ``ParallelAgent``
  (``agents/parallel_agent.py``, ``_merge_agent_run`` plus ``_asks_this_agent_to_exit``)
  cancels its siblings on an escalation.

Deviation from the Java foil: the Java version says the agent tree has no
first-wins path. In ADK Python 2.11 the deprecated ``ParallelAgent`` has one: a branch
that escalates ends the merge and cancels the other branches.
``test_foil_deprecated_parallel_agent_escalation_is_first_wins`` locks that in rather
than hiding it. The cost is that the branch has to opt in by escalating, and no
proof says the commit happens at most once.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.workflow import FunctionNode, JoinNode, Workflow
from google.genai import types

from ._harness import Lifecycle, branch, delayed, run_root

FAST, MEDIUM, SLOW = 0.02, 0.12, 0.30
BRANCHES = ("fast", "medium", "slow")


def _racers(life: Lifecycle) -> tuple[FunctionNode, FunctionNode, FunctionNode]:
    return (
        branch("fast", FAST, life),
        branch("medium", MEDIUM, life),
        branch("slow", SLOW, life),
    )


async def test_foil_join_node_waits_for_slowest_branch() -> None:
    life = Lifecycle()
    fast, medium, slow = _racers(life)
    join = JoinNode(name="join")
    commit = FunctionNode(func=lambda node_input: node_input, name="commit")
    workflow = Workflow(
        name="race_join",
        edges=[
            ("START", (fast, medium, slow)),
            (fast, join),
            (medium, join),
            (slow, join),
            (join, commit),
        ],
    )

    run = await run_root(node=workflow)

    # Every branch ran to completion and none was cancelled: the join is a barrier.
    assert sorted(life.finished) == sorted(BRANCHES)
    assert life.cancelled == []
    # The commit sees all three answers, not the first one.
    assert run.outputs_of("commit") == [{name: f"answer from {name}" for name in BRANCHES}]
    # Runtime is bounded below by the slowest branch.
    assert run.elapsed >= SLOW


async def test_foil_plain_successor_commits_once_per_branch() -> None:
    life = Lifecycle()
    fast, medium, slow = _racers(life)
    commits: list[Any] = []

    def commit(node_input: Any) -> str:
        commits.append(node_input)
        return f"commit({node_input})"

    commit_node = FunctionNode(func=commit, name="commit")
    race = Workflow(
        name="race_fanin",
        edges=[
            ("START", (fast, medium, slow)),
            (fast, commit_node),
            (medium, commit_node),
            (slow, commit_node),
        ],
    )
    turn = Workflow(
        name="turn",
        edges=[("START", race, FunctionNode(func=lambda node_input: node_input, name="reply"))],
    )

    run = await run_root(node=turn)

    # The fast answer does reach commit first, but nothing stops the later ones:
    # commit fires once per branch, so at-most-once commit does not hold.
    assert commits == ["answer from fast", "answer from medium", "answer from slow"]
    assert len(run.outputs_of("commit")) == 3
    # Worse, the race's own output is the LAST commit: _finalize reads
    # node_outputs["commit"], which each later run overwrote. Last writer wins.
    assert run.outputs_of("reply") == ["commit(answer from slow)"]
    # And the losers are not cancelled: the workflow runs until the slowest finishes.
    assert life.cancelled == []
    assert run.elapsed >= SLOW


async def test_foil_only_cancellation_is_invocation_abort() -> None:
    # Cancelling the losers requires tripping the invocation-wide abort signal, reachable
    # from a node only through the private ``ctx._invocation_context``. It cancels the
    # losers, but it aborts the whole invocation, not just the race: the race workflow
    # returns before _finalize, nothing downstream of it runs, and the stream ends with
    # an INVOCATION_ABORTED event instead of the turn's answer.
    life = Lifecycle()
    fast, medium, slow = _racers(life)
    commits: list[Any] = []

    def commit_and_abort(ctx: Context, node_input: Any) -> str:
        commits.append(node_input)
        ctx._invocation_context.abort()  # the only lever; private on purpose upstream
        return f"commit({node_input})"

    commit_node = FunctionNode(func=commit_and_abort, name="commit")
    race = Workflow(
        name="race_abort",
        edges=[
            ("START", (fast, medium, slow)),
            (fast, commit_node),
            (medium, commit_node),
            (slow, commit_node),
        ],
    )
    downstream: list[Any] = []

    def use_winner(node_input: Any) -> str:
        downstream.append(node_input)
        return f"reply({node_input})"

    turn = Workflow(
        name="turn", edges=[("START", race, FunctionNode(func=use_winner, name="use_winner"))]
    )

    run = await run_root(node=turn)

    assert commits == ["answer from fast"]
    assert sorted(life.cancelled) == ["medium", "slow"]
    assert run.elapsed < SLOW
    assert run.events[-1].error_code == "INVOCATION_ABORTED"
    # The winner never leaves the race: the node after it does not run.
    assert downstream == []
    assert run.outputs_of("use_winner") == []


async def test_foil_first_wins_requires_single_node_escape() -> None:
    # The escape: collapse the race into one FunctionNode that runs the branches as plain
    # coroutines and cancels the losers itself. It works, but the branches are no longer
    # graph nodes: they get no node events, retry_config, timeouts or checkpoints, and the
    # race and cancellation are hand-written asyncio that ADK does not check.
    life = Lifecycle()
    bodies = [
        delayed("fast", FAST, life),
        delayed("medium", MEDIUM, life),
        delayed("slow", SLOW, life),
    ]

    async def first_wins(node_input: Any) -> str:
        tasks = [asyncio.ensure_future(body(node_input)) for body in bodies]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return next(iter(done)).result()

    workflow = Workflow(
        name="race_escape",
        edges=[("START", FunctionNode(func=first_wins, name="first_wins"))],
    )

    run = await run_root(node=workflow)

    assert run.outputs_of("first_wins") == ["answer from fast"]
    assert sorted(life.cancelled) == ["medium", "slow"]
    assert run.elapsed < SLOW
    # Nothing in the event stream says which branches existed or which won.
    paths = [e.node_info.path for e in run.events if e.node_info and e.node_info.path]
    assert not any(name in path for path in paths for name in BRANCHES)


class _EscalatingBranch(BaseAgent):
    """A sub-agent that answers after `delay_s` and escalates so the parallel parent exits."""

    delay_s: float
    life: Lifecycle

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        self.life.started.append(self.name)
        try:
            await asyncio.sleep(self.delay_s)
        except asyncio.CancelledError:
            self.life.cancelled.append(self.name)
            raise
        self.life.finished.append(self.name)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=types.Content(
                role="model", parts=[types.Part(text=f"answer from {self.name}")]
            ),
            actions=EventActions(escalate=True),
        )


async def test_foil_deprecated_parallel_agent_escalation_is_first_wins() -> None:
    # Deviation from Java, locked in: ADK Python's deprecated ParallelAgent stops at the
    # first escalation from a direct sub-agent and cancels the other branches
    # (_merge_agent_run -> _cancel_tasks). Workflow, its replacement, has no such path.
    from google.adk.agents.parallel_agent import ParallelAgent

    life = Lifecycle()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        parallel = ParallelAgent(
            name="race_parallel",
            sub_agents=[
                _EscalatingBranch(name="fast", delay_s=FAST, life=life),
                _EscalatingBranch(name="medium", delay_s=MEDIUM, life=life),
                _EscalatingBranch(name="slow", delay_s=SLOW, life=life),
            ],
        )

    run = await run_root(agent=parallel)

    assert [e.author for e in run.events if e.author in BRANCHES] == ["fast"]
    assert sorted(life.cancelled) == ["medium", "slow"]
    assert run.elapsed < SLOW


async def test_foil_workflow_ignores_escalation() -> None:
    # The same escalation in a Workflow node changes nothing: the loop still waits for
    # every branch, because google/adk/workflow/ never reads actions.escalate.
    life = Lifecycle()

    async def fast_escalates(node_input: Any) -> AsyncGenerator[Any, None]:
        await asyncio.sleep(FAST)
        life.finished.append("fast")
        yield Event(output="answer from fast", actions=EventActions(escalate=True))

    fast = FunctionNode(func=fast_escalates, name="fast")
    medium, slow = branch("medium", MEDIUM, life), branch("slow", SLOW, life)
    join = JoinNode(name="join")
    workflow = Workflow(
        name="race_escalate",
        edges=[("START", (fast, medium, slow)), (fast, join), (medium, join), (slow, join)],
    )

    run = await run_root(node=workflow)

    assert sorted(life.finished) == sorted(BRANCHES)
    assert life.cancelled == []
    assert run.elapsed >= SLOW
