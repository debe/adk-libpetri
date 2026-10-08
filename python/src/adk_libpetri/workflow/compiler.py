"""``from_workflow``: compile an ADK 2 graph ``Workflow`` into a libpetri net (``@experimental``).

The compiler reads ``workflow.graph`` (after ADK's own validation) and emits a
:class:`~adk_libpetri._spec.NetSpec`, per-session action bindings and a
:class:`~adk_libpetri.workflow.report.TranslationReport`. Each ADK node stays
an ADK node: its transition runs it through ADK's own node runner on the
invocation's loop, so session appends, plugins, tracing and event paths are
native. What moves into the net is the *scheduling*: triggers, routes, joins,
retries, concurrency, interrupts and the turn itself, which is what Z3 can
then prove things about.

Translation (``N`` a node, places named ``wf/N/...``):

* **node** -- ``wf/N/trigger`` (FIFO of inputs) and a seeded ``wf/N/idle``,
  which reproduces ADK's per-node serialization. ``Wf_N_Run`` consumes both.
* **edges** -- unrouted targets are in every branch; each route label is one
  XOR branch; ``DEFAULT_ROUTE`` is the branch for no specific match; a node
  with routed edges and no match lands on ``wf/N/unmatched``, making ADK's
  silent branch end a place a proof can name.
* **JoinNode** -- one ``wf/J/from/P`` place per predecessor, consumed together
  (consume semantics; identical to ADK on a DAG).
* **terminal output** -- one ``wf/N/terminalOutput`` per terminal node, holding
  its last output as ADK's ``node_outputs`` does. The node runs with
  ``use_as_output``, so its own event is the workflow's output event. Two
  terminal nodes with output end the turn on ``wf/terminalConflict`` (ADK's
  "multiple terminal outputs" error), which a proof can show unreachable.
* **retry_config** -- a retry loop: a failed attempt ``i`` lands on
  ``wf/N/retryI``, whose timed ``Wf_N_BackoffI`` (ADK's delay for attempt
  ``i``, without its random jitter) moves it to ``wf/N/again``, where
  ``Wf_N_Retry`` runs the next attempt. The node stays busy through the
  backoff, as under ADK's retry loop. One retry transition, not one per
  attempt, keeps the proofs flat in the number of attempts. Every attempt is the same ADK run (same
  node path) and sees its ``ctx.attempt_count``; whether to retry is ADK's
  own ``_should_retry_node``.
* **timeout** -- left on the node: ADK's node runner enforces it per attempt.
* **max_concurrency** -- a seeded ``wf/concurrency`` permit place.
* **RequestInput** (opt-in per node: ``interruptible``) -- the run parks on
  ``wf/parked``; the next turn's function response arrives on ``wf/resumeIn``
  and a ν-join on the interrupt id resumes the node.
* **conditional cycles** -- kept; ``back_edge_budget={(a, b): K}`` routes the
  edge through a budget place with the reask-budget motif (ADR 0007), which
  makes termination provable.
* **turn** -- ``Wf_Start`` takes the input + ``TURN_PERMIT`` (a new workflow
  run: run ids restart at ``@1``, as ADK allocates them per run); the turn
  ends (``Wf_EndTurn*``, priority -100) once every node is idle and no work is
  queued, returning the permit. The end transition records the turn's result
  on the :class:`TurnScope` (output, pending interrupts or failure) and emits
  a marker event; :class:`~adk_libpetri.workflow.PetriWorkflow` hands the
  result to ADK the way ``Workflow`` does, and adds no event of its own.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import itertools
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from google.adk.events.event import Event
from google.adk.workflow import DEFAULT_ROUTE, Workflow
from google.adk.workflow._base_node import START, BaseNode

from .. import colours as C
from .._aio import on_loop
from .._experimental import experimental
from .._spec import (
    Action,
    Ctx,
    Match,
    NetSpec,
    Out,
    Place,
    Port,
    TransitionSpec,
    and_,
    at_least,
    delayed,
    one,
    out,
    xor,
)
from .report import (
    AmbiguousRouteError,
    LoopBudgetExhausted,
    NotInterruptibleError,
    TranslationReport,
    WorkflowTranslationError,
)
from .tokens import NodeOutput, Parked, Resumed, ResumeTrigger, Retry, WfToken, WorkflowFailure

MultiRoute = Literal["reject", "first"]
StateMode = Literal["reject", "legacy_read"]

INPUT: Place[Any] = Place(C.USER_IN.name, object)
"""The turn's input: the user's message for a root workflow, or whatever
``node_input`` a parent passes (a tool's arguments, a predecessor's output),
unchanged, as ``Workflow`` passes it to START's successors."""
TURN_ACTIVE: Place[None] = Place("wf/turnActive")
CONFLICT: Place[None] = Place("wf/terminalConflict")
FAILED: Place[WorkflowFailure] = Place("wf/failed", WorkflowFailure)
PARKED: Place[Parked] = Place("wf/parked", Parked)
RESUME_IN: Place[Resumed] = Place("wf/resumeIn", Resumed)
RESUMED: Place[Resumed] = Place("wf/resumed", Resumed)
CONCURRENCY: Place[None] = Place("wf/concurrency")
QUIET: Place[None] = Place("wf/quiet")
"""Held by every bookkeeping transition (budgeted edge, resume match) while it
fires, and read by every turn end. Without it, such a transition in flight
holds its token in no place, and the turn could end mid-step (a
counterexample the verifier found without assuming atomic firing)."""


def _idle(n: str) -> Place[None]:
    return Place(f"wf/{n}/idle")


def _trigger(n: str) -> Place[WfToken]:
    return Place(f"wf/{n}/trigger", WfToken)


def _from(n: str, p: str) -> Place[WfToken]:
    return Place(f"wf/{n}/from/{p}", WfToken)


def _terminal(n: str) -> Place[NodeOutput]:
    return Place(f"wf/{n}/terminalOutput", NodeOutput)


def _again(n: str) -> Place[Retry]:
    """A retry whose backoff is over: the node's next attempt, any attempt."""
    return Place(f"wf/{n}/again", Retry)


def _retry(n: str, i: int) -> Place[Retry]:
    """Attempt ``i`` failed; its backoff (ADK's delay for attempt ``i``) runs."""
    return Place(f"wf/{n}/retry{i}", Retry)


def _unmatched(n: str) -> Place[WfToken]:
    return Place(f"wf/{n}/unmatched", WfToken)


def _resume(n: str) -> Place[ResumeTrigger]:
    return Place(f"wf/{n}/resume", ResumeTrigger)


def _edge(a: str, b: str) -> Place[WfToken]:
    return Place(f"wf/edge/{a}->{b}", WfToken)


def _budget(a: str, b: str) -> Place[None]:
    return Place(f"wf/budget/{a}->{b}")


@dataclass(frozen=True)
class TurnResult:
    """How a turn ended, as ``Workflow._finalize`` would leave its context."""

    kind: Literal["output", "waiting", "empty", "failed"]
    output: Any = None
    interrupt_ids: frozenset[str] = frozenset()
    failure: WorkflowFailure | None = None


@dataclass
class TurnScope:
    """The ADK invocation a session's compiled net serves right now.

    Set by the workflow agent before each turn's inject. Node actions read it
    to run ADK nodes inside that invocation, on its loop, and the turn's end
    leaves its :class:`TurnResult` here. Not net state: it is the address of
    the caller, like the reply-to of a message.
    """

    ctx: Any = None
    loop: asyncio.AbstractEventLoop | None = None
    result: TurnResult | None = None
    run_ids: dict[str, Any] = field(default_factory=dict)
    """Per-node run counters of the current workflow run (ADK's
    ``_LoopState.run_counters``): reset when a turn starts a new run, kept
    across a resume."""

    def next_run_id(self, node: str) -> str:
        counter = self.run_ids.get(node)
        if counter is None:
            counter = self.run_ids[node] = itertools.count(1)
        return str(next(counter))


@dataclass(frozen=True)
class _Branch:
    """One XOR outcome of a node: where its output goes."""

    key: str
    dests: tuple[str, ...]  # target node names; "" = terminal output


@dataclass
class _NodePlan:
    name: str
    node: BaseNode
    is_join: bool
    preds: list[str]
    interruptible: bool
    attempts: int = 1
    backoff_ms: list[int] = field(default_factory=list)
    """Per retry, the delay of its backoff."""
    route_to_branch: dict[Any, int] = field(default_factory=dict)
    default_branch: int | None = None
    branches: list[_Branch] = field(default_factory=list)
    unmatched: bool = False
    terminal: bool = False
    wait_for_output: bool = False
    folded_retries: bool = False
    """Verification net only: the retry loop folded into the run (see
    :attr:`CompiledWorkflow.verification_spec`)."""


@experimental
@dataclass
class CompiledWorkflow:
    workflow: Workflow
    spec: NetSpec
    report: TranslationReport
    verification_spec: NetSpec
    """The net the proofs run on: :attr:`spec` with every retry loop folded
    into its node's run, which gains a branch that only hands the node back
    (a retry dropped after an abort, or cancelled by another node's failure).

    Sound for the proofs: a retry token maps to the run still in flight, and
    the retry loop never strands a token (a pending retry's backoff or retry
    is always enabled, or else its cancel or drop is), so every marking of
    :attr:`spec` maps to one of this net with the same transitions enabled
    outside the loop. Safety and deadlock freedom proved here hold on the
    executed net. It keeps the proofs as cheap as for a node without retries."""
    max_concurrency: int | None
    budgets: dict[tuple[str, str], int]
    _plans: dict[str, _NodePlan]
    _multi_route: MultiRoute

    @property
    def name(self) -> str:
        return self.workflow.name

    @property
    def node_names(self) -> list[str]:
        return list(self._plans)

    def run_nodes(self) -> dict[str, BaseNode]:
        """``Wf_N_Run`` -> the ADK node N it runs."""
        return {f"Wf_{n}_Run": p.node for n, p in self._plans.items()}

    @property
    def terminal_nodes(self) -> list[str]:
        return [n for n, p in self._plans.items() if p.terminal]

    @property
    def interruptible_nodes(self) -> list[str]:
        return [n for n, p in self._plans.items() if p.interruptible]

    def idle_place(self, node: str) -> Place[None]:
        return _idle(node)

    def terminal_place(self, node: str) -> Place[NodeOutput]:
        return _terminal(node)

    def unmatched_places(self) -> list[Place[WfToken]]:
        return [_unmatched(n) for n, p in self._plans.items() if p.unmatched]

    def initial_marking(self) -> dict[str, list[Any]]:
        """What a runner must seed besides ``TURN_PERMIT``: idles and concurrency permits."""
        m: dict[str, list[Any]] = {_idle(n).name: [None] for n in self._plans}
        m[QUIET.name] = [None]
        if self.max_concurrency:
            m[CONCURRENCY.name] = [None] * self.max_concurrency
        return m

    def initial_counts(self) -> dict[str, int]:
        """:meth:`initial_marking` as counts, plus the permit, for the verifier."""
        counts = {p: len(ts) for p, ts in self.initial_marking().items()}
        counts[C.TURN_PERMIT.name] = 1
        return counts

    def actions(self, scope: TurnScope, author: str | None = None) -> dict[str, Action]:
        """Per-session bindings: every node transition runs in ``scope``'s invocation."""
        return _actions(self, scope, author or self.name)


@experimental
def compile_workflow(
    workflow: Workflow,
    *,
    interruptible: Iterable[str] = (),
    back_edge_budget: Mapping[tuple[str, str], int] | None = None,
    multi_route: MultiRoute = "reject",
    state: StateMode = "reject",
) -> CompiledWorkflow:
    """Compile ``workflow``. Raises :class:`WorkflowTranslationError` for what
    cannot be compiled faithfully; everything approximated is in the report.

    Nodes that interrupt by construction (a ``FunctionNode`` with
    ``auth_config``, an agent with a tool that requires confirmation) are
    compiled interruptible without being named in ``interruptible``.
    """
    if not isinstance(workflow, Workflow):
        raise TypeError(
            f"compile_workflow expects a google.adk Workflow, got {type(workflow).__name__}; "
            "an agent root runs on ADK (or PetriAgent) directly"
        )
    graph = workflow.graph
    report = TranslationReport(workflow.name)
    if graph is None:
        raise ValueError(f"workflow {workflow.name!r} has no graph")
    budgets = dict(back_edge_budget or {})
    hitl = set(interruptible)

    nodes = [n for n in graph.nodes if n is not START and n.name != START.name]
    names = {n.name for n in nodes}
    out_edges: dict[str, list[Any]] = {START.name: []} | {n: [] for n in names}
    preds: dict[str, list[str]] = {n: [] for n in names}
    for e in graph.edges:
        out_edges[e.from_node.name].append(e)
        preds[e.to_node.name].append(e.from_node.name)
    for a, b in budgets:
        if not any(e.from_node.name == a and e.to_node.name == b for e in graph.edges):
            report.add("rejected", f"{a}->{b}", "back_edge_budget names no edge of the graph")
    for n in hitl - names:
        report.add("rejected", n, "interruptible names no node of the graph")
    for node in nodes:
        reason = _static_interrupt(node)
        if reason and node.name not in hitl:
            hitl.add(node.name)
            report.add("exact", node.name, f"{reason}: compiled interruptible")
    if workflow.max_concurrency is not None and workflow.max_concurrency > 0:
        report.add("exact", "max_concurrency", f"seeded permit place of {workflow.max_concurrency}")

    plans: dict[str, _NodePlan] = {}
    for node in nodes:
        plans[node.name] = _plan_node(node, out_edges[node.name], preds[node.name], hitl, report)
        _check_node(node, state, report)
    _check_cycles(graph.edges, budgets, report)
    if report.rejected:
        raise WorkflowTranslationError(report)

    terminals = [n for n, p in plans.items() if p.terminal]
    if len(terminals) > 1:
        report.add(
            "exact",
            "terminal output",
            f"{len(terminals)} terminal nodes {terminals}: two with output in one run end the "
            "turn with ADK's WorkflowConfigurationError (wf/terminalConflict)",
        )
    if (
        any(len(b.dests) > 1 for p in plans.values() for b in p.branches)
        or len(out_edges[START.name]) > 1
    ):
        report.add(
            "approximated",
            "fan-out order",
            "sibling branches run concurrently and complete in scheduling order; ADK starts "
            "them in edge order on one loop. Run ids of a shared successor follow completion "
            "order (max_concurrency=1 makes it deterministic)",
        )
    report.add(
        "approximated",
        "branches",
        "ADK branch scoping is passed through per trigger; isolation scopes are ADK's own",
    )
    report.add(
        "approximated",
        "event replay",
        "resume uses the net's marking, not ADK event replay; in a resumable app the node "
        "emits none of Workflow's agent_state checkpoints or end_of_agent marker",
    )

    spec = _build_spec(
        workflow.name, plans, out_edges[START.name], budgets, workflow.max_concurrency
    )
    folded = {
        n: replace(p, attempts=1, backoff_ms=[], folded_retries=p.attempts > 1)
        for n, p in plans.items()
    }
    verification_spec = _build_spec(
        workflow.name, folded, out_edges[START.name], budgets, workflow.max_concurrency
    )
    for (a, b), k in budgets.items():
        report.add("exact", f"{a}->{b}", f"back edge bounded by a budget of {k} per workflow run")
    return CompiledWorkflow(
        workflow=workflow,
        spec=spec,
        report=report,
        verification_spec=verification_spec,
        max_concurrency=workflow.max_concurrency if workflow.max_concurrency else None,
        budgets=budgets,
        _plans=plans,
        _multi_route=multi_route,
    )


def _retry_plan(node: BaseNode, report: TranslationReport) -> tuple[int, list[int]]:
    """ADK's attempts and backoff (``_get_retry_delay`` without the random draw)."""
    rc = node.retry_config
    if rc is None:
        return 1, []
    attempts = max(1, rc.max_attempts if rc.max_attempts is not None else 5)
    initial = rc.initial_delay if rc.initial_delay is not None else 1.0
    factor = rc.backoff_factor if rc.backoff_factor is not None else 2.0
    cap = rc.max_delay if rc.max_delay is not None else 60.0
    jitter = rc.jitter if rc.jitter is not None else 1.0
    delays: list[int] = []
    for failed in range(1, attempts):
        d = initial * factor ** (failed - 1)
        if jitter > 0.0:
            d = min(d, cap / (1.0 + jitter))  # ADK's cap before jittering
        delays.append(max(1, round(min(d, cap) * 1000)))
    report.add(
        "exact",
        node.name,
        f"retry loop of {attempts} attempts in one run, backoff {delays} ms",
    )
    if jitter > 0.0:
        # A firing window [d(1-j), d(1+j)] would be the honest TPN reading, but
        # libpetri force-disables a window transition that misses its latest
        # bound, and a backoff must never expire.
        report.add(
            "approximated",
            node.name,
            f"retry jitter {jitter} dropped: each backoff waits ADK's undrawn delay",
        )
    return attempts, delays


def _with_attempt(node: BaseNode, attempt: int) -> BaseNode:
    """A copy of ``node`` that runs as attempt ``attempt`` of its run.

    ADK's dynamic node runner always starts a run at attempt 1; the net owns
    the retry, so the copy sets the attempt on its context before the node's
    own body runs (``ctx.attempt_count``, as under ADK's own retry loop).
    """
    from google.adk.utils.context_utils import Aclosing

    copy = node.model_copy(update={"retry_config": None})
    if attempt == 1:
        return copy
    body = copy._run_impl

    async def run_impl(*, ctx: Any, node_input: Any) -> Any:
        ctx._attempt_count = attempt
        async with Aclosing(body(ctx=ctx, node_input=node_input)) as agen:
            async for item in agen:
                yield item

    object.__setattr__(copy, "_run_impl", run_impl)
    return copy


def _should_retry(node: BaseNode, err: BaseException, attempt: int) -> bool:
    from google.adk.workflow._node_state import NodeState
    from google.adk.workflow.utils._retry_utils import _should_retry_node

    return _should_retry_node(err, node.retry_config, NodeState(attempt_count=attempt))


def _routes_of(edge: Any) -> list[Any]:
    r = edge.route
    return list(r) if isinstance(r, list) else [r]


def _plan_node(
    node: BaseNode, edges: list[Any], preds: list[str], hitl: set[str], report: TranslationReport
) -> _NodePlan:
    attempts, backoff = _retry_plan(node, report)
    if node.timeout is not None:
        report.add(
            "exact",
            node.name,
            f"timeout {node.timeout}s kept on the node: ADK's node runner enforces it",
        )
    plan = _NodePlan(
        name=node.name,
        node=node,
        is_join=bool(node._requires_all_predecessors),
        preds=preds,
        interruptible=node.name in hitl,
        attempts=attempts,
        backoff_ms=backoff,
        wait_for_output=bool(node.wait_for_output),
    )
    if plan.is_join:
        report.add(
            "exact",
            node.name,
            f"join over {preds} (consume semantics); its input dict is in edge order, "
            "where ADK's follows set iteration",
        )
    if plan.wait_for_output and not plan.is_join:
        report.add(
            "approximated",
            node.name,
            "wait_for_output: a run without output triggers nothing within the turn; ADK "
            "also keeps the node WAITING into the next turn, the net does not",
        )
    if not edges:
        plan.terminal = True
        plan.branches = [_Branch("terminal", ("",))]
        return plan

    unrouted = tuple(e.to_node.name for e in edges if e.route is None)
    routed = [e for e in edges if e.route is not None and e.route != DEFAULT_ROUTE]
    default = tuple(e.to_node.name for e in edges if e.route == DEFAULT_ROUTE)
    by_dests: dict[tuple[str, ...], int] = {}

    def branch(key: str, dests: tuple[str, ...]) -> int:
        if dests not in by_dests:
            by_dests[dests] = len(plan.branches)
            plan.branches.append(_Branch(key, dests))
        return by_dests[dests]

    if not routed and not default:
        branch("always", unrouted)
        plan.default_branch = 0
        return plan
    labels: list[Any] = []
    for e in routed:
        for r in _routes_of(e):
            if r not in labels:
                labels.append(r)
    for label in labels:
        dests = unrouted + tuple(e.to_node.name for e in routed if label in _routes_of(e))
        plan.route_to_branch[label] = branch(f"route={label!r}", dests)
    fallback = unrouted + default
    if fallback:
        plan.default_branch = branch("default", fallback)
    else:
        plan.unmatched = True
        report.add(
            "exact",
            node.name,
            f"a route other than {labels} ends the branch (ADK logs a warning); the net "
            f"marks wf/{node.name}/unmatched, which 'route coverage' names",
        )
    return plan


# ----------------------------------------------------------------------------
#  Static checks: session-state reads (commitment 2) and interrupt sources
# ----------------------------------------------------------------------------


def _static_interrupt(node: BaseNode) -> str | None:
    """Why ``node`` interrupts by construction, if it does."""
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.workflow import FunctionNode

    inner = _unwrap(node)
    if isinstance(inner, FunctionNode) and inner.auth_config is not None:
        return "requests credentials (auth_config)"
    if isinstance(inner, LlmAgent):
        confirming = [
            getattr(t, "name", type(t).__name__)
            for t in inner.tools
            if getattr(t, "_require_confirmation", False)
        ]
        if confirming:
            return f"tools {confirming} require confirmation"
    return None


def _unwrap(node: BaseNode) -> BaseNode:
    from google.adk.workflow._parallel_worker import _ParallelWorker

    while isinstance(node, _ParallelWorker):
        node = node._node
    return node


def _check_node(
    node: BaseNode, state: StateMode, report: TranslationReport, prefix: str = ""
) -> None:
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.workflow import FunctionNode
    from google.adk.workflow._parallel_worker import _ParallelWorker

    subject = prefix + node.name
    if isinstance(node, _ParallelWorker):
        report.add(
            "opaque",
            subject,
            "parallel worker: the per-item fan-out runs inside one transition, by ADK",
        )
        node = _unwrap(node)
    reads: list[str] = []
    if isinstance(node, Workflow):
        report.add("opaque", subject, "nested Workflow: runs as one node, ADK-scheduled inside")
        for child in node.graph.nodes if node.graph else ():
            if child is not START and child.name != START.name:
                _check_node(child, state, report, f"{subject}/")
        return
    if isinstance(node, FunctionNode):
        ctx_name = node._context_param_name
        sig = getattr(node, "_sig", None)
        if node.parameter_binding == "state" and sig is not None:
            reads += [f"parameter {p}" for p in sig.parameters if p not in (ctx_name, "node_input")]
        if sig is not None and ctx_name in sig.parameters:
            found = _ctx_state_reads(node, ctx_name)
            if found is None:
                report.add(
                    "approximated",
                    subject,
                    f"takes {ctx_name} but its source is unavailable: it may read ctx.state",
                )
            else:
                reads += found
            report.add(
                "opaque",
                subject,
                f"takes {ctx_name}: children it runs via ctx.run_node are ADK-scheduled inside "
                "this transition, and the proofs do not see them",
            )
        report.add("exact", subject, "FunctionNode, run by ADK's node runner")
    elif isinstance(node, LlmAgent):
        if node.mode in ("task", "chat"):
            report.add(
                "rejected",
                subject,
                f"mode={node.mode!r} agent: it waits for the user across turns inside one "
                "workflow run, which the net does not model; run it as a PetriAgent or use "
                "mode='single_turn'",
            )
        reads += [f"instruction {{{v}}}" for v in _template_vars(node)]
        report.add("opaque", subject, "LlmAgent, run by ADK's node runner as one transition")
    else:
        report.add("opaque", subject, f"{type(node).__name__}, run by ADK's node runner")
    if reads:
        if state == "reject":
            report.add(
                "rejected",
                subject,
                f"reads session state ({', '.join(reads)}) (commitment 2: the marking is the "
                "state); pass state='legacy_read' to accept",
            )
        else:
            report.add("approximated", subject, f"reads legacy session state ({', '.join(reads)})")


def _template_vars(agent: Any) -> list[str]:
    """Session-state keys an agent's string instructions read (ADK's templating)."""
    from google.adk.flows.llm_flows.prompt._instructions_utils import (
        _TEMPLATE_VAR_PATTERN,
        _is_valid_state_name,
    )

    found: list[str] = []
    for text in (agent.instruction, getattr(agent, "global_instruction", None)):
        if not isinstance(text, str):
            continue
        for m in _TEMPLATE_VAR_PATTERN.finditer(text):
            name = m.group().lstrip("{").rstrip("}").strip().removesuffix("?")
            if name.startswith("artifact.") or not _is_valid_state_name(name):
                continue
            if name not in found:
                found.append(name)
    return found


_STATE_WRITES = frozenset({"update", "__setitem__", "__delitem__"})


def _ctx_state_reads(node: Any, ctx_name: str) -> list[str] | None:
    """``ctx.state`` reads in a FunctionNode's body; ``None`` if its source is unavailable.

    Writes (``ctx.state[k] = v``, ``ctx.state.update(...)``) are the legacy
    write bridge and stay allowed; any other use of ``ctx.state`` counts as
    a read.
    """
    func = getattr(node, "_unwrapped_func", None) or getattr(node, "_func", None)
    if func is None:
        return None
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError, SyntaxError):
        return None
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    reads: list[str] = []
    for n in ast.walk(tree):
        if not (
            isinstance(n, ast.Attribute)
            and n.attr == "state"
            and isinstance(n.value, ast.Name)
            and n.value.id == ctx_name
        ):
            continue
        up = parents.get(n)
        if isinstance(up, ast.Subscript) and isinstance(up.ctx, ast.Store | ast.Del):
            continue
        if isinstance(up, ast.Attribute) and up.attr in _STATE_WRITES:
            continue
        what = f"{ctx_name}.state"
        if isinstance(up, ast.Subscript) and isinstance(up.slice, ast.Constant):
            what += f"[{up.slice.value!r}]"
        elif isinstance(up, ast.Attribute) and isinstance(parents.get(up), ast.Call):
            call = parents[up]
            assert isinstance(call, ast.Call)
            key = call.args[0] if call.args else None
            arg = f"{key.value!r}" if isinstance(key, ast.Constant) else ""
            what += f".{up.attr}({arg})"
        if what not in reads:
            reads.append(what)
    return reads


def _check_cycles(
    edges: list[Any], budgets: Mapping[tuple[str, str], int], report: TranslationReport
) -> None:
    succ: dict[str, list[str]] = {}
    for e in edges:
        succ.setdefault(e.from_node.name, []).append(e.to_node.name)
    budgeted = set(budgets)
    seen: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    def dfs(n: str) -> None:
        seen.add(n)
        stack.append(n)
        on_stack.add(n)
        for m in succ.get(n, []):
            if m in on_stack:
                cycles.append([*stack[stack.index(m) :], m])
            elif m not in seen:
                dfs(m)
        stack.pop()
        on_stack.discard(n)

    for n in list(succ):
        if n not in seen:
            dfs(n)
    for cyc in cycles:
        edges_of = set(itertools.pairwise(cyc))
        if edges_of & budgeted:
            report.add("exact", "->".join(cyc), "cycle bounded by a back-edge budget")
        else:
            report.add(
                "approximated",
                "->".join(cyc),
                "unbudgeted cycle: deadlock freedom and safety are provable, termination is not; "
                "pass back_edge_budget",
            )


# ----------------------------------------------------------------------------
#  Net construction
# ----------------------------------------------------------------------------


def _dest_place(
    target: str, source: str, plans: dict[str, _NodePlan], budgets: Mapping[Any, int]
) -> Place[WfToken]:
    if (source, target) in budgets:
        return _edge(source, target)
    if plans[target].is_join:
        return _from(target, source)
    return _trigger(target)


def _retry_chain(n: str, p: _NodePlan) -> list[Place[Retry]]:
    if p.attempts == 1:
        return []
    return [_retry(n, i) for i in range(1, p.attempts)] + [_again(n)]


def _work_places(plans: dict[str, _NodePlan], budgets: Mapping[Any, int]) -> list[Place[Any]]:
    ps: list[Place[Any]] = []
    for n, p in plans.items():
        if p.is_join:
            ps += [_from(n, q) for q in p.preds]
        else:
            ps.append(_trigger(n))
        ps += _retry_chain(n, p)
        if p.interruptible:
            ps.append(_resume(n))
    ps += [_edge(a, b) for a, b in budgets]
    return list({q.name: q for q in ps}.values())


def _build_spec(
    name: str,
    plans: dict[str, _NodePlan],
    start_edges: list[Any],
    budgets: Mapping[tuple[str, str], int],
    max_conc: int | None,
) -> NetSpec:
    conc = bool(max_conc and max_conc > 0)
    start_dests = [e.to_node.name for e in start_edges]
    ts: list[TransitionSpec] = []
    budget_places = [_budget(a, b) for a, b in budgets]
    interruptible = [n for n, p in plans.items() if p.interruptible]
    terminals = [_terminal(n) for n, p in plans.items() if p.terminal]
    conflict = len(terminals) > 1

    # -- turn start ----------------------------------------------------------
    start_outs: list[Any] = [TURN_ACTIVE]
    start_outs += [_dest_place(d, START.name, plans, budgets) for d in start_dests]
    start_outs += budget_places
    ts.append(
        TransitionSpec(
            "Wf_Start",
            (one(INPUT), one(C.TURN_PERMIT)),
            and_(*start_outs),
            resets=(PARKED, *budget_places, *((CONFLICT,) if conflict else ())),
        )
    )
    if interruptible:
        ts.append(
            TransitionSpec(
                "Wf_Resume",
                (at_least(1, RESUME_IN), one(C.TURN_PERMIT)),
                and_(TURN_ACTIVE, RESUMED),
            )
        )
        resume_outs = [_resume(n) for n in interruptible]
        ts.append(
            TransitionSpec(
                "Wf_ResumeMatch",
                (one(PARKED), one(RESUMED), one(QUIET)),
                xor(*(and_(r, QUIET) for r in resume_outs)),
                match=Match(
                    ((PARKED, lambda t: t.interrupt_id), (RESUMED, lambda t: t.interrupt_id))
                ),
            )
        )
        ts.append(
            TransitionSpec(
                "Wf_DropResume",
                (one(RESUMED),),
                None,
                inhibitors=(PARKED,),
                priority=-50,
            )
        )

    # -- nodes -----------------------------------------------------------------
    for n, p in plans.items():
        common_in = [one(_idle(n))] + ([one(CONCURRENCY)] if conc else [])
        give_back: list[Place[Any]] = [_idle(n)] + ([CONCURRENCY] if conc else [])
        # A terminal run replaces the node's earlier output (ADK: node_outputs[n]).
        own = (_terminal(n),) if p.terminal else ()
        ins = [one(_from(n, q)) for q in p.preds] if p.is_join else [one(_trigger(n))]
        first_retry = [_retry(n, 1)] if p.attempts > 1 else []
        ts.append(
            TransitionSpec(
                f"Wf_{n}_Run",
                (*ins, *common_in),
                _node_output(n, p, plans, budgets, give_back, first_retry),
                inhibitors=(FAILED,),
                resets=own,
            )
        )
        if p.attempts > 1:
            # A retry keeps the node busy through its backoff, as ADK's retry
            # loop does: the retry token holds the idle token (and the
            # concurrency permit) the first run took.
            later = [_retry(n, i) for i in range(2, p.attempts)]
            ts.append(
                TransitionSpec(
                    f"Wf_{n}_Retry",
                    (one(_again(n)),),
                    _node_output(n, p, plans, budgets, give_back, later),
                    inhibitors=(FAILED,),
                    resets=own,
                )
            )
            for i in range(1, p.attempts):
                ts.append(
                    TransitionSpec(
                        f"Wf_{n}_Backoff{i}",
                        (one(_retry(n, i)), one(QUIET)),
                        and_(_again(n), QUIET),
                        inhibitors=(FAILED,),
                        timing=delayed(p.backoff_ms[i - 1]),
                    )
                )
        for q in _retry_chain(n, p):
            stage = q.name.rsplit("/", 1)[1]
            # A retry that will not run hands the node back: after an abort
            # (no turn is active), or once another node failed the run (ADK
            # cancels the pending tasks then).
            for why, read in (("DropRetry", C.TURN_PERMIT), ("CancelRetry", FAILED)):
                ts.append(
                    TransitionSpec(
                        f"Wf_{n}_{why}_{stage}",
                        (one(q),),
                        and_(*give_back),
                        reads=(read,),
                        priority=30,
                    )
                )
        if p.interruptible:
            ts.append(
                TransitionSpec(
                    f"Wf_{n}_ResumeRun",
                    (one(_resume(n)), *common_in),
                    _node_output(n, p, plans, budgets, give_back),
                    inhibitors=(FAILED,),
                    resets=own,
                )
            )

    # -- budgeted back edges -------------------------------------------------
    for a, b in budgets:
        target = _from(b, a) if plans[b].is_join else _trigger(b)
        ts.append(
            TransitionSpec(
                f"Wf_Edge_{a}_{b}",
                (one(_edge(a, b)), one(_budget(a, b)), one(QUIET)),
                and_(target, QUIET),
                priority=10,
            )
        )
        ts.append(
            TransitionSpec(
                f"Wf_Edge_{a}_{b}_Exhausted",
                (one(_edge(a, b)), one(QUIET)),
                and_(FAILED, QUIET),
                inhibitors=(_budget(a, b),),
                priority=-10,
            )
        )

    # -- turn end ----------------------------------------------------------------
    idles = tuple(_idle(n) for n in plans)
    work = _work_places(plans, budgets)
    # Attempt and retry tokens stand for a node that is still busy (they hold
    # its idle token), so no turn end may reset them: that would lose the
    # idle. They drain through Wf_N_DropRetry once no turn is active. Nor do
    # the turn ends inhibit on them: each end reads every idle token, which
    # is absent while a retry is pending (the P-invariant the verifier finds).
    in_retry = {q.name for n, p in plans.items() for q in _retry_chain(n, p)}
    clearable = tuple(q for q in work if q.name not in in_retry)
    unmatched = tuple(_unmatched(n) for n, p in plans.items() if p.unmatched)
    ends = (C.EVENT_OUT, C.TURN_PERMIT)
    no_park = (PARKED,) if interruptible else ()
    if interruptible:
        ts.append(
            TransitionSpec(
                "Wf_EndTurnWaiting",
                (one(TURN_ACTIVE),),
                and_(*ends),
                reads=(PARKED, QUIET, *idles),
                inhibitors=(*clearable, FAILED),
                resets=(*terminals, *unmatched),
                priority=-100,
            )
        )
    for t in terminals:
        others = tuple(o for o in terminals if o is not t)
        ts.append(
            TransitionSpec(
                f"Wf_EndTurnOutput_{t.name.split('/')[1]}",
                (one(TURN_ACTIVE), one(t)),
                and_(*ends),
                reads=(QUIET, *idles),
                inhibitors=(*clearable, FAILED, *no_park),
                resets=(*others, *unmatched, *budget_places),
                priority=-100,
            )
        )
    if conflict:
        for t1, t2 in itertools.combinations(terminals, 2):
            ts.append(
                TransitionSpec(
                    f"Wf_EndTurnConflict_{t1.name.split('/')[1]}_{t2.name.split('/')[1]}",
                    (one(TURN_ACTIVE), one(t1), one(t2)),
                    and_(*ends, CONFLICT),
                    reads=(QUIET, *idles),
                    inhibitors=(*clearable, FAILED, *no_park),
                    resets=(*terminals, *unmatched, *budget_places),
                    priority=-95,
                )
            )
    ts.append(
        TransitionSpec(
            "Wf_EndTurnEmpty",
            (one(TURN_ACTIVE),),
            and_(*ends),
            reads=(QUIET, *idles),
            inhibitors=(*clearable, FAILED, *terminals, *no_park),
            resets=(*unmatched, *budget_places),
            priority=-100,
        )
    )
    ts.append(
        TransitionSpec(
            "Wf_EndTurnFailed",
            (one(TURN_ACTIVE), at_least(1, FAILED)),
            and_(*ends),
            reads=(QUIET, *idles),
            resets=(*clearable, *terminals, *unmatched, *budget_places, PARKED),
            priority=-90,
        )
    )
    ts.append(
        TransitionSpec(
            "Wf_AbortTurn",
            (one(C.TURN_ABORT), one(TURN_ACTIVE)),
            out(C.TURN_PERMIT),
            resets=(*clearable, *terminals, *unmatched, *budget_places, PARKED, FAILED),
            priority=30,
        )
    )
    ts.append(
        TransitionSpec(
            "Wf_DropAbort",
            (one(C.TURN_ABORT),),
            None,
            reads=(C.TURN_PERMIT,),
            priority=30,
        )
    )
    extra = [C.TURN_PERMIT, TURN_ACTIVE, *terminals, FAILED, QUIET, *idles]
    if conflict:
        extra.append(CONFLICT)
    if conc:
        extra.append(CONCURRENCY)
    ports = [
        Port("userIn", "in", INPUT),
        Port("turnAbort", "in", C.TURN_ABORT),
        Port("eventOut", "out", C.EVENT_OUT),
    ]
    if interruptible:
        ports.append(Port("resumeIn", "in", RESUME_IN))
    return NetSpec(f"Workflow_{name}", tuple(ts), tuple(extra), tuple(ports))


def _node_output(
    n: str,
    p: _NodePlan,
    plans: dict[str, _NodePlan],
    budgets: Mapping[Any, int],
    give_back: list[Place[Any]],
    retries: list[Place[Retry]] = (),  # type: ignore[assignment]
) -> Out:
    branches: list[Out] = []
    for b in p.branches:
        if b.dests == ("",):
            branches.append(and_(*give_back, _terminal(n)))
            branches.append(and_(*give_back))  # terminal run without output
        elif not b.dests:
            branches.append(and_(*give_back))
        else:
            branches.append(and_(*give_back, *(_dest_place(d, n, plans, budgets) for d in b.dests)))
    if p.unmatched:
        branches.append(and_(*give_back, _unmatched(n)))
    if p.wait_for_output and not p.terminal:
        branches.append(and_(*give_back))
    if p.interruptible:
        branches.append(and_(*give_back, PARKED))
    for retry in retries:
        branches.append(out(retry))  # the node stays busy: idle is not given back
    if p.folded_retries:
        branches.append(and_(*give_back))  # a dropped or cancelled retry
    branches.append(and_(*give_back, FAILED))
    unique: dict[str, Out] = {}
    for br in branches:
        unique.setdefault(repr(br), br)
    return xor(*unique.values())


# ----------------------------------------------------------------------------
#  Actions
# ----------------------------------------------------------------------------


@dataclass
class _Outcome:
    output: Any = None
    route: Any = None
    interrupts: tuple[str, ...] = ()
    error: BaseException | None = None
    error_node_path: str = ""
    branch: str | None = None


def _error_code(err: BaseException) -> str:
    status = getattr(err, "status", None)
    return status if isinstance(status, str) else type(err).__name__


async def _run_node(
    scope: TurnScope,
    node: BaseNode,
    token: WfToken,
    run_id: str,
    use_as_output: bool,
    resume_inputs: dict[str, Any] | None = None,
) -> _Outcome:
    ctx = scope.ctx
    if ctx is None:
        raise RuntimeError("compiled workflow node ran outside a turn (no TurnScope)")
    if use_as_output:
        # ADK allows one delegate per non-Workflow parent; a terminal node can
        # run several times in one run, and its last output is the one kept.
        ctx._output_delegated = False
    try:
        child = await ctx._run_node_internal(
            node,
            node_input=token.input,
            use_as_output=use_as_output,
            return_ctx=True,
            run_id=run_id,
            use_sub_branch=token.use_sub_branch,
            override_branch=token.branch,
            resume_inputs=resume_inputs,
            skip_run_id_validation=True,
        )
    except Exception as err:
        return _Outcome(error=err, error_node_path=f"{ctx.node_path}/{node.name}@{run_id}")
    return _Outcome(
        output=child.output,
        route=child.route,
        interrupts=tuple(sorted(child.interrupt_ids)),
        error=child.error,
        error_node_path=child.error_node_path,
        branch=child._invocation_context.branch,
    )


def _actions(cw: CompiledWorkflow, scope: TurnScope, author: str) -> dict[str, Action]:
    plans = cw._plans
    budgets = cw.budgets
    conc = bool(cw.max_concurrency)
    acts: dict[str, Action] = {}
    start_edges = [e for e in cw.workflow.graph.edges if e.from_node.name == START.name]  # type: ignore[union-attr]
    start_dests = [e.to_node.name for e in start_edges]

    def start(ctx: Ctx) -> None:
        ctx.input(C.TURN_PERMIT)
        content = ctx.input(INPUT)
        ctx.signal(TURN_ACTIVE)
        scope.run_ids.clear()  # a new workflow run: ADK's run ids restart at @1
        sub = len(start_dests) > 1
        for d in start_dests:
            ctx.output(_dest_place(d, START.name, plans, budgets), WfToken(content, None, sub))
        for (a, b), k in budgets.items():
            ctx.output_many(_budget(a, b), [None] * k)

    acts["Wf_Start"] = start

    if any(p.interruptible for p in plans.values()):

        def resume(ctx: Ctx) -> None:
            ctx.input(C.TURN_PERMIT)
            ctx.signal(TURN_ACTIVE)
            ctx.output_many(RESUMED, ctx.inputs(RESUME_IN))

        def resume_match(ctx: Ctx) -> None:
            ctx.input(QUIET)
            ctx.signal(QUIET)
            parked = ctx.input(PARKED)
            answer = ctx.input(RESUMED)
            ctx.output(_resume(parked.node), ResumeTrigger(parked, answer.response))

        acts["Wf_Resume"] = resume
        acts["Wf_ResumeMatch"] = resume_match

        def drop_resume(ctx: Ctx) -> None:
            ctx.input(RESUMED)

        acts["Wf_DropResume"] = drop_resume

    for n, p in plans.items():

        def make_run(
            n: str = n, p: _NodePlan = p, again: bool = False, resume: bool = False
        ) -> Action:
            # One node copy per attempt number, each reporting its ctx.attempt_count.
            execs = {a: _with_attempt(p.node, a) for a in range(1, p.attempts + 1)}

            async def run(ctx: Ctx) -> None:
                resume_inputs = None
                run_id: str | None = None
                attempt = 1
                if again:
                    r = ctx.input(_again(n))
                    token, run_id = r.trigger, r.run_id  # the same ADK run, one attempt on
                    attempt = r.attempt + 1
                elif resume:
                    rt = ctx.input(_resume(n))
                    token = rt.parked.trigger
                    run_id = rt.parked.run_id  # ADK resumes the interrupted run
                    resume_inputs = {rt.parked.interrupt_id: rt.response}
                elif p.is_join:
                    token = WfToken({q: ctx.input(_from(n, q)).input for q in p.preds})
                else:
                    token = ctx.input(_trigger(n))
                if not again:
                    ctx.input(_idle(n))
                    if conc:
                        ctx.input(CONCURRENCY)

                def give_back() -> None:
                    ctx.signal(_idle(n))
                    if conc:
                        ctx.signal(CONCURRENCY)

                if run_id is None:
                    run_id = scope.next_run_id(n)
                if resume and not p.node.rerun_on_resume:
                    outcome = _Outcome(output=next(iter(resume_inputs.values())))  # type: ignore[union-attr]
                else:
                    if scope.loop is None:
                        raise RuntimeError("compiled workflow node ran outside a turn")
                    outcome = await on_loop(
                        _run_node(scope, execs[attempt], token, run_id, p.terminal, resume_inputs),
                        loop=scope.loop,
                    )
                if (
                    not resume
                    and attempt < p.attempts
                    and outcome.error is not None
                    and _should_retry(p.node, outcome.error, attempt)
                ):
                    ctx.output(_retry(n, attempt), Retry(token, run_id, attempt))
                    return
                give_back()
                _route_outcome(ctx, n, p, plans, budgets, token, run_id, outcome, cw._multi_route)

            return run

        acts[f"Wf_{n}_Run"] = make_run()
        if p.attempts > 1:
            acts[f"Wf_{n}_Retry"] = make_run(again=True)
        for i in range(1, p.attempts):

            def backoff(ctx: Ctx, n: str = n, i: int = i) -> None:
                ctx.input(QUIET)
                ctx.signal(QUIET)
                ctx.output(_again(n), ctx.input(_retry(n, i)))

            acts[f"Wf_{n}_Backoff{i}"] = backoff
        for q in _retry_chain(n, p):

            def drop(ctx: Ctx, q: Place[Retry] = q, n: str = n) -> None:
                ctx.input(q)
                ctx.signal(_idle(n))
                if conc:
                    ctx.signal(CONCURRENCY)

            stage = q.name.rsplit("/", 1)[1]
            acts[f"Wf_{n}_DropRetry_{stage}"] = drop
            acts[f"Wf_{n}_CancelRetry_{stage}"] = drop
        if p.interruptible:
            acts[f"Wf_{n}_ResumeRun"] = make_run(resume=True)

    for a, b in budgets:
        target = _from(b, a) if plans[b].is_join else _trigger(b)

        def edge(ctx: Ctx, a: str = a, b: str = b, target: Place[WfToken] = target) -> None:
            ctx.input(QUIET)
            ctx.signal(QUIET)
            ctx.input(_budget(a, b))
            ctx.output(target, ctx.input(_edge(a, b)))

        def exhausted(ctx: Ctx, a: str = a, b: str = b) -> None:
            ctx.input(QUIET)
            ctx.signal(QUIET)
            ctx.input(_edge(a, b))
            msg = f"back edge {a}->{b} exhausted its budget of {budgets[(a, b)]}"
            ctx.output(
                FAILED,
                WorkflowFailure(a, "LoopBudgetExhausted", msg, error=LoopBudgetExhausted(msg)),
            )

        acts[f"Wf_Edge_{a}_{b}"] = edge
        acts[f"Wf_Edge_{a}_{b}_Exhausted"] = exhausted

    def finish(ctx: Ctx, result: TurnResult, **event: Any) -> None:
        """Record the turn's result, then emit the marker event that ends it."""
        ctx.input(TURN_ACTIVE)
        scope.result = result
        ctx.output(C.EVENT_OUT, Event(author=author, **event))
        ctx.signal(C.TURN_PERMIT)

    def end_waiting(ctx: Ctx) -> None:
        ids = frozenset(pk.interrupt_id for pk in ctx.reads(PARKED))
        finish(ctx, TurnResult("waiting", interrupt_ids=ids), long_running_tool_ids=set(ids))

    def end_output(t: Place[NodeOutput]) -> Action:
        def act(ctx: Ctx) -> None:
            value = ctx.input(t).output
            finish(ctx, TurnResult("output", output=value), output=value)

        return act

    def end_conflict(t1: Place[NodeOutput], t2: Place[NodeOutput]) -> Action:
        def act(ctx: Ctx) -> None:
            ctx.input(t1)
            ctx.input(t2)
            ctx.signal(CONFLICT)
            from google.adk.workflow._errors import WorkflowConfigurationError

            msg = (
                f"Workflow {cw.name}: multiple terminal nodes produced output. "
                "A workflow must have at most one terminal output."
            )
            failure = WorkflowFailure(
                cw.name, "WorkflowConfigurationError", msg, error=WorkflowConfigurationError(msg)
            )
            finish(
                ctx,
                TurnResult("failed", failure=failure),
                error_code=failure.error_code,
                error_message=msg,
            )

        return act

    def end_empty(ctx: Ctx) -> None:
        finish(ctx, TurnResult("empty"))

    def end_failed(ctx: Ctx) -> None:
        f = ctx.inputs(FAILED)[0]
        finish(
            ctx,
            TurnResult("failed", failure=f),
            error_code=f.error_code,
            error_message=f"node {f.node!r} failed: {f.message}",
        )

    def abort(ctx: Ctx) -> None:
        ctx.input(C.TURN_ABORT)
        ctx.input(TURN_ACTIVE)
        ctx.signal(C.TURN_PERMIT)

    if any(p.interruptible for p in plans.values()):
        acts["Wf_EndTurnWaiting"] = end_waiting
    terminals = [_terminal(n) for n, p in plans.items() if p.terminal]
    for t in terminals:
        acts[f"Wf_EndTurnOutput_{t.name.split('/')[1]}"] = end_output(t)
    if len(terminals) > 1:
        for t1, t2 in itertools.combinations(terminals, 2):
            acts[f"Wf_EndTurnConflict_{t1.name.split('/')[1]}_{t2.name.split('/')[1]}"] = (
                end_conflict(t1, t2)
            )
    acts["Wf_EndTurnEmpty"] = end_empty
    acts["Wf_EndTurnFailed"] = end_failed
    acts["Wf_AbortTurn"] = abort

    def drop_abort(ctx: Ctx) -> None:
        ctx.input(C.TURN_ABORT)

    acts["Wf_DropAbort"] = drop_abort
    cw.spec.check_bindings(acts)
    return acts


def _route_outcome(
    ctx: Ctx,
    n: str,
    p: _NodePlan,
    plans: dict[str, _NodePlan],
    budgets: Mapping[Any, int],
    token: WfToken,
    run_id: str,
    outcome: _Outcome,
    multi_route: MultiRoute,
) -> None:
    if outcome.error is not None:
        # ADK's node runner has recorded its error event; retries are spent.
        err = outcome.error
        ctx.output(
            FAILED,
            WorkflowFailure(
                n, _error_code(err), str(err), err, outcome.error_node_path, from_node=True
            ),
        )
        return
    if outcome.interrupts:
        if not p.interruptible:
            msg = f"node {n!r} requested input but was not compiled as interruptible"
            ctx.output(
                FAILED,
                WorkflowFailure(n, "NotInterruptibleError", msg, NotInterruptibleError(msg)),
            )
            return
        for iid in outcome.interrupts:
            ctx.output(PARKED, Parked(n, iid, token, run_id))
        return
    if p.terminal:
        if outcome.output is not None:
            ctx.output(_terminal(n), NodeOutput(n, outcome.output))
        return
    if p.wait_for_output and outcome.output is None and outcome.route is None:
        return
    route = outcome.route
    index: int | None
    if isinstance(route, list):
        hits = [p.route_to_branch[r] for r in route if r in p.route_to_branch]
        if len(set(hits)) > 1 and multi_route == "reject":
            msg = (
                f"node {n!r} emitted routes {route!r} matching several branches; "
                "compile with multi_route='first' to take the first"
            )
            ctx.output(
                FAILED, WorkflowFailure(n, "AmbiguousRouteError", msg, AmbiguousRouteError(msg))
            )
            return
        index = hits[0] if hits else p.default_branch
    elif route is not None and route in p.route_to_branch:
        index = p.route_to_branch[route]
    else:
        index = p.default_branch
    if index is None:
        ctx.output(_unmatched(n), WfToken(outcome.output, outcome.branch))
        return
    dests = p.branches[index].dests
    sub = len(dests) > 1
    for d in dests:
        ctx.output(_dest_place(d, n, plans, budgets), WfToken(outcome.output, outcome.branch, sub))


__all__ = [
    "CONCURRENCY",
    "CONFLICT",
    "FAILED",
    "INPUT",
    "PARKED",
    "QUIET",
    "RESUMED",
    "RESUME_IN",
    "TURN_ACTIVE",
    "CompiledWorkflow",
    "TurnResult",
    "TurnScope",
    "compile_workflow",
]
