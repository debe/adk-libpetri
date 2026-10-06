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
* **terminal output** -- ``wf/terminalOutput``; ``place_bound(..., 1)`` proves
  ADK's runtime "multiple terminal outputs" error away.
* **retry_config** -- attempts unrolled into ``Wf_N_Run``..``Wf_N_Run_k`` with
  ``delayed`` backoff transitions between them (jitter dropped).
* **timeout** -- enforced in the action; a timed-out run is a failure branch.
* **max_concurrency** -- a seeded ``wf/concurrency`` permit place.
* **RequestInput** (opt-in per node: ``interruptible``) -- the run parks on
  ``wf/parked``; the next turn's function response arrives on ``wf/resumeIn``
  and a ν-join on the interrupt id resumes the node.
* **conditional cycles** -- kept; ``back_edge_budget={(a, b): K}`` routes the
  edge through a budget place with the reask-budget motif (ADR 0007), which
  makes termination provable.
* **turn** -- ``Wf_Start`` takes ``USER_IN`` + ``TURN_PERMIT``; the turn ends
  (``Wf_EndTurn*``, priority -100) once every node is idle and no work is
  queued, returning the permit and emitting the final event.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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
from .report import TranslationReport, WorkflowTranslationError
from .tokens import NodeOutput, Parked, Resumed, ResumeTrigger, WfToken, WorkflowFailure

MultiRoute = Literal["reject", "first"]
StateMode = Literal["reject", "legacy_read"]

TURN_ACTIVE: Place[None] = Place("wf/turnActive")
TERMINAL: Place[NodeOutput] = Place("wf/terminalOutput", NodeOutput)
FAILED: Place[WorkflowFailure] = Place("wf/failed", WorkflowFailure)
PARKED: Place[Parked] = Place("wf/parked", Parked)
RESUME_IN: Place[Resumed] = Place("wf/resumeIn", Resumed)
RESUMED: Place[Resumed] = Place("wf/resumed", Resumed)
CONCURRENCY: Place[None] = Place("wf/concurrency")
QUIET: Place[None] = Place("wf/quiet")
"""Held by every bookkeeping transition (backoff, budgeted edge, resume match)
while it fires, and read by every turn end. Without it, such a transition in
flight holds its token in no place, and the turn could end mid-retry (a
counterexample the verifier found without assuming atomic firing)."""


def _idle(n: str) -> Place[None]:
    return Place(f"wf/{n}/idle")


def _trigger(n: str) -> Place[WfToken]:
    return Place(f"wf/{n}/trigger", WfToken)


def _from(n: str, p: str) -> Place[WfToken]:
    return Place(f"wf/{n}/from/{p}", WfToken)


def _attempt(n: str, i: int) -> Place[WfToken]:
    return Place(f"wf/{n}/attempt{i}", WfToken)


def _retry(n: str, i: int) -> Place[WfToken]:
    return Place(f"wf/{n}/retry{i}", WfToken)


def _unmatched(n: str) -> Place[WfToken]:
    return Place(f"wf/{n}/unmatched", WfToken)


def _resume(n: str) -> Place[ResumeTrigger]:
    return Place(f"wf/{n}/resume", ResumeTrigger)


def _edge(a: str, b: str) -> Place[WfToken]:
    return Place(f"wf/edge/{a}->{b}", WfToken)


def _budget(a: str, b: str) -> Place[None]:
    return Place(f"wf/budget/{a}->{b}")


@dataclass
class TurnScope:
    """The ADK invocation a session's compiled net serves right now.

    Set by the workflow agent before each turn's inject. Node actions read it
    to run ADK nodes inside that invocation, on its loop. Not net state: it is
    the address of the caller, like the reply-to of a message.
    """

    ctx: Any = None
    loop: asyncio.AbstractEventLoop | None = None


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
    attempts: int
    delays_ms: list[int]
    timeout_s: float | None
    interruptible: bool
    route_to_branch: dict[Any, int] = field(default_factory=dict)
    default_branch: int | None = None
    branches: list[_Branch] = field(default_factory=list)
    unmatched: bool = False
    terminal: bool = False
    wait_for_output: bool = False


@experimental
@dataclass
class CompiledWorkflow:
    workflow: Workflow
    spec: NetSpec
    report: TranslationReport
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

    def idle_place(self, node: str) -> Place[None]:
        return _idle(node)

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
    cannot be compiled faithfully; everything approximated is in the report."""
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
    if workflow.max_concurrency is not None and workflow.max_concurrency > 0:
        report.add("exact", "max_concurrency", f"seeded permit place of {workflow.max_concurrency}")

    plans: dict[str, _NodePlan] = {}
    for node in nodes:
        plans[node.name] = _plan_node(node, out_edges[node.name], preds[node.name], hitl, report)
        _check_node(node, state, report)
    _check_cycles(graph.edges, budgets, report)
    if report.rejected:
        raise WorkflowTranslationError(report)

    if any(isinstance(n, Workflow) for n in nodes):
        report.add(
            "opaque", "nested Workflow", "runs as one node; its inner graph is ADK-scheduled"
        )
    report.add(
        "approximated",
        "branches",
        "ADK branch scoping is passed through per trigger; isolation scopes are ADK's own",
    )
    report.add(
        "approximated", "event replay", "resume uses the net's marking, not ADK event replay"
    )

    spec = _build_spec(
        workflow.name, plans, out_edges[START.name], budgets, workflow.max_concurrency, report
    )
    return CompiledWorkflow(
        workflow=workflow,
        spec=spec,
        report=report,
        max_concurrency=workflow.max_concurrency if workflow.max_concurrency else None,
        budgets=budgets,
        _plans=plans,
        _multi_route=multi_route,
    )


def _routes_of(edge: Any) -> list[Any]:
    r = edge.route
    return list(r) if isinstance(r, list) else [r]


def _plan_node(
    node: BaseNode, edges: list[Any], preds: list[str], hitl: set[str], report: TranslationReport
) -> _NodePlan:
    rc = node.retry_config
    attempts = 1
    delays: list[int] = []
    if rc is not None:
        attempts = rc.max_attempts if rc.max_attempts is not None else 5
        initial = rc.initial_delay if rc.initial_delay is not None else 1.0
        factor = rc.backoff_factor if rc.backoff_factor is not None else 2.0
        cap = rc.max_delay if rc.max_delay is not None else 60.0
        delays = [int(min(initial * factor**i, cap) * 1000) for i in range(attempts - 1)]
        if rc.jitter:
            report.add("approximated", node.name, "retry jitter dropped; backoff is deterministic")
        report.add("exact", node.name, f"retry unrolled into {attempts} attempts {delays} ms")
    if node.timeout is not None:
        report.add(
            "approximated",
            node.name,
            f"timeout {node.timeout}s enforced in the action; the verifier sees a failure branch",
        )
    plan = _NodePlan(
        name=node.name,
        node=node,
        is_join=bool(node._requires_all_predecessors),
        preds=preds,
        attempts=max(1, attempts),
        delays_ms=delays,
        timeout_s=node.timeout,
        interruptible=node.name in hitl,
        wait_for_output=bool(node.wait_for_output),
    )
    if plan.is_join:
        report.add("exact", node.name, f"join over {preds} (consume semantics)")
    if plan.wait_for_output and not plan.is_join:
        report.add(
            "approximated",
            node.name,
            "wait_for_output: a run without output triggers nothing, as in ADK",
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
    return plan


def _check_node(node: BaseNode, state: StateMode, report: TranslationReport) -> None:
    from google.adk.workflow import FunctionNode

    kind = type(node).__name__
    if isinstance(node, FunctionNode):
        sig = getattr(node, "_sig", None)
        state_params = [
            p for p in (sig.parameters if sig else {}) if p not in ("ctx", "node_input", "self")
        ]
        if state_params:
            if state == "reject":
                report.add(
                    "rejected",
                    node.name,
                    f"reads session state {state_params} (commitment 2: the marking is the "
                    "state); pass state='legacy_read' to accept",
                )
            else:
                report.add("approximated", node.name, f"reads legacy session state {state_params}")
        report.add("exact", node.name, "FunctionNode, run by ADK's node runner")
    else:
        report.add("opaque", node.name, f"{kind}, run by ADK's node runner as one transition")


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


def _work_places(
    plans: dict[str, _NodePlan], budgets: Mapping[Any, int], start_dests: list[str]
) -> list[Place[Any]]:
    ps: list[Place[Any]] = []
    for n, p in plans.items():
        if p.is_join:
            ps += [_from(n, q) for q in p.preds]
        else:
            ps.append(_trigger(n))
        ps += [_attempt(n, i) for i in range(2, p.attempts + 1)]
        ps += [_retry(n, i) for i in range(1, p.attempts)]
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
    report: TranslationReport,
) -> NetSpec:
    conc = bool(max_conc and max_conc > 0)
    start_dests = [e.to_node.name for e in start_edges]
    ts: list[TransitionSpec] = []
    budget_places = [_budget(a, b) for a, b in budgets]
    interruptible = [n for n, p in plans.items() if p.interruptible]

    # -- turn start ----------------------------------------------------------
    start_outs: list[Any] = [TURN_ACTIVE]
    start_outs += [_dest_place(d, START.name, plans, budgets) for d in start_dests]
    start_outs += budget_places
    ts.append(
        TransitionSpec(
            "Wf_Start",
            (one(C.USER_IN), one(C.TURN_PERMIT)),
            and_(*start_outs),
            resets=(PARKED, *budget_places),
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
        for i in range(1, p.attempts + 1):
            if i == 1:
                ins = [one(_from(n, q)) for q in p.preds] if p.is_join else [one(_trigger(n))]
                tname = f"Wf_{n}_Run"
            else:
                ins = [one(_attempt(n, i))]
                tname = f"Wf_{n}_Run{i}"
            fail_to = _retry(n, i) if i < p.attempts else FAILED
            ts.append(
                TransitionSpec(
                    tname,
                    (*ins, *common_in),
                    _node_output(n, p, plans, budgets, give_back, fail_to),
                    inhibitors=(FAILED,),
                )
            )
            if i < p.attempts:
                ts.append(
                    TransitionSpec(
                        f"Wf_{n}_Backoff{i}",
                        (one(_retry(n, i)), one(QUIET)),
                        and_(_attempt(n, i + 1), QUIET),
                        inhibitors=(FAILED,),
                        timing=delayed(max(1, p.delays_ms[i - 1])),
                    )
                )
        if p.interruptible:
            ts.append(
                TransitionSpec(
                    f"Wf_{n}_ResumeRun",
                    (one(_resume(n)), *common_in),
                    _node_output(n, p, plans, budgets, give_back, FAILED),
                    inhibitors=(FAILED,),
                )
            )

    # -- budgeted back edges -------------------------------------------------
    for (a, b), k in budgets.items():
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
        report.add("exact", f"{a}->{b}", f"back edge bounded by a budget of {k}")

    # -- turn end ----------------------------------------------------------------
    idles = tuple(_idle(n) for n in plans)
    work = _work_places(plans, budgets, start_dests)
    unmatched = tuple(_unmatched(n) for n, p in plans.items() if p.unmatched)
    ends = (C.EVENT_OUT, C.TURN_PERMIT)
    if interruptible:
        ts.append(
            TransitionSpec(
                "Wf_EndTurnWaiting",
                (one(TURN_ACTIVE),),
                and_(*ends),
                reads=(PARKED, QUIET, *idles),
                inhibitors=(*work, FAILED),
                resets=(TERMINAL, *unmatched),
                priority=-100,
            )
        )
    no_park = (PARKED,) if interruptible else ()
    ts.append(
        TransitionSpec(
            "Wf_EndTurnOutput",
            (one(TURN_ACTIVE), at_least(1, TERMINAL)),
            and_(*ends),
            reads=(QUIET, *idles),
            inhibitors=(*work, FAILED, *no_park),
            resets=(*unmatched, *budget_places),
            priority=-100,
        )
    )
    ts.append(
        TransitionSpec(
            "Wf_EndTurnEmpty",
            (one(TURN_ACTIVE),),
            and_(*ends),
            reads=(QUIET, *idles),
            inhibitors=(*work, FAILED, TERMINAL, *no_park),
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
            resets=(*work, TERMINAL, *unmatched, *budget_places, PARKED),
            priority=-90,
        )
    )
    ts.append(
        TransitionSpec(
            "Wf_AbortTurn",
            (one(C.TURN_ABORT), one(TURN_ACTIVE)),
            out(C.TURN_PERMIT),
            resets=(*work, TERMINAL, *unmatched, *budget_places, PARKED, FAILED),
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
    extra = [C.TURN_PERMIT, TURN_ACTIVE, TERMINAL, FAILED, QUIET, *idles]
    if conc:
        extra.append(CONCURRENCY)
    ports = [
        Port("userIn", "in", C.USER_IN),
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
    fail_to: Place[Any],
) -> Out:
    branches: list[Out] = []
    for b in p.branches:
        if b.dests == ("",):
            branches.append(and_(*give_back, TERMINAL))
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
    branches.append(and_(*give_back, fail_to))
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
    branch: str | None = None


def _error_code(err: BaseException) -> str:
    status = getattr(err, "status", None)
    return status if isinstance(status, str) else type(err).__name__


def _retryable(node: BaseNode, err: BaseException) -> bool:
    rc = node.retry_config
    if rc is None:
        return False
    if rc.exceptions is None:
        return True
    names = {cls.__name__ for cls in type(err).__mro__ if cls is not object}
    return not names.isdisjoint(rc.exceptions)  # type: ignore[arg-type]


async def _run_node(
    scope: TurnScope,
    node: BaseNode,
    token: WfToken,
    run_id: str,
    timeout_s: float | None,
    resume_inputs: dict[str, Any] | None = None,
) -> _Outcome:
    ctx = scope.ctx
    if ctx is None:
        raise RuntimeError("compiled workflow node ran outside a turn (no TurnScope)")

    async def go() -> Any:
        return await ctx._run_node_internal(
            node,
            node_input=token.input,
            return_ctx=True,
            run_id=run_id,
            use_sub_branch=token.use_sub_branch,
            override_branch=token.branch,
            resume_inputs=resume_inputs,
            skip_run_id_validation=True,
        )

    try:
        child = await (asyncio.wait_for(go(), timeout_s) if timeout_s else go())
    except TimeoutError:
        from google.adk.workflow import NodeTimeoutError

        return _Outcome(error=NodeTimeoutError(node_name=node.name, timeout=timeout_s or 0))
    except Exception as err:
        return _Outcome(error=err)
    return _Outcome(
        output=child.output,
        route=child.route,
        interrupts=tuple(sorted(child.interrupt_ids)),
        error=child.error,
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
        content = ctx.input(C.USER_IN)
        ctx.signal(TURN_ACTIVE)
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
        counter = itertools.count(1)
        node_exec = p.node.model_copy(update={"retry_config": None, "timeout": None})

        def make_run(
            n: str = n,
            p: _NodePlan = p,
            i: int = 1,
            node_exec: BaseNode = node_exec,
            counter: Any = counter,
            resume: bool = False,
        ) -> Action:
            fail_to = _retry(n, i) if (not resume and i < p.attempts) else FAILED

            async def run(ctx: Ctx) -> None:
                resume_inputs = None
                if resume:
                    rt = ctx.input(_resume(n))
                    token = rt.parked.trigger
                    resume_inputs = {rt.parked.interrupt_id: rt.response}
                elif i == 1 and p.is_join:
                    token = WfToken({q: ctx.input(_from(n, q)).input for q in p.preds})
                elif i == 1:
                    token = ctx.input(_trigger(n))
                else:
                    token = ctx.input(_attempt(n, i))
                ctx.input(_idle(n))
                if conc:
                    ctx.input(CONCURRENCY)

                def give_back() -> None:
                    ctx.signal(_idle(n))
                    if conc:
                        ctx.signal(CONCURRENCY)

                if resume and not p.node.rerun_on_resume:
                    outcome = _Outcome(output=next(iter(resume_inputs.values())))  # type: ignore[union-attr]
                else:
                    if scope.loop is None:
                        raise RuntimeError("compiled workflow node ran outside a turn")
                    outcome = await on_loop(
                        _run_node(
                            scope, node_exec, token, str(next(counter)), p.timeout_s, resume_inputs
                        ),
                        loop=scope.loop,
                    )
                give_back()
                _route_outcome(ctx, n, p, plans, budgets, token, outcome, fail_to, cw._multi_route)

            return run

        for i in range(1, p.attempts + 1):
            acts[f"Wf_{n}_Run" if i == 1 else f"Wf_{n}_Run{i}"] = make_run(i=i)
            if i < p.attempts:

                def backoff(ctx: Ctx, n: str = n, i: int = i) -> None:
                    ctx.input(QUIET)
                    ctx.signal(QUIET)
                    ctx.output(_attempt(n, i + 1), ctx.input(_retry(n, i)))

                acts[f"Wf_{n}_Backoff{i}"] = backoff
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
            ctx.output(
                FAILED,
                WorkflowFailure(
                    a, "LoopBudgetExhausted", f"back edge {a}->{b} exhausted its budget"
                ),
            )

        acts[f"Wf_Edge_{a}_{b}"] = edge
        acts[f"Wf_Edge_{a}_{b}_Exhausted"] = exhausted

    def final(**kw: Any) -> Event:
        return Event(author=author, **kw)

    def end_waiting(ctx: Ctx) -> None:
        ctx.input(TURN_ACTIVE)
        ids = sorted({pk.interrupt_id for pk in ctx.reads(PARKED)})
        ctx.output(C.EVENT_OUT, final(long_running_tool_ids=set(ids)))
        ctx.signal(C.TURN_PERMIT)

    def end_output(ctx: Ctx) -> None:
        ctx.input(TURN_ACTIVE)
        outs = ctx.inputs(TERMINAL)
        if len(outs) > 1:
            ctx.output(
                C.EVENT_OUT,
                final(
                    error_code="WorkflowConfigurationError",
                    error_message=f"Workflow {cw.name}: multiple terminal nodes produced output "
                    f"({len(outs)}). A workflow must have at most one terminal output.",
                ),
            )
        else:
            ctx.output(C.EVENT_OUT, final(output=outs[0].output))
        ctx.signal(C.TURN_PERMIT)

    def end_empty(ctx: Ctx) -> None:
        ctx.input(TURN_ACTIVE)
        ctx.output(C.EVENT_OUT, final())
        ctx.signal(C.TURN_PERMIT)

    def end_failed(ctx: Ctx) -> None:
        ctx.input(TURN_ACTIVE)
        failures = ctx.inputs(FAILED)
        f = failures[0]
        ctx.output(
            C.EVENT_OUT,
            final(
                error_code=f.error_code,
                error_message=f"node {f.node!r} failed: {f.message}",
            ),
        )
        ctx.signal(C.TURN_PERMIT)

    def abort(ctx: Ctx) -> None:
        ctx.input(C.TURN_ABORT)
        ctx.input(TURN_ACTIVE)
        ctx.signal(C.TURN_PERMIT)

    if any(p.interruptible for p in plans.values()):
        acts["Wf_EndTurnWaiting"] = end_waiting
    acts["Wf_EndTurnOutput"] = end_output
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
    outcome: _Outcome,
    fail_to: Place[Any],
    multi_route: MultiRoute,
) -> None:
    if outcome.error is not None:
        if fail_to is FAILED or not _retryable(p.node, outcome.error):
            ctx.output(FAILED, WorkflowFailure(n, _error_code(outcome.error), str(outcome.error)))
        else:
            ctx.output(fail_to, token)
        return
    if outcome.interrupts:
        if not p.interruptible:
            ctx.output(
                FAILED,
                WorkflowFailure(
                    n,
                    "WorkflowTranslationError",
                    f"node {n!r} requested input but was not compiled as interruptible",
                ),
            )
            return
        for iid in outcome.interrupts:
            ctx.output(PARKED, Parked(n, iid, token))
        return
    if p.terminal:
        if outcome.output is not None:
            ctx.output(TERMINAL, NodeOutput(n, outcome.output))
        return
    if p.wait_for_output and outcome.output is None and outcome.route is None:
        return
    route = outcome.route
    index: int | None
    if isinstance(route, list):
        hits = [p.route_to_branch[r] for r in route if r in p.route_to_branch]
        if len(set(hits)) > 1 and multi_route == "reject":
            ctx.output(
                FAILED,
                WorkflowFailure(
                    n,
                    "WorkflowTranslationError",
                    f"node {n!r} emitted routes {route!r} matching several branches; "
                    "compile with multi_route='first' to take the first",
                ),
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
    "FAILED",
    "PARKED",
    "QUIET",
    "RESUMED",
    "RESUME_IN",
    "TERMINAL",
    "TURN_ACTIVE",
    "CompiledWorkflow",
    "TurnScope",
    "compile_workflow",
]
