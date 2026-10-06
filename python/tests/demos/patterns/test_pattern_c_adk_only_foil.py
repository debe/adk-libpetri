"""Pattern C foil: optimistic commit with fallback in stock ADK 2.11.

Run a cheap path and a slow path concurrently, commit the cheap answer when it
validates, fall back to the already-running slow answer when it does not, and never
commit twice. Port of ``PatternC_AdkOnlyFoilTest.java``. Each test green-locks a gap
in ADK's own orchestration; if a future ADK release closes one, its assertion flips red.

What ``google.adk.workflow.Workflow`` can and cannot express (ADK Python 2.11 source):

* **Conditional fallback works, but only sequentially.** A node that emits
  ``Event(route=...)`` selects its outgoing edges (``Graph.get_next_pending_nodes`` in
  ``workflow/_graph.py``, with ``DEFAULT_ROUTE`` as the else branch). So "cheap,
  validate, fall back to slow on failure" is expressible, but the slow path then
  starts only after the cheap path has failed: a failed turn costs cheap plus slow.
* **Pre-warming breaks at-most-once commit.** Fanning cheap and slow out from START
  makes ``commit`` a plain successor of both, and ``_buffer_downstream_triggers``
  triggers it once per arriving predecessor. When cheap validates, commit still fires
  again when slow arrives. The workflow's result becomes the slow answer, because
  ``_handle_completion`` overwrites ``node_outputs["commit"]`` and ``_finalize`` reads
  it. ``_run_loop`` cancels nothing, so the turn lasts as long as the slow path.
* **Joining instead gives up the latency win.** ``JoinNode`` gives one commit but
  waits for both paths (``_buffer_barrier_trigger``), so a validated cheap answer is
  held until the slow path finishes.
* **RetryConfig has no fallback target.** ``BaseNode.retry_config``
  (``workflow/_retry_config.py``) reruns the *same* node up to ``max_attempts`` and
  then fails the workflow. No edge type means "on failure, go to another node".

Deviations from the Java foil, stated rather than forced:

* Java's ``sequential_agent_runs_slow_even_when_cheap_validates`` targets
  ``SequentialAgent``. Its replacement, ``Workflow``, *can* skip the slow path through
  routes (``test_foil_route_fallback_skips_slow_when_cheap_validates``). The claim
  still holds for the deprecated ``SequentialAgent``, locked in by
  ``test_foil_deprecated_sequential_agent_runs_slow_even_when_cheap_validates``.
* Java's ``loop_agent_only_retries_same_agent`` does not carry over as stated: a
  Workflow edge can route back to any node, so loops are not limited to one agent.
  What carries over is that ADK's retry primitive, ``RetryConfig``, reruns only the
  failing node (``test_foil_retry_config_reruns_same_node_only``).
* Java's custom-``BaseAgent`` escape for conditional fallback is not needed: routes
  cover it. What has no Workflow expression is the concurrent (pre-warmed) form
  with a single commit.
"""

from __future__ import annotations

import warnings
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.workflow import DEFAULT_ROUTE, FunctionNode, JoinNode, RetryConfig, Workflow
from google.genai import types

from ._harness import Lifecycle, branch, run_root

CHEAP, SLOW = 0.03, 0.25
THRESHOLD = 50


def _passes(answer: str) -> bool:
    return int(answer.rsplit("score=", 1)[-1]) >= THRESHOLD


def _validate(node_input: str) -> Event:
    """Route on the cheap answer's score: "ok" commits it, anything else falls back."""
    return Event(output=node_input, route="ok" if _passes(node_input) else "fail")


def _committer(commits: list[Any]) -> FunctionNode:
    def commit(node_input: Any) -> str:
        commits.append(node_input)
        return f"commit({node_input})"

    return FunctionNode(func=commit, name="commit")


def _sequential_fallback(life: Lifecycle, cheap_score: int, commits: list[Any]) -> Workflow:
    cheap = branch("cheap", CHEAP, life, answer=f"cheap score={cheap_score}")
    validate = FunctionNode(func=_validate, name="validate")
    slow = branch("slow", SLOW, life, answer="slow score=100")
    commit = _committer(commits)
    return Workflow(
        name="optimistic_routes",
        edges=[
            ("START", cheap, validate),
            (validate, {"ok": commit, DEFAULT_ROUTE: slow}),
            (slow, commit),
        ],
    )


async def test_foil_route_fallback_skips_slow_when_cheap_validates() -> None:
    # Deviation from Java, locked in: Workflow routes DO express conditional skip.
    life, commits = Lifecycle(), []
    run = await run_root(node=_sequential_fallback(life, cheap_score=80, commits=commits))

    assert life.started == ["cheap"]
    assert commits == ["cheap score=80"]
    assert run.elapsed < SLOW


async def test_foil_route_fallback_is_sequential_no_prewarm() -> None:
    # ...but only sequentially: slow starts after cheap has failed validation, so a
    # failed turn pays for both paths back to back.
    life, commits = Lifecycle(), []
    run = await run_root(node=_sequential_fallback(life, cheap_score=10, commits=commits))

    assert life.started == ["cheap", "slow"]
    assert commits == ["slow score=100"]
    assert run.elapsed >= CHEAP + SLOW


async def test_foil_prewarm_fanout_double_commits_and_waits_for_slow() -> None:
    # Pre-warm both paths from START. Cheap validates quickly and commits, but slow is
    # not cancelled, arrives later, and commits AGAIN, and its answer becomes the
    # result of the turn.
    life, commits = Lifecycle(), []
    cheap = branch("cheap", CHEAP, life, answer="cheap score=80")
    slow = branch("slow", SLOW, life, answer="slow score=100")
    validate = FunctionNode(func=_validate, name="validate")
    commit = _committer(commits)
    optimistic = Workflow(
        name="optimistic_prewarm",
        edges=[
            ("START", (cheap, slow)),
            (cheap, validate),
            (validate, {"ok": commit}),
            (slow, commit),
        ],
    )
    turn = Workflow(
        name="turn",
        edges=[
            ("START", optimistic, FunctionNode(func=lambda node_input: node_input, name="reply"))
        ],
    )

    run = await run_root(node=turn)

    assert commits == ["cheap score=80", "slow score=100"]
    assert run.outputs_of("reply") == ["commit(slow score=100)"]
    assert life.cancelled == []
    assert run.elapsed >= SLOW


async def test_foil_join_prewarm_waits_for_slow_even_when_cheap_validates() -> None:
    # Joining both paths restores a single commit, but the validated cheap answer waits
    # for the slow path to finish: the latency the pre-warm was meant to save is lost.
    life, commits = Lifecycle(), []
    cheap = branch("cheap", CHEAP, life, answer="cheap score=80")
    slow = branch("slow", SLOW, life, answer="slow score=100")
    join = JoinNode(name="join")

    def choose(node_input: dict[str, str]) -> str:
        return node_input["cheap"] if _passes(node_input["cheap"]) else node_input["slow"]

    commit = _committer(commits)
    workflow = Workflow(
        name="optimistic_join",
        edges=[
            ("START", (cheap, slow)),
            (cheap, join),
            (slow, join),
            (join, FunctionNode(func=choose, name="choose"), commit),
        ],
    )

    run = await run_root(node=workflow)

    assert commits == ["cheap score=80"]
    assert sorted(life.finished) == ["cheap", "slow"]
    assert run.elapsed >= SLOW


async def test_foil_retry_config_reruns_same_node_only() -> None:
    # RetryConfig is ADK's retry primitive, and it is node-local: the failing cheap node
    # is rerun max_attempts times, then the workflow fails. Nothing falls back to slow.
    attempts: list[int] = []
    life = Lifecycle()

    class CheapUnavailableError(RuntimeError):
        pass

    def cheap(node_input: Any) -> str:
        attempts.append(len(attempts) + 1)
        raise CheapUnavailableError("cheap path unavailable")

    retrying_cheap = FunctionNode(
        func=cheap,
        name="cheap",
        retry_config=RetryConfig(max_attempts=3, initial_delay=0.01, jitter=0.0),
    )
    workflow = Workflow(
        name="optimistic_retry",
        edges=[("START", retrying_cheap, branch("slow", SLOW, life))],
    )

    with pytest.raises(CheapUnavailableError):
        await run_root(node=workflow)

    assert attempts == [1, 2, 3]
    assert life.started == []


class _CountingAgent(BaseAgent):
    """A sub-agent that answers immediately and counts how often it ran."""

    answer: str
    life: Lifecycle  # a dataclass, so pydantic keeps the shared instance (a list is copied)

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        self.life.started.append(self.name)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            content=types.Content(role="model", parts=[types.Part(text=self.answer)]),
        )


async def test_foil_deprecated_sequential_agent_runs_slow_even_when_cheap_validates() -> None:
    # The Java claim, still true for ADK Python's deprecated SequentialAgent: every
    # sub-agent runs in order, with no conditional skip, so slow runs even though
    # cheap's answer already passes the threshold.
    from google.adk.agents.sequential_agent import SequentialAgent

    life = Lifecycle()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        sequence = SequentialAgent(
            name="seq_cheap_then_slow",
            sub_agents=[
                _CountingAgent(name="cheap", answer="cheap score=80", life=life),
                _CountingAgent(name="slow", answer="slow score=100", life=life),
            ],
        )

    run = await run_root(agent=sequence)

    assert life.started == ["cheap", "slow"]
    assert [e.author for e in run.events if e.author in ("cheap", "slow")] == ["cheap", "slow"]
