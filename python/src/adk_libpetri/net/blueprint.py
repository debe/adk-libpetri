"""Net blueprints: a Petri net as data, parsed into a :class:`Blueprint` (``@experimental``).

:func:`parse_blueprint` is a pure function from the YAML mapping of an
``agent_class: adk_libpetri.net.PetriNet`` file (``places``, ``transitions``,
``env``, ``ports``, ``subnets``, ``prove``) and the ADK nodes it names to a
:class:`Blueprint`: one flat :class:`~adk_libpetri._spec.NetSpec`, one
:class:`ActionPlan` per transition, the seeds, the environment places, the
mounted subnets and the proof claims. It does not need ADK's loader; the
:class:`~adk_libpetri.net.PetriNet` node calls it from ``model_post_init``.

Every error is a :class:`BlueprintError` naming the YAML key path it is
about (``transitions.Race_Commit.out.xor[1]``) and how to fix it, so a person
or an agent can repair the file from the message alone.

The format, in short (the full rules are in the docstrings below):

* ``places: {name: {type: dotted.Ref, seed: n | [values]}}`` -- no type is a
  unit place (``Void``); ``userIn``, ``eventOut``, ``turnPermit``,
  ``turnAbort`` and the other catalog colours are implicit with their
  catalog types.
* ``transitions: {Name: {in, out, read, inhibit, reset, priority, timing,
  node | action}}``.
* ``env: [place, ...]``, ``ports: {name: {place, direction}}``,
  ``subnets: {inst: {net | stock, from, bind}}``, ``prove: {options, claims}``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import contextlib
import difflib
import inspect
import os
import re
import threading
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, NoReturn, TypeVar, cast

from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .. import colours as C
from .._aio import HotStream, on_future, on_loop
from .._experimental import experimental
from .._spec import (
    VOID,
    Action,
    And,
    Ctx,
    In,
    Match,
    NetSpec,
    Out,
    OutPlace,
    Place,
    Port,
    Timeout,
    Timing,
    TransitionSpec,
    Xor,
    at_least,
    deadline,
    delayed,
    exact,
    exactly,
    one,
    window,
)
from .._spec import (
    Forward as _Forward,
)
from .._spec import (
    all_tokens as _all_tokens,
)
from ..workflow.compiler import TurnResult, TurnScope

T = TypeVar("T")

# ----------------------------------------------------------------------------
#  Errors and run-time colours
# ----------------------------------------------------------------------------


@experimental
class BlueprintError(Exception):
    """A blueprint that cannot be built: where (a YAML key path), what, and the fix.

    Not a ``ValueError`` on purpose: pydantic re-wraps a ``ValueError`` raised
    while ADK's loader builds the node, which would bury the key path.
    """

    def __init__(
        self, path: str, message: str, hint: str | None = None, source: str | None = None
    ) -> None:
        self.path = path
        self.message = message
        self.hint = hint
        self.source = source
        super().__init__(str(self))

    def with_source(self, source: str | None) -> BlueprintError:
        if source is None or self.source is not None:
            return self
        return BlueprintError(self.path, self.message, self.hint, source)

    def __str__(self) -> str:
        where = f"{self.source}: " if self.source else ""
        text = f"{where}{self.path}: {self.message}"
        return f"{text}. Fix: {self.hint}" if self.hint else text


@experimental
class NetRunError(RuntimeError):
    """A blueprint net failed at run time for a reason of its own (not a node's error)."""


@experimental
@dataclass(frozen=True, slots=True)
class NodeError:
    """The token a node's failure puts on its transition's ``error`` branch.

    Plain data (no exception object), so a node can take it as its input.
    """

    node: str
    error_code: str
    message: str


# ----------------------------------------------------------------------------
#  The turn a session's net serves
# ----------------------------------------------------------------------------

TurnPhase = Literal["closed", "waiting", "draining"]


@dataclass
class NetScope(TurnScope):
    """The invocation a session's blueprint net serves, and the node runs of its turn.

    Turns are served one at a time (:meth:`turn_slot`). A turn is ``waiting``
    from its inject to its first ``eventOut`` token, then ``draining`` until
    every node run it started has finished (they run inside its invocation,
    which must outlive them), then ``closed``. A node transition that fires
    while no turn is open (a seeded one, a timed one after the answer) keeps
    its tokens in flight and runs in the next turn's invocation; if the
    session's net closes first, it fails.
    """

    turn: int = 0
    phase: TurnPhase = "closed"
    egress: HotStream[Any] = field(default_factory=HotStream)
    running: int = 0
    answered_by: list[tuple[str | None, Any]] = field(default_factory=list)
    """Each ``eventOut`` token of this turn with the transition that put it there
    (None when no event named it), in order; the net's egress tap appends."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _drained: asyncio.Event | None = field(default=None, repr=False)
    _deferred: list[tuple[str, cf.Future[int]]] = field(default_factory=list, repr=False)
    _closed: bool = field(default=False, repr=False)
    _turns: asyncio.Lock | None = field(default=None, repr=False)
    _turns_loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    @contextlib.asynccontextmanager
    async def turn_slot(self, net: str) -> AsyncIterator[None]:
        """Hold the session's net for one turn: a second turn waits for this one to end."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._turns is None or self._turns_loop is not loop:
                if self._turns is not None and self._turns.locked():
                    raise NetRunError(
                        f"PetriNet {net!r} got a turn from a second event loop while a turn "
                        "is open; a session's net serves one turn at a time"
                    )
                self._turns = asyncio.Lock()
                self._turns_loop = loop
            turns = self._turns
        async with turns:
            yield

    def open_turn(self, ctx: Any, loop: asyncio.AbstractEventLoop) -> None:
        """Point the scope at this turn's invocation; release node runs that waited for one."""
        with self._lock:
            self.ctx = ctx
            self.loop = loop
            self.turn += 1
            self.phase = "waiting"
            self.result = None
            self.run_ids.clear()
            self.answered_by.clear()
            waiting, self._deferred = self._deferred, []
            self.running += len(waiting)
            turn = self.turn
        for _, fut in waiting:
            fut.set_result(turn)

    def begin_node(self, transition: str) -> int | cf.Future[int]:
        """Count a node run in (any thread): its turn, or a future of the next turn's."""
        with self._lock:
            if self._closed:
                raise NetRunError(
                    f"transition {transition!r} fired while the session's net is closing"
                )
            if self.phase != "closed" and self.ctx is not None and self.loop is not None:
                self.running += 1
                return self.turn
            fut: cf.Future[int] = cf.Future()
            self._deferred.append((transition, fut))
            return fut

    async def tracked(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` (a node run) on the invocation's loop; count it out after."""
        try:
            return await coro
        finally:
            with self._lock:
                self.running -= 1
                done = self._drained if self.running == 0 else None
            if done is not None:
                done.set()

    def record_failure(self, turn: int, result: TurnResult) -> None:
        """A node failed: the turn fails, unless it has its answer already."""
        with self._lock:
            if self.phase == "waiting" and turn == self.turn:
                self.result = result

    def answered(self) -> None:
        with self._lock:
            if self.phase == "waiting":
                self.phase = "draining"

    async def drain(self) -> None:
        """Wait (on the invocation's loop) for the turn's node runs, then close it."""
        while True:
            with self._lock:
                if self.running == 0:
                    self.phase = "closed"
                    self._drained = None
                    # The invocation is over: do not keep it (or its agent tree) alive.
                    self.ctx = None
                    return
                self.phase = "draining"
                self._drained = asyncio.Event()
                done = self._drained
            await done.wait()

    def close(self) -> None:
        """The session's net is draining: node runs still waiting for a turn fail."""
        with self._lock:
            self._closed = True
            waiting, self._deferred = self._deferred, []
        for transition, fut in waiting:
            fut.set_exception(
                NetRunError(
                    f"transition {transition!r} fired with no turn open, and the session's "
                    "net closed before another turn came to run its node"
                )
            )


# ----------------------------------------------------------------------------
#  The blueprint
# ----------------------------------------------------------------------------

ActionKind = Literal["move", "emit", "node"]


@dataclass(frozen=True, slots=True)
class Branch:
    """One outcome an action can choose: the places it marks."""

    label: str | None
    places: tuple[Place[Any], ...]


@experimental
@dataclass(frozen=True)
class ActionPlan:
    """What a transition's action does, decided when the blueprint is parsed.

    * ``move`` -- consume the inputs; the single coloured value goes to every
      coloured output place, unit places are signalled;
    * ``emit`` -- as ``move``, with the value turned into an ``Event`` first;
    * ``node`` -- run :attr:`node` on the turn's invocation with the inputs'
      value, put its output on the chosen branch.

    A timeout branch is the executor's and never one the action chooses.
    """

    transition: str
    kind: ActionKind
    takes: tuple[In, ...]
    reads: tuple[Place[Any], ...]
    """Coloured read places: part of a node's input."""
    branches: tuple[Branch, ...]
    routes: Mapping[str, int] = field(default_factory=dict)
    default: int | None = None
    error: int | None = None
    node: Any = None
    author: str = ""

    def action(self, scope: NetScope, prefix: str = "") -> Action:
        """The action, for a blueprint mounted at ``prefix`` (``""``: the root)."""
        return _plan_action(self, scope, prefix)


@dataclass(frozen=True)
class Mount:
    """A mounted subnet: its places' names in the parent and its actions."""

    prefix: str
    places: Mapping[str, str]
    """Child place name -> parent place name (a bound port, or ``prefix/name``)."""
    actions: Callable[[Any, str], Mapping[str, Action]]
    """Per session (and the prefix the parent is mounted at): the child's actions
    under the child's own names."""
    asynchronous: tuple[str, ...] = ()
    """The child's transitions (parent names) whose actions do not fire in one step."""
    rest: tuple[str, ...] = ()
    """Unbound child places (parent names) where the child's turn leaves tokens."""
    net: str = ""
    """The child blueprint's name (``stock:<kind>`` for a stock subnet)."""
    node_transitions: tuple[str, ...] = ()
    """The child's ``node:`` transitions (parent names): a turn drains their runs."""
    child: Blueprint | None = field(default=None, compare=False, repr=False)
    """The child blueprint (``net:`` mounts; for drawing it)."""
    agent: str | None = None
    """The ``from:`` LlmAgent's name (stock mounts)."""

    def transition(self, name: str) -> str:
        return f"{self.prefix}/{name}"


EnvMode = tuple[Literal["arrivals", "bounded", "always"], int, int]
"""``(kind, min, max)``: ``arrivals`` uses both, ``bounded`` the max."""


@dataclass(frozen=True)
class ProofOptions:
    initial_marking: Mapping[str, int] | None = None
    environment: tuple[str, ...] | None = None
    mode: EnvMode | None = None
    sinks: tuple[str, ...] | None = None
    sinks_when: Mapping[str, tuple[str, ...]] | None = None
    assume_atomic_firing: bool | None = None
    assume_atomic_nodes: bool | None = None
    """The author's word that ``assume_atomic_firing`` is exact on a net with nodes."""

    def over(self, base: ProofOptions) -> ProofOptions:
        """These options, falling back to ``base`` key by key."""

        def pick(mine: Any, theirs: Any) -> Any:
            return mine if mine is not None else theirs

        return ProofOptions(
            pick(self.initial_marking, base.initial_marking),
            pick(self.environment, base.environment),
            pick(self.mode, base.mode),
            pick(self.sinks, base.sinks),
            pick(self.sinks_when, base.sinks_when),
            pick(self.assume_atomic_firing, base.assume_atomic_firing),
            pick(self.assume_atomic_nodes, base.assume_atomic_nodes),
        )


ClaimKind = Literal["deadlock_free", "place_bound", "unreachable", "mutual_exclusion"]


@dataclass(frozen=True)
class Claim:
    kind: ClaimKind
    label: str
    places: tuple[str, ...] = ()
    bound: int = 0
    options: ProofOptions = field(default_factory=ProofOptions)


@dataclass(frozen=True)
class ProofPlan:
    claims: tuple[Claim, ...] = ()
    options: ProofOptions = field(default_factory=ProofOptions)
    on_load: bool = False


@experimental
@dataclass(frozen=True)
class Blueprint:
    """A parsed net: structure, actions, seeds, environment, mounts, proofs."""

    name: str
    spec: NetSpec
    seeds: Mapping[str, tuple[Any, ...]]
    """Tokens each place starts with (``turnPermit`` included when present)."""
    env: tuple[str, ...]
    """Environment places: ``userIn`` (when present) and ``env:``."""
    plans: Mapping[str, ActionPlan]
    mounts: tuple[Mount, ...] = ()
    proof: ProofPlan = field(default_factory=ProofPlan)
    source: str | None = None
    asynchronous: tuple[str, ...] = ()
    """Transitions whose action does not fire in one step: nodes, and the stock
    subnets' (which await a model or a tool)."""
    rest: tuple[str, ...] = ()
    """Where a turn leaves tokens: ``eventOut``, ``turnPermit`` and each mounted
    subnet's unbound ones (the default ``deadlock_free`` sinks)."""
    node_transitions: tuple[str, ...] = ()
    """``node:`` transitions, mounted ones included: the runs a turn drains
    before the next turn can start."""

    def initial_marking(self) -> dict[str, list[Any]]:
        return {p: list(ts) for p, ts in self.seeds.items()}

    def initial_counts(self) -> dict[str, int]:
        return {p: len(ts) for p, ts in self.seeds.items() if ts}

    def actions(self, scope: NetScope, prefix: str = "") -> dict[str, Action]:
        """Per-session bindings for every transition, mounted ones included.

        ``prefix``: where this blueprint is mounted (``""`` at the root). A
        function node mounted under a prefix runs under a name of its own
        (:func:`run_name`).
        """
        acts: dict[str, Action] = {t: plan.action(scope, prefix) for t, plan in self.plans.items()}
        for m in self.mounts:
            for name, act in m.actions(scope, f"{prefix}/{m.prefix}".strip("/")).items():
                acts[m.transition(name)] = _renamed(act, m.places)
        self.spec.check_bindings(acts)
        return acts


# ----------------------------------------------------------------------------
#  Parsing
# ----------------------------------------------------------------------------

CATALOG: dict[str, Place[Any]] = {
    p.name: p
    for p in (
        C.USER_IN,
        C.EVENT_OUT,
        C.TURN_PERMIT,
        C.TURN_ABORT,
        C.LLM_REQUEST,
        C.LLM_RESPONSE,
        C.TOOL_CALLS,
        C.TOOL_RESULTS,
        C.TRANSFER,
        C.LEGACY_SESSION_WRITE,
        C.END_INVOCATION,
    )
}
"""Places a blueprint may use without declaring them, with their catalog types."""

TYPE_ALIASES: dict[str, Any] = {
    "Void": VOID,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "dict": dict,
    "list": list,
    "object": object,
    "Any": object,
    "Content": types.Content,
    "Event": Event,
    "LlmRequest": LlmRequest,
    "LlmResponse": LlmResponse,
    "ToolCalls": C.ToolCalls,
    "ToolResults": C.ToolResults,
    "TransferTarget": C.TransferTarget,
    "NodeError": NodeError,
}
"""Short type names a place may use instead of a dotted reference."""

TOP_KEYS = ("places", "transitions", "env", "ports", "subnets", "prove")
_TRANSITION_KEYS = ("in", "out", "read", "inhibit", "reset", "priority", "timing", "node", "action")
_STOCK = ("llm_agent", "llm_step", "tool_dispatch", "router")


def _suggest(word: str, choices: Iterable[str]) -> str | None:
    close = difflib.get_close_matches(word, list(choices), n=1)
    return f"did you mean {close[0]!r}?" if close else None


def _keys(path: str, d: Any, allowed: Sequence[str], what: str) -> Mapping[str, Any]:
    if d is None:
        return {}
    if not isinstance(d, Mapping):
        raise BlueprintError(path, f"{what} must be a mapping, got {type(d).__name__}")
    for k in d:
        if k not in allowed:
            hint = _suggest(str(k), allowed) or f"the allowed keys are {list(allowed)}"
            raise BlueprintError(f"{path}.{k}" if path else str(k), f"unknown key {k!r}", hint)
    return cast(Mapping[str, Any], d)


def _name(path: str, n: Any, what: str) -> str:
    if not isinstance(n, str) or not n:
        raise BlueprintError(path, f"{what} name must be a non-empty string, got {n!r}")
    if "/" in n:
        raise BlueprintError(
            path, f"{what} name {n!r} contains '/'", "'/' separates a subnet prefix; rename it"
        )
    return n


def _int(path: str, v: Any, *, minimum: int | None = None) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise BlueprintError(path, f"expected an integer, got {v!r}")
    if minimum is not None and v < minimum:
        raise BlueprintError(path, f"expected an integer >= {minimum}, got {v}")
    return v


def _str_list(path: str, v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise BlueprintError(path, f"expected a place name or a list of place names, got {v!r}")
    return list(v)


def resolve_type(path: str, ref: Any, package: str | None) -> Any:
    """A place type: an alias, ``dotted.module.Name``, or ``.module.Name`` in the YAML's package."""
    from google.adk.agents.config_agent_utils import resolve_fully_qualified_name

    if ref is None:
        return VOID
    if not isinstance(ref, str) or not ref:
        raise BlueprintError(path, f"a type is a dotted name, got {ref!r}")
    if ref in TYPE_ALIASES:
        return TYPE_ALIASES[ref]
    name = ref
    if ref.startswith("."):
        if package is None:
            raise BlueprintError(
                path,
                f"leading-dot type {ref!r} needs the YAML file it is relative to",
                "write the fully qualified name (my_pkg.module.Name)",
            )
        name = package + ref
    if "." not in name:
        raise BlueprintError(
            path,
            f"unknown type {ref!r}",
            _suggest(ref, TYPE_ALIASES) or f"use one of {sorted(TYPE_ALIASES)} or a dotted name",
        )
    try:
        t = resolve_fully_qualified_name(name)
    except ValueError as err:
        raise BlueprintError(
            path, f"cannot import type {name!r} ({err.__cause__ or err})", None
        ) from err
    return t


def package_of(source: str | None) -> str | None:
    """The package a YAML file's leading-dot references resolve in (ADK's rule)."""
    if source is None:
        return None
    return os.path.basename(os.path.dirname(os.path.abspath(source)))


class _Places:
    """The places a blueprint knows, with where each was declared."""

    def __init__(self, net: str) -> None:
        self.net = net
        self.by_name: dict[str, Place[Any]] = {}

    def add(self, p: Place[Any], path: str) -> Place[Any]:
        prior = self.by_name.get(p.name)
        if prior is not None and prior.token_type is not p.token_type and prior != p:
            raise BlueprintError(
                path,
                f"place {p.name!r} is {prior.type_name} here and {p.type_name} where it is bound",
                "give both sides one type",
            )
        self.by_name.setdefault(p.name, p)
        return self.by_name[p.name]

    def get(self, path: str, name: Any) -> Place[Any]:
        if not isinstance(name, str):
            raise BlueprintError(path, f"expected a place name, got {name!r}")
        p = self.by_name.get(name)
        if p is None:
            p = CATALOG.get(name)
            if p is None:
                hint = _suggest(name, [*self.by_name, *CATALOG]) or "declare it under places:"
                raise BlueprintError(path, f"unknown place {name!r}", hint)
            self.by_name[name] = p
        return p


@experimental
def parse_blueprint(
    name: str,
    data: Mapping[str, Any],
    *,
    nodes: Mapping[str, Any] | None = None,
    package: str | None = None,
    source: str | None = None,
) -> Blueprint:
    """Parse a blueprint mapping (the YAML keys :data:`TOP_KEYS`) into a :class:`Blueprint`.

    ``nodes`` are the ADK nodes transitions and subnets name, by node name (a
    child ``PetriNet`` among them can be mounted with ``subnets: {x: {net:
    name}}``). ``package`` resolves leading-dot type references; ``source``
    (the YAML path) only labels errors. Raises :class:`BlueprintError`.
    """
    try:
        return _parse(name, data, dict(nodes or {}), package, source)
    except BlueprintError as err:
        raise err.with_source(source) from None


def _parse(
    name: str,
    data: Mapping[str, Any],
    nodes: dict[str, Any],
    package: str | None,
    source: str | None,
) -> Blueprint:
    data = _keys("", data, TOP_KEYS, "a blueprint")
    _no_blocked_keys("", data)
    places = _Places(name)
    seeds: dict[str, tuple[Any, ...]] = {}

    # -- places ------------------------------------------------------------
    raw_places = _keys("places", data.get("places"), _AnyKeys(), "places")
    for pname, decl in raw_places.items():
        path = f"places.{pname}"
        _name(path, pname, "place")
        decl = _keys(path, decl, ("type", "seed"), f"place {pname!r}")
        t = resolve_type(f"{path}.type", decl.get("type"), package) if "type" in decl else VOID
        p = places.add(Place(pname, t), path)
        if "seed" in decl:
            seeds[pname] = _seed(f"{path}.seed", p, decl["seed"])

    # -- subnets -----------------------------------------------------------
    mounts: list[Mount] = []
    mounted_specs: list[NetSpec] = []
    env: list[str] = []
    raw_subnets = _keys("subnets", data.get("subnets"), _AnyKeys(), "subnets")
    for inst, decl in raw_subnets.items():
        path = f"subnets.{inst}"
        _name(path, inst, "subnet instance")
        mount, spec, child_seeds, child_env = _mount(path, inst, decl, nodes, places)
        mounts.append(mount)
        env.extend(child_env)
        mounted_specs.append(spec)
        for p, ts in child_seeds.items():
            seeds[p] = ts
        for p in spec.places:
            places.by_name.setdefault(p.name, p)

    # -- transitions -------------------------------------------------------
    raw_ts = _keys("transitions", data.get("transitions"), _AnyKeys(), "transitions")
    own: list[TransitionSpec] = []
    plans: dict[str, ActionPlan] = {}
    for tname, decl in raw_ts.items():
        path = f"transitions.{tname}"
        _name(path, tname, "transition")
        t, plan = _transition(path, tname, decl, places, nodes, name)
        own.append(t)
        plans[tname] = plan
    if not own and not mounts:
        raise BlueprintError("transitions", "a net needs at least one transition or subnet")
    for m in mounts:
        # The drawing titles a collapsed subnet by its prefix, as it titles a
        # place or transition by its name; one name may not mean both.
        clash = (
            "place" if m.prefix in places.by_name else "transition" if m.prefix in plans else None
        )
        if clash is not None:
            raise BlueprintError(
                f"subnets.{m.prefix}",
                f"subnet instance {m.prefix!r} has the name of a {clash}",
                "rename the instance or the " + clash,
            )

    # -- env -----------------------------------------------------------------
    for i, pn in enumerate(_list("env", data.get("env"))):
        p = places.get(f"env[{i}]", pn)
        if p.name not in env:
            env.append(p.name)

    # -- the spec ----------------------------------------------------------
    declared = [places.by_name[n] for n in raw_places]
    try:
        spec = NetSpec.compose(name, *own, *mounted_specs, extra_places=declared)
    except (TypeError, ValueError) as err:
        raise BlueprintError("transitions", str(err), "give each place one type") from err
    for t in spec.transitions:
        if t.name in plans or any(t.name.startswith(m.prefix + "/") for m in mounts):
            continue
        raise BlueprintError(f"transitions.{t.name}", "transition without an action plan")
    if spec.has_place(C.USER_IN) and C.USER_IN.name not in env:
        env.insert(0, C.USER_IN.name)

    # -- ports ---------------------------------------------------------------
    ports = _ports(data.get("ports"), spec, places)
    spec = NetSpec(spec.name, spec.transitions, spec.extra_places, ports, spec.membership)

    if spec.has_place(C.TURN_PERMIT) and C.TURN_PERMIT.name not in seeds:
        seeds[C.TURN_PERMIT.name] = (None,)
    node_transitions = (
        *(t for t, plan in plans.items() if plan.kind == "node"),
        *(t for m in mounts for t in m.node_transitions),
    )
    asynchronous = (
        *(t for t, plan in plans.items() if plan.kind == "node"),
        *(t for m in mounts for t in m.asynchronous),
    )
    rest = (
        *(p.name for p in (C.EVENT_OUT, C.TURN_PERMIT) if spec.has_place(p)),
        *(p for m in mounts for p in m.rest),
    )
    proof = _proof(data.get("prove"), spec, tuple(env), asynchronous, mounts)
    return Blueprint(
        name=name,
        spec=spec,
        seeds=seeds,
        env=tuple(env),
        plans=plans,
        mounts=tuple(mounts),
        proof=proof,
        source=source,
        asynchronous=asynchronous,
        rest=tuple(dict.fromkeys(rest)),
        node_transitions=node_transitions,
    )


_BLOCKED_KEYS = frozenset({"args"})
"""Keys ADK's web UI refuses anywhere in an agent YAML (``config_agent_utils``)."""


def _no_blocked_keys(path: str, v: Any) -> None:
    """``adk web`` refuses a file with a key named ``args`` at any depth: so do we."""
    if isinstance(v, Mapping):
        for k, child in cast(Mapping[Any, Any], v).items():
            where = f"{path}.{k}" if path else str(k)
            if k in _BLOCKED_KEYS:
                raise BlueprintError(
                    where,
                    f"ADK's web UI refuses any YAML key named {k!r}, so adk web would not "
                    "load this file",
                    f"rename {k!r} (a place, transition, port, subnet or route label)",
                )
            _no_blocked_keys(where, child)
    elif isinstance(v, list):
        for i, child in enumerate(cast(list[Any], v)):
            _no_blocked_keys(f"{path}[{i}]", child)


class _AnyKeys(tuple[str, ...]):
    """``allowed`` for a mapping whose keys are names (every key is allowed)."""

    def __contains__(self, _: object) -> bool:
        return True


def _list(path: str, v: Any) -> list[Any]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise BlueprintError(path, f"expected a list, got {v!r}")
    return v


def _seed(path: str, p: Place[Any], v: Any) -> tuple[Any, ...]:
    if p.is_unit:
        return (None,) * _int(path, v, minimum=0)
    if not isinstance(v, list):
        raise BlueprintError(
            path,
            f"place {p.name!r} is coloured ({p.type_name}): its seed lists the tokens",
            "write seed: [value, ...], or make it a unit place to seed a count",
        )
    return tuple(v)


# -- arcs ---------------------------------------------------------------------


def _in(path: str, item: Any, places: _Places) -> In:
    if isinstance(item, str):
        return one(places.get(path, item))
    d = _keys(path, item, ("place", "count", "at_least", "all"), "an input arc")
    if "place" not in d:
        raise BlueprintError(path, "an input arc needs 'place'", "write {place: p, count: n}")
    p = places.get(f"{path}.place", d["place"])
    forms = [k for k in ("count", "at_least", "all") if k in d]
    if len(forms) > 1:
        raise BlueprintError(path, f"an input arc takes one of count, at_least, all; got {forms}")
    if not forms:
        return one(p)
    form = forms[0]
    if form == "all":
        if d["all"] is not True:
            raise BlueprintError(f"{path}.all", "write all: true")
        return _all_tokens(p)
    n = _int(f"{path}.{form}", d[form], minimum=1)
    if form == "count":
        return one(p) if n == 1 else exactly(n, p)
    return at_least(n, p)


def _out(path: str, v: Any, places: _Places) -> tuple[Out, tuple[str | None, ...] | None]:
    """The output tree, and the top-level xor's labels (``None`` if not an xor)."""
    if isinstance(v, str):
        return OutPlace(places.get(path, v)), None
    if not isinstance(v, Mapping) or len(v) == 0:
        raise BlueprintError(
            path,
            f"an output is a place, {{and: [...]}}, {{xor: ...}} or {{timeout: ms, child: ...}}, "
            f"got {v!r}",
            f"to mark several places write {{and: [{', '.join(map(str, cast(list[Any], v)))}]}}"
            if isinstance(v, list)
            else None,
        )
    v = cast(Mapping[str, Any], v)
    if "timeout" in v:
        d = _keys(path, v, ("timeout", "child"), "a timeout output")
        if "child" not in d:
            raise BlueprintError(path, "a timeout output needs 'child'")
        ms = _int(f"{path}.timeout", d["timeout"], minimum=1)
        child, inner = _out(f"{path}.child", d["child"], places)
        if inner is not None:
            raise BlueprintError(
                f"{path}.child",
                "a timeout's child cannot be an xor",
                "put the xor at the top: {xor: [done, {timeout: ms, child: late}]}",
            )
        return Timeout(ms, child), None
    if len(v) != 1:
        raise BlueprintError(path, f"an output has one key (and, xor, timeout), got {list(v)}")
    ((key, body),) = v.items()
    if key == "and":
        items = _list(f"{path}.and", body)
        if not items:
            raise BlueprintError(f"{path}.and", "an and needs at least one output")
        return And(tuple(_out(f"{path}.and[{i}]", c, places)[0] for i, c in enumerate(items))), None
    if key == "xor":
        if isinstance(body, Mapping):
            labelled = cast(Mapping[str, Any], body)
            if len(labelled) < 2:
                raise BlueprintError(f"{path}.xor", "an xor needs at least two branches")
            children: list[Out] = []
            labels: list[str | None] = []
            for label, c in labelled.items():
                if not isinstance(label, str):
                    raise BlueprintError(f"{path}.xor", f"route label {label!r} is not a string")
                child, _ = _out(f"{path}.xor.{label}", c, places)
                children.append(child)
                labels.append(label)
            return Xor(tuple(children)), tuple(labels)
        items = _list(f"{path}.xor", body)
        if len(items) < 2:
            raise BlueprintError(f"{path}.xor", "an xor needs at least two branches")
        children = []
        list_labels: list[str | None] = []
        for i, c in enumerate(items):
            child, _ = _out(f"{path}.xor[{i}]", c, places)
            children.append(child)
            list_labels.append(child.place.name if isinstance(child, OutPlace) else None)
        return Xor(tuple(children)), tuple(list_labels)
    raise BlueprintError(
        f"{path}.{key}", f"unknown output form {key!r}", _suggest(key, ("and", "xor", "timeout"))
    )


def _timing(path: str, v: Any) -> Timing:
    if v is None or v == "immediate":
        return Timing("immediate")
    d = _keys(path, v, ("delayed", "deadline", "exact", "window"), "a timing")
    if len(d) != 1:
        raise BlueprintError(path, "a timing has one key: delayed, deadline, exact or window")
    ((kind, ms),) = d.items()
    if kind == "window":
        if not (isinstance(ms, list) and len(ms) == 2):
            raise BlueprintError(f"{path}.window", "write window: [earliest_ms, latest_ms]")
        a = _int(f"{path}.window[0]", ms[0], minimum=0)
        b = _int(f"{path}.window[1]", ms[1], minimum=0)
        if b < a:
            raise BlueprintError(f"{path}.window", f"latest {b} is before earliest {a}")
        return window(a, b)
    n = _int(f"{path}.{kind}", ms, minimum=0)
    return {"delayed": delayed, "deadline": deadline, "exact": exact}[kind](n)


# -- transitions and their actions --------------------------------------------


def _places_of(o: Out) -> list[Place[Any]]:
    match o:
        case OutPlace(p):
            return [p]
        case And(cs) | Xor(cs):
            return [p for c in cs for p in _places_of(c)]
        case Timeout(_, c):
            return _places_of(c)
        case _Forward(_, b):
            return [b]


def _branch_places(path: str, o: Out) -> tuple[Place[Any], ...]:
    """The places an action marks when it takes ``o``: and/timeout are transparent."""
    match o:
        case OutPlace(p):
            return (p,)
        case And(cs):
            return tuple(p for i, c in enumerate(cs) for p in _branch_places(f"{path}.and[{i}]", c))
        case Timeout(_, c):
            return _branch_places(f"{path}.child", c)
        case Xor():
            raise BlueprintError(
                path,
                "a nested xor: nothing chooses its branch",
                "put the choice at the top of out, as {xor: {label: out}}",
            )
        case _Forward(_, b):
            return (b,)


def _accepts(dst: Place[Any], src: Any) -> bool:
    t = dst.token_type
    if t is object or t is src or t == src:
        return True
    return isinstance(t, type) and isinstance(src, type) and issubclass(src, t)


def _transition(
    path: str,
    tname: str,
    decl: Any,
    places: _Places,
    nodes: Mapping[str, Any],
    net: str,
) -> tuple[TransitionSpec, ActionPlan]:
    d = _keys(path, decl, _TRANSITION_KEYS, f"transition {tname!r}")
    raw_in = d.get("in")
    in_items = [raw_in] if isinstance(raw_in, str | Mapping) else _list(f"{path}.in", raw_in)
    inputs = tuple(_in(f"{path}.in[{i}]", x, places) for i, x in enumerate(in_items))
    output: Out | None = None
    labels: tuple[str | None, ...] | None = None
    if d.get("out") is not None:
        output, labels = _out(f"{path}.out", d["out"], places)
    reads = tuple(
        places.get(f"{path}.read[{i}]", n)
        for i, n in enumerate(_str_list(f"{path}.read", d.get("read")))
    )
    inhibitors = tuple(
        places.get(f"{path}.inhibit[{i}]", n)
        for i, n in enumerate(_str_list(f"{path}.inhibit", d.get("inhibit")))
    )
    resets = tuple(
        places.get(f"{path}.reset[{i}]", n)
        for i, n in enumerate(_str_list(f"{path}.reset", d.get("reset")))
    )
    priority = _int(f"{path}.priority", d["priority"]) if "priority" in d else 0
    timing = _timing(f"{path}.timing", d.get("timing"))
    if not inputs and timing.kind == "immediate":
        raise BlueprintError(
            f"{path}.in",
            "a transition with no input arc is always enabled and fires without end",
            "give it an input place",
        )
    egress = C.EVENT_OUT.name
    for key, ps in (("in", [i.place for i in inputs]), ("read", reads), ("inhibit", inhibitors)):
        if any(p.name == egress for p in ps):
            raise BlueprintError(
                f"{path}.{key}",
                "eventOut is the turn's answer, taken by the turn: no transition may consume, "
                "read or inhibit it (the proofs take each answer when the next turn starts, "
                "and an arc on it would see the answers of earlier turns)",
                "mark a place of your own next to eventOut, {and: [eventOut, answered]}, "
                "reset it when the turn starts, and test that",
            )
    spec = TransitionSpec(tname, inputs, output, reads, inhibitors, resets, priority, timing)
    plan = _plan(path, spec, labels, d, nodes, net)
    return spec, plan


def _plan(
    path: str,
    spec: TransitionSpec,
    labels: tuple[str | None, ...] | None,
    d: Mapping[str, Any],
    nodes: Mapping[str, Any],
    net: str,
) -> ActionPlan:
    if "node" in d and "action" in d:
        raise BlueprintError(
            path,
            "a transition has at most one action: node or action",
            "drop action:, and put the node's output on a place another transition emits: "
            "{in: [p], out: eventOut, action: emit}. A node's output on eventOut becomes an "
            "Event by itself",
        )
    kind: ActionKind = "move"
    node = None
    if "node" in d:
        nname = d["node"]
        node = nodes.get(nname) if isinstance(nname, str) else None
        if node is None:
            raise BlueprintError(
                f"{path}.node",
                f"unknown node {nname!r}",
                _suggest(str(nname), nodes) or "list the node under nodes: (- [file.yaml])",
            )
        kind = "node"
    elif "action" in d:
        if d["action"] not in ("emit", "move"):
            raise BlueprintError(
                f"{path}.action", f"unknown action {d['action']!r}", "use emit or move"
            )
        kind = d["action"]

    # The branches the action chooses from; timeout branches are the executor's.
    out_path = f"{path}.out"
    branches: list[Branch] = []
    if spec.output is not None:
        if isinstance(spec.output, Xor):
            assert labels is not None
            for i, (c, label) in enumerate(zip(spec.output.children, labels, strict=True)):
                if isinstance(c, Timeout):
                    continue
                where = f"{out_path}.xor.{label}" if _is_labelled(d) else f"{out_path}.xor[{i}]"
                branches.append(Branch(label, _branch_places(where, c)))
        else:
            branches.append(Branch(None, _branch_places(out_path, spec.output)))

    coloured_in = [i for i in spec.inputs if not i.place.is_unit]
    coloured_reads = tuple(p for p in spec.reads if not p.is_unit)
    if kind in ("move", "emit"):
        if len(branches) > 1:
            raise BlueprintError(
                out_path,
                f"an xor without a node: nothing chooses its branch ({kind})",
                "name a node: that decides by its route, or split it into one transition "
                "per branch",
            )
        outs = branches[0].places if branches else ()
        coloured_out = [p for p in outs if not p.is_unit]
        if (kind == "emit" or coloured_out) and (
            len(coloured_in) != 1 or coloured_in[0].kind != "one"
        ):
            raise BlueprintError(
                f"{path}.in",
                f"{kind} forwards one coloured token, but this transition consumes "
                f"{[f'{i.place.name} ({i.kind})' for i in coloured_in] or 'none'}",
                "consume exactly one token of one coloured place, or run a node"
                + (
                    " (a count, at_least or all arc hands a list: run a node to fold it)"
                    if any(i.kind != "one" for i in coloured_in)
                    else ""
                ),
            )
        src = coloured_in[0].place.token_type if coloured_in else None
        for p in coloured_out:
            want = Event if kind == "emit" else src
            if not _accepts(p, want):
                raise BlueprintError(
                    out_path,
                    f"place {p.name!r} takes {p.type_name}, but {kind} puts "
                    f"{getattr(want, '__name__', want)} there",
                    "give the places one type, or run a node to convert",
                )
        return ActionPlan(spec.name, kind, spec.inputs, (), tuple(branches), author=net)

    # A node: its route picks a labelled branch; a failure takes `error`.
    routes: dict[str, int] = {}
    default: int | None = None
    error: int | None = None
    for i, b in enumerate(branches):
        if b.label == "error":
            error = i
            for p in b.places:
                if not p.is_unit and not _accepts(p, NodeError):
                    raise BlueprintError(
                        f"{out_path}.xor.error",
                        f"place {p.name!r} takes {p.type_name}; the error branch carries a "
                        "NodeError",
                        f"declare {p.name} with type: NodeError, or make it a unit place",
                    )
        elif b.label == "default":
            default = i
        elif b.label is not None:
            routes[b.label] = i
    if len(branches) > 1 and any(b.label is None for b in branches):
        raise BlueprintError(
            out_path,
            "an xor branch that is not a single place has no route label",
            "label every branch: {xor: {route_a: out_a, route_b: out_b, default: ...}}",
        )
    return ActionPlan(
        spec.name,
        "node",
        spec.inputs,
        coloured_reads,
        tuple(branches),
        routes,
        default,
        error,
        node,
        net,
    )


def _is_labelled(d: Mapping[str, Any]) -> bool:
    o = d.get("out")
    return isinstance(o, Mapping) and isinstance(o.get("xor"), Mapping)


# -- ports --------------------------------------------------------------------


def _ports(raw: Any, spec: NetSpec, places: _Places) -> tuple[Port, ...]:
    if raw is None:
        defaults: list[Port] = []
        if spec.has_place(C.USER_IN):
            defaults.append(
                Port(C.USER_IN.name, "in", cast(Place[Any], spec.place_named("userIn")))
            )
        if spec.has_place(C.EVENT_OUT):
            defaults.append(
                Port(C.EVENT_OUT.name, "out", cast(Place[Any], spec.place_named("eventOut")))
            )
        return tuple(defaults)
    ports: list[Port] = []
    for pname, decl in _keys("ports", raw, _AnyKeys(), "ports").items():
        path = f"ports.{pname}"
        _name(path, pname, "port")
        d = _keys(path, decl, ("place", "direction"), f"port {pname!r}")
        direction = d.get("direction")
        if direction not in ("in", "out", "inout"):
            raise BlueprintError(
                f"{path}.direction", f"expected in, out or inout, got {direction!r}"
            )
        place_name = d.get("place", pname)
        if not spec.has_place(place_name if isinstance(place_name, str) else ""):
            raise BlueprintError(
                f"{path}.place",
                f"port {pname!r} names place {place_name!r}, which no transition uses",
                _suggest(str(place_name), [p.name for p in spec.places]),
            )
        ports.append(Port(pname, direction, cast(Place[Any], spec.place_named(place_name))))
    return tuple(ports)


# -- subnets ------------------------------------------------------------------


def _rename_place(p: Place[Any], m: Mapping[str, str]) -> Place[Any]:
    return Place(m[p.name], p.token_type) if p.name in m else p


def _rename_out(o: Out, m: Mapping[str, str]) -> Out:
    match o:
        case OutPlace(p):
            return OutPlace(_rename_place(p, m))
        case And(cs):
            return And(tuple(_rename_out(c, m) for c in cs))
        case Xor(cs):
            return Xor(tuple(_rename_out(c, m) for c in cs))
        case Timeout(ms, c):
            return Timeout(ms, _rename_out(c, m))
        case _Forward(a, b):
            return _Forward(_rename_place(a, m), _rename_place(b, m))


def rename_spec(spec: NetSpec, name: str, places: Mapping[str, str], prefix: str) -> NetSpec:
    """``spec`` with places renamed by ``places`` and transitions prefixed ``prefix/``.

    Ports are dropped: a mounted subnet's interface is its binding.
    """

    def rp(p: Place[Any]) -> Place[Any]:
        return _rename_place(p, places)

    ts = tuple(
        replace(
            t,
            name=f"{prefix}/{t.name}",
            inputs=tuple(In(i.kind, rp(i.place), i.count) for i in t.inputs),
            output=None if t.output is None else _rename_out(t.output, places),
            reads=tuple(rp(p) for p in t.reads),
            inhibitors=tuple(rp(p) for p in t.inhibitors),
            resets=tuple(rp(p) for p in t.resets),
            match=None
            if t.match is None
            else Match(
                tuple((rp(p), k) for p, k in t.match.keys),
                tuple((rp(p), k) for p, k in t.match.relay_to),
            ),
        )
        for t in spec.transitions
    )
    return NetSpec(name, ts, tuple(rp(p) for p in spec.extra_places))


def _mount(
    path: str,
    inst: str,
    decl: Any,
    nodes: Mapping[str, Any],
    places: _Places,
) -> tuple[Mount, NetSpec, dict[str, tuple[Any, ...]], tuple[str, ...]]:
    d = _keys(path, decl, ("net", "stock", "from", "bind"), f"subnet {inst!r}")
    if ("net" in d) == ("stock" in d):
        raise BlueprintError(
            path,
            "a subnet is either net: <a PetriNet node> or stock: <kind>",
            f"write net: <name> or stock: one of {list(_STOCK)}",
        )
    if "net" in d:
        if "from" in d:
            raise BlueprintError(f"{path}.from", "from: configures a stock subnet only")
        child_name = d["net"]
        child = nodes.get(child_name) if isinstance(child_name, str) else None
        bp = getattr(child, "blueprint", None)
        if not isinstance(bp, Blueprint):
            raise BlueprintError(
                f"{path}.net",
                f"{child_name!r} is not a PetriNet node of this net"
                + (f" (it is a {type(child).__name__})" if child is not None else ""),
                _suggest(str(child_name), [n for n, v in nodes.items() if hasattr(v, "blueprint")])
                or "list the child blueprint under nodes: (- [child.yaml])",
            )
        spec, ports, seeds, env = bp.spec, bp.spec.ports, bp.seeds, bp.env
        source: Callable[[Any, str], Mapping[str, Action]] = bp.actions
        child_async, child_rest, net = bp.asynchronous, bp.rest, bp.name
        child_nodes = bp.node_transitions
        child_bp: Blueprint | None = bp
        agent_name: str | None = None
    else:
        spec, ports, seeds, stock_source = _stock(path, d, nodes)
        source = _ignoring_prefix(stock_source)

        env = ()
        child_async = spec.transition_names
        net = f"stock:{d['stock']}"
        child_nodes = ()
        child_bp = None
        agent_name = getattr(nodes.get(d.get("from", "")), "name", None)
        child_rest = tuple(
            p.name for p in (C.EVENT_OUT, C.TURN_PERMIT, C.TRANSFER) if spec.has_place(p)
        )

    by_port = {p.name: p for p in ports}
    bind = _keys(f"{path}.bind", d.get("bind"), _AnyKeys(), "bind")
    mapping: dict[str, str] = {}
    for port_name, parent_name in bind.items():
        bpath = f"{path}.bind.{port_name}"
        port = by_port.get(port_name)
        if port is None:
            raise BlueprintError(
                bpath,
                f"unknown port {port_name!r}",
                _suggest(str(port_name), by_port) or f"the ports are {sorted(by_port)}",
            )
        if not isinstance(parent_name, str):
            raise BlueprintError(bpath, f"bind a port to a place name, got {parent_name!r}")
        _name(bpath, parent_name, "place")
        if parent_name in places.by_name or parent_name in CATALOG:
            parent = places.get(bpath, parent_name)
            want = port.place.token_type
            if parent.token_type is not want and parent.token_type != want:
                raise BlueprintError(
                    bpath,
                    f"port {port_name!r} carries {port.place.type_name}, but place "
                    f"{parent_name!r} is {parent.type_name}",
                    "bind it to a place of the port's type, or convert with a node",
                )
        else:
            places.add(Place(parent_name, port.place.token_type), bpath)
        if port.place.name in mapping and mapping[port.place.name] != parent_name:
            raise BlueprintError(bpath, f"place {port.place.name!r} is bound twice")
        mapping[port.place.name] = parent_name
    # The runner signals the top-level turnAbort only: a child's is that one,
    # unless the child's port is bound to another place.
    abort = C.TURN_ABORT.name
    if spec.has_place(C.TURN_ABORT) and abort not in mapping:
        places.get(path, abort)
        mapping[abort] = abort
    unbound_in = sorted(
        p.name
        for p in ports
        if p.direction == "in" and p.name not in bind and p.place.name not in mapping
    )
    if unbound_in:
        raise BlueprintError(
            f"{path}.bind",
            f"in-port(s) {unbound_in} are not bound: nothing could ever reach them",
            "bind each to a place of this net: " + ", ".join(f"{p}: <place>" for p in unbound_in),
        )
    for p in spec.places:
        mapping.setdefault(p.name, f"{inst}/{p.name}")
    renamed = rename_spec(spec, inst, mapping, inst)
    bound = {port.place.name for port in ports if port.name in bind} | (
        {abort} if spec.has_place(C.TURN_ABORT) else set()
    )
    # A bound place is the parent's: its seeds are the parent's to declare.
    child_seeds = {mapping[p]: ts for p, ts in seeds.items() if p not in bound}
    # An unbound environment place stays one (under its prefix); a bound one
    # is now fed by the parent's net.
    child_env = tuple(mapping[p] for p in env if p not in bound)
    asynchronous = tuple(f"{inst}/{t}" for t in child_async)
    rest = tuple(mapping[p] for p in child_rest if mapping[p].startswith(f"{inst}/"))
    nodes_of = tuple(f"{inst}/{t}" for t in child_nodes)
    mount = Mount(inst, mapping, source, asynchronous, rest, net, nodes_of, child_bp, agent_name)
    return mount, renamed, child_seeds, child_env


def _ignoring_prefix(
    actions: Callable[[Any], Mapping[str, Action]],
) -> Callable[[Any, str], Mapping[str, Action]]:
    """A stock subnet's actions, as a mount's source (it runs no named nodes)."""

    def source(scope: Any, prefix: str = "") -> Mapping[str, Action]:
        return actions(scope)

    return source


def _stock(
    path: str, d: Mapping[str, Any], nodes: Mapping[str, Any]
) -> tuple[
    NetSpec, tuple[Port, ...], dict[str, tuple[Any, ...]], Callable[[Any], Mapping[str, Action]]
]:
    from google.adk.agents.llm_agent import LlmAgent

    from ..subnet import llm_agent, llm_step, router, tool_dispatch

    kind = d["stock"]
    if kind not in _STOCK:
        raise BlueprintError(
            f"{path}.stock", f"unknown stock subnet {kind!r}", _suggest(str(kind), _STOCK)
        )
    agent: LlmAgent | None = None
    if "from" in d:
        node = nodes.get(d["from"]) if isinstance(d["from"], str) else None
        if not isinstance(node, LlmAgent):
            raise BlueprintError(
                f"{path}.from",
                f"{d['from']!r} is not an LlmAgent node of this net"
                + (f" (it is a {type(node).__name__})" if node is not None else ""),
                "list the agent under nodes: (- [agent.yaml]) and name it here",
            )
        agent = node
    elif kind in ("llm_agent", "llm_step"):
        raise BlueprintError(
            path, f"stock {kind} takes its model from an LlmAgent", "add from: <LlmAgent node>"
        )
    if agent is not None and kind in ("llm_agent", "llm_step"):
        _check_model(f"{path}.from", agent)
    tools = _tools(f"{path}.from", agent) if agent is not None else {}
    if agent is not None and kind == "llm_agent" and not isinstance(agent.instruction, str):
        raise BlueprintError(
            f"{path}.from",
            f"agent {agent.name!r} has an instruction provider; stock llm_agent takes a string",
            "give the agent a string instruction",
        )

    seeds: dict[str, tuple[Any, ...]] = {}
    if kind == "llm_agent":
        assert agent is not None
        a = agent

        def agent_actions(_scope: Any) -> Mapping[str, Action]:
            llm = _llm(a)
            config = llm_agent.Config(
                name=a.name,
                model=llm.model,
                system_instruction=cast(str, a.instruction) or None,
                tools=tools,
            )
            return llm_agent.action_bindings(llm, config)

        seeds[C.TURN_PERMIT.name] = (None,)
        return llm_agent.DEF, llm_agent.DEF.ports, seeds, agent_actions
    if kind == "llm_step":
        assert agent is not None
        a = agent
        return llm_step.DEF, llm_step.DEF.ports, seeds, lambda _s: llm_step.action_bindings(_llm(a))
    if kind == "tool_dispatch":
        return (
            tool_dispatch.DEF,
            tool_dispatch.DEF.ports,
            seeds,
            lambda _s: tool_dispatch.action_bindings(tools),
        )
    author = agent.name if agent is not None else path.rsplit(".", 1)[-1]
    return (
        router.DEF,
        router.DEF.ports,
        seeds,
        lambda _s: router.action_bindings(router.Config(author)),
    )


def _check_model(path: str, agent: Any) -> None:
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.registry import LLMRegistry

    model = agent.model
    if isinstance(model, BaseLlm):
        return
    if not model:
        raise BlueprintError(
            path, f"agent {agent.name!r} names no model", "set model: on the LlmAgent"
        )
    try:
        LLMRegistry.resolve(model)
    except ValueError as err:
        raise BlueprintError(path, f"agent {agent.name!r}: {err}") from err


def _llm(agent: Any) -> Any:
    """The agent's model now (a session's runner resolves it when it starts)."""
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.registry import LLMRegistry

    model = agent.model
    return model if isinstance(model, BaseLlm) else LLMRegistry.new_llm(model)


def _tools(path: str, agent: Any) -> dict[str, Any]:
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.function_tool import FunctionTool

    tools: dict[str, Any] = {}
    for t in agent.tools:
        if isinstance(t, BaseTool):
            tool = t
        elif callable(t) and not inspect.isclass(t):
            tool = FunctionTool(t)
        else:
            raise BlueprintError(
                path,
                f"agent {agent.name!r}: tool {type(t).__name__} is not supported by the stock "
                "subnets (toolsets are not)",
                "list plain tools or functions",
            )
        tools[tool.name] = tool
    return tools


# -- proofs -------------------------------------------------------------------

_OPTION_KEYS = (
    "initial_marking",
    "environment",
    "sinks",
    "sinks_when",
    "assume_atomic_firing",
    "assume_atomic_nodes",
)
_CLAIMS: tuple[ClaimKind, ...] = (
    "deadlock_free",
    "place_bound",
    "unreachable",
    "mutual_exclusion",
)


class _Names:
    """The composed net's places, for claims: did-you-mean, and subnet-prefix hints."""

    def __init__(self, spec: NetSpec, mounts: Sequence[Mount]) -> None:
        self.spec = spec
        self.mounts = {m.prefix: m for m in mounts}

    def known(self, path: str, name: Any) -> str:
        if isinstance(name, str) and self.spec.has_place(name):
            return name
        raise BlueprintError(path, f"unknown place {name!r}", self._hint(str(name)))

    def _hint(self, name: str) -> str | None:
        head, sep, tail = name.partition("/")
        if sep:
            m = self.mounts.get(head)
            if m is not None and tail in m.places and not m.places[tail].startswith(head + "/"):
                return f"port {tail!r} of {head!r} is bound to {m.places[tail]!r}: name that place"
            if m is None:
                insts = [i for i, mm in self.mounts.items() if mm.net == head]
                if insts:
                    return (
                        f"{head!r} is the child net's name; its places are named by instance: "
                        + ", ".join(f"{i}/{tail}" for i in insts)
                    )
        return _suggest(name, [p.name for p in self.spec.places])


def _mode_yaml(mode: EnvMode) -> str:
    kind, lo, hi = mode
    if kind == "always":
        return "always"
    if kind == "bounded":
        return f"{{bounded: {hi}}}"
    return f"{{arrivals: {hi}}}" if lo == 0 else f"{{arrivals: [{lo}, {hi}]}}"


def _env_mode(path: str, v: Any) -> EnvMode:
    if v == "always":
        return ("always", 0, 0)
    d = _keys(path, v, ("arrivals", "bounded"), "an environment mode")
    if len(d) != 1:
        raise BlueprintError(
            path, "write always, {arrivals: k}, {arrivals: [min, max]} or {bounded: k}"
        )
    ((kind, n),) = d.items()
    if kind == "bounded":
        k = _int(f"{path}.bounded", n, minimum=1)
        return ("bounded", 0, k)
    if isinstance(n, list):
        if len(n) != 2:
            raise BlueprintError(f"{path}.arrivals", "write arrivals: [min, max]")
        lo = _int(f"{path}.arrivals[0]", n[0], minimum=0)
        hi = _int(f"{path}.arrivals[1]", n[1], minimum=lo)
        return ("arrivals", lo, hi)
    return ("arrivals", 0, _int(f"{path}.arrivals", n, minimum=0))


def _bool(path: str, d: Mapping[str, Any], key: str) -> bool | None:
    if key not in d:
        return None
    if not isinstance(d[key], bool):
        raise BlueprintError(f"{path}.{key}", "expected true or false")
    return d[key]


def _options(path: str, raw: Any, names: _Names) -> ProofOptions:
    d = _keys(path, raw, _OPTION_KEYS, "proof options")
    initial = None
    if "initial_marking" in d:
        m = _keys(f"{path}.initial_marking", d["initial_marking"], _AnyKeys(), "a marking")
        initial = {
            names.known(f"{path}.initial_marking.{p}", p): _int(
                f"{path}.initial_marking.{p}", n, minimum=0
            )
            for p, n in m.items()
        }
    env = mode = None
    if "environment" in d:
        e = _keys(f"{path}.environment", d["environment"], _AnyKeys(), "an environment")
        modes = {
            names.known(f"{path}.environment.{p}", p): _env_mode(f"{path}.environment.{p}", v)
            for p, v in e.items()
        }
        if len(set(modes.values())) > 1:
            shown = ", ".join(f"{p}: {_mode_yaml(m)}" for p, m in modes.items())
            raise BlueprintError(
                f"{path}.environment",
                f"libpetri models every environment place under one mode, got {shown}",
                "give every place the same mode; for an optional input (an approval, a "
                "webhook) {arrivals: k} lets it come at most k times and also lets userIn not "
                "come, {arrivals: [k, k]} makes every place come exactly k times",
            )
        env = tuple(modes)
        mode = next(iter(modes.values()), None)
    sinks = None
    if "sinks" in d:
        sinks = tuple(
            names.known(f"{path}.sinks[{i}]", p)
            for i, p in enumerate(_str_list(f"{path}.sinks", d["sinks"]))
        )
    when = None
    if "sinks_when" in d:
        w = _keys(f"{path}.sinks_when", d["sinks_when"], _AnyKeys(), "sinks_when")
        when = {
            names.known(f"{path}.sinks_when.{m}", m): tuple(
                names.known(f"{path}.sinks_when.{m}[{i}]", p)
                for i, p in enumerate(_str_list(f"{path}.sinks_when.{m}", ps))
            )
            for m, ps in w.items()
        }
    atomic = _bool(path, d, "assume_atomic_firing")
    atomic_nodes = _bool(path, d, "assume_atomic_nodes")
    return ProofOptions(initial, env, mode, sinks, when, atomic, atomic_nodes)


def _claim(path: str, raw: Any, names: _Names) -> Claim:
    if raw == "deadlock_free":
        return Claim("deadlock_free", "deadlock_free")
    if not isinstance(raw, Mapping):
        raise BlueprintError(
            path, f"a claim is deadlock_free or {{kind: ...}}, got {raw!r}", f"kinds: {_CLAIMS}"
        )
    raw = cast(Mapping[str, Any], raw)
    kinds = [k for k in raw if k in _CLAIMS]
    extra = [k for k in raw if k not in (*_CLAIMS, "label", "options")]
    if extra:
        raise BlueprintError(
            f"{path}.{extra[0]}",
            f"unknown claim key {extra[0]!r}",
            _suggest(str(extra[0]), (*_CLAIMS, "label", "options")),
        )
    if len(kinds) != 1:
        raise BlueprintError(path, f"a claim names exactly one of {list(_CLAIMS)}, got {kinds}")
    kind = kinds[0]
    body = raw[kind]
    options = _options(f"{path}.options", raw.get("options"), names)
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        raise BlueprintError(f"{path}.label", "a label is a string")
    kp = f"{path}.{kind}"
    if kind == "deadlock_free":
        if body not in (None, {}, True):
            raise BlueprintError(kp, "deadlock_free takes no arguments")
        return Claim(kind, label or "deadlock_free", options=options)
    if kind == "place_bound":
        b = _keys(kp, body, ("place", "bound"), "place_bound")
        if "place" not in b or "bound" not in b:
            raise BlueprintError(kp, "write place_bound: {place: p, bound: n}")
        p = names.known(f"{kp}.place", b["place"])
        n = _int(f"{kp}.bound", b["bound"], minimum=0)
        return Claim(kind, label or f"place_bound({p}, {n})", (p,), n, options)
    ps = tuple(names.known(f"{kp}[{i}]", p) for i, p in enumerate(_str_list(kp, body)))
    if not ps or (kind == "mutual_exclusion" and len(ps) < 2):
        need = "two places" if kind == "mutual_exclusion" else "a place"
        raise BlueprintError(kp, f"{kind} needs {need}")
    return Claim(cast(ClaimKind, kind), label or f"{kind}({list(ps)})", ps, 0, options)


def _check_claim(
    i: int,
    claim: Claim,
    shared: ProofOptions,
    env: tuple[str, ...],
    asynchronous: tuple[str, ...],
) -> None:
    """What a claim's options leave out of the run they prove, as a load error."""
    o = claim.options.over(shared)

    def where(key: str) -> str:
        own = getattr(claim.options, key) is not None
        return f"prove.claims[{i}].options.{key}" if own else f"prove.options.{key}"

    if o.environment is not None:
        seeded = set(o.initial_marking or ())
        missing = [p for p in env if p not in o.environment and p not in seeded]
        if missing:
            assert o.mode is not None
            listed = ", ".join(f"{p}: {_mode_yaml(o.mode)}" for p in (*o.environment, *missing))
            raise BlueprintError(
                where("environment"),
                f"environment leaves out the net's environment place(s) {missing}: the proof "
                "would give them no token, and a claim about what they do would hold vacuously",
                f"list every environment place, in one mode: environment: {{{listed}}}",
            )
    if o.assume_atomic_firing and not o.assume_atomic_nodes and asynchronous:
        raise BlueprintError(
            where("assume_atomic_firing"),
            "assume_atomic_firing reads every firing as one step, but "
            f"{asynchronous[0]!r} runs a node (or a model or tool call), which other "
            "transitions fire during: a bound proven so need not hold at run time",
            "drop it; or, if the claim's counterexample without it needs only move and emit "
            "transitions to be atomic, add assume_atomic_nodes: true to say so",
        )


def _proof(
    raw: Any,
    spec: NetSpec,
    env: tuple[str, ...],
    asynchronous: tuple[str, ...],
    mounts: Sequence[Mount],
) -> ProofPlan:
    if raw is None:
        return ProofPlan()
    names = _Names(spec, mounts)
    d = _keys("prove", raw, ("options", "claims", "on_load"), "prove")
    options = _options("prove.options", d.get("options"), names)
    claims = tuple(
        _claim(f"prove.claims[{i}]", c, names)
        for i, c in enumerate(_list("prove.claims", d.get("claims")))
    )
    for i, c in enumerate(claims):
        _check_claim(i, c, options, env, asynchronous)
    on_load = d.get("on_load", False)
    if not isinstance(on_load, bool):
        raise BlueprintError("prove.on_load", "expected true or false")
    if on_load and not claims:
        raise BlueprintError("prove.on_load", "on_load proves the claims, but none are listed")
    return ProofPlan(claims, options, on_load)


# ----------------------------------------------------------------------------
#  Actions
# ----------------------------------------------------------------------------


class _RenamedContext:
    """A transition context that maps a mounted subnet's place names to the parent's."""

    __slots__ = ("_c", "_m")

    def __init__(self, c: Any, m: Mapping[str, str]) -> None:
        self._c = c
        self._m = m

    @property
    def transition_name(self) -> str:
        return self._c.transition_name

    def fresh_name(self) -> str:
        return self._c.fresh_name()

    def input(self, name: str) -> Any:
        return self._c.input(self._m.get(name, name))

    def inputs(self, name: str) -> list[Any]:
        return self._c.inputs(self._m.get(name, name))

    def read(self, name: str) -> Any:
        return self._c.read(self._m.get(name, name))

    def reads(self, name: str) -> list[Any]:
        return self._c.reads(self._m.get(name, name))

    def output(self, name: str, value: Any) -> None:
        self._c.output(self._m.get(name, name), value)

    def output_many(self, name: str, values: Iterable[Any]) -> None:
        self._c.output_many(self._m.get(name, name), values)

    def flush(self) -> None:
        self._c.flush()


def _renamed(action: Action, places: Mapping[str, str]) -> Action:
    if inspect.iscoroutinefunction(action):
        inner_async = cast(Callable[[Ctx], Any], action)

        async def run_async(ctx: Ctx) -> None:
            await inner_async(Ctx(cast(Any, _RenamedContext(ctx.raw, places))))

        return run_async

    inner = cast(Callable[[Ctx], Any], action)

    def run(ctx: Ctx) -> None:
        inner(Ctx(cast(Any, _RenamedContext(ctx.raw, places))))

    return run


def _take(ctx: Ctx, plan: ActionPlan, *, with_reads: bool) -> Any:
    """Consume the inputs; the coloured value (one value, or ``{place: value}``)."""
    values: dict[str, Any] = {}
    for i in plan.takes:
        v = ctx.input(i.place) if i.kind == "one" else ctx.inputs(i.place)
        if not i.place.is_unit:
            values[i.place.name] = v
    if with_reads:
        for p in plan.reads:
            values[p.name] = ctx.read(p)
    if not values:
        return None
    if len(values) == 1:
        return next(iter(values.values()))
    return values


def _mark(ctx: Ctx, branch: Branch, value: Any) -> None:
    for p in branch.places:
        if p.is_unit:
            ctx.signal(p)
        else:
            ctx.output(p, value)


def to_event(value: Any, author: str) -> Event:
    """``emit``: an ``Event`` as is, ``Content`` as content, text as a model message,
    anything else as the event's ``output``.

    A ``str`` or ``Content`` is the event's ``output`` as well, as ADK's own
    nodes set it: a ``Workflow`` hands the next node only the ``output``.
    """
    if isinstance(value, Event):
        return value
    if isinstance(value, types.Content):
        return Event(author=author, content=value, output=value)
    if isinstance(value, str):
        return Event(
            author=author,
            content=types.Content(role="model", parts=[types.Part(text=value)]),
            output=value,
        )
    return Event(author=author, output=value)


def _type_label(t: Any) -> str:
    return getattr(t, "__name__", None) or str(t)


def _node_value(plan: ActionPlan, place: Place[Any], value: Any) -> Any:
    """The token a node's output puts on ``place``, or :class:`NetRunError`.

    An ``Event`` place gets the output as ``emit`` makes it (so a node can
    answer the turn itself); a pydantic model, which ADK hands on dumped, is
    rebuilt; anything else must be of the place's type.
    """
    from pydantic import BaseModel

    name = plan.node.name
    t = place.token_type
    if value is None:
        raise NetRunError(
            f"node {name!r} of transition {plan.transition!r} gave no output for place "
            f"{place.name!r} ({place.type_name}). A node that returns a types.Content "
            "hands ADK content but no output: return Event(output=content), or the value"
        )
    if t is Event:
        return to_event(value, plan.author)
    if not isinstance(t, type) or t is object:
        return value
    if issubclass(t, BaseModel) and isinstance(value, Mapping) and not isinstance(value, t):
        return t.model_validate(value)
    if not isinstance(value, t):
        raise NetRunError(
            f"node {name!r} of transition {plan.transition!r} gave a "
            f"{type(value).__name__}, but place {place.name!r} takes {place.type_name}"
        )
    return value


def run_name(prefix: str, node: Any) -> str:
    """The name a ``node:`` transition's node runs under, mounted at ``prefix``.

    A function node mounted in a subnet runs as ``<prefix>·<name>``
    (``second·fast``; a nested mount ``first·inner·leaf``): its event path
    then says which mount ran it, in ADK's Events tab and for the dev UI's
    graph, where two mounts of one blueprint would otherwise be told apart by
    nothing. Any other node (an agent, a workflow, a net) keeps its own name.
    """
    from google.adk.workflow import FunctionNode

    name = str(getattr(node, "name", ""))
    if not prefix or not isinstance(node, FunctionNode):
        return name
    parts = [re.sub(r"\W", "_", part) for part in prefix.split("/") if part]
    return "·".join([*parts, name])


def _plan_action(plan: ActionPlan, scope: NetScope, prefix: str = "") -> Action:
    if plan.kind == "move":

        def move(ctx: Ctx) -> None:
            value = _take(ctx, plan, with_reads=False)
            if plan.branches:
                _mark(ctx, plan.branches[0], value)

        return move

    if plan.kind == "emit":

        def emit(ctx: Ctx) -> None:
            value = _take(ctx, plan, with_reads=False)
            if plan.branches:
                _mark(ctx, plan.branches[0], to_event(value, plan.author))

        return emit

    return _node_action(plan, scope, prefix)


def _node_action(plan: ActionPlan, scope: NetScope, prefix: str = "") -> Action:
    from ..workflow.compiler import _error_code, _run_node
    from ..workflow.tokens import WfToken, WorkflowFailure

    node = plan.node
    name = node.name
    run_as = run_name(prefix, node)
    if run_as != name:
        node = node.model_copy(update={"name": run_as})

    def fail(failure: WorkflowFailure, turn: int) -> NoReturn:
        scope.record_failure(turn, TurnResult("failed", failure=failure))
        raise NetRunError(f"transition {plan.transition!r}: {failure.message}")

    async def run(ctx: Ctx) -> None:
        value = _take(ctx, plan, with_reads=True)
        begun = scope.begin_node(plan.transition)
        # No turn open: the tokens stay in flight until the next turn runs the node.
        turn = begun if isinstance(begun, int) else await on_future(begun)
        loop = scope.loop
        assert loop is not None
        outcome = await on_loop(
            scope.tracked(_run_node(scope, node, WfToken(value), scope.next_run_id(run_as), False)),
            loop=loop,
        )
        err: BaseException | None = outcome.error
        if err is None and outcome.interrupts:
            err = NetRunError(
                f"node {name!r} requested input {list(outcome.interrupts)}; a PetriNet "
                "node cannot interrupt"
            )
        index: int | None = None
        tokens: list[tuple[Place[Any], Any]] = []
        if err is None:
            index = _route(plan, outcome.route)
            if index is None:
                msg = (
                    f"node {name!r} routed {outcome.route!r}, which matches no branch of "
                    f"{plan.transition!r} ({sorted(plan.routes)}) and there is no default"
                )
                fail(WorkflowFailure(run_as, "NetRunError", msg, NetRunError(msg)), turn)
            if plan.branches:
                try:
                    tokens = [
                        (p, None if p.is_unit else _node_value(plan, p, outcome.output))
                        for p in plan.branches[index].places
                    ]
                except Exception as bad:  # a wrong output is the node's error
                    err = bad
        if err is not None:
            code = _error_code(err)
            if plan.error is not None:
                _mark(ctx, plan.branches[plan.error], NodeError(name, code, str(err)))
                return
            fail(
                WorkflowFailure(
                    run_as,
                    code,
                    str(err),
                    err,
                    outcome.error_node_path,
                    from_node=outcome.error is not None,
                ),
                turn,
            )
        for p, token in tokens:
            if p.is_unit:
                ctx.signal(p)
            else:
                ctx.output(p, token)

    return run


def _route(plan: ActionPlan, route: Any) -> int | None:
    """The branch a node's route picks: the only one, a labelled one, or ``default``."""
    if not plan.branches:
        return 0
    choosable = [i for i in range(len(plan.branches)) if i != plan.error]
    if len(choosable) == 1:
        return choosable[0]
    for r in route if isinstance(route, list) else [route]:
        if isinstance(r, str) and r in plan.routes:
            return plan.routes[r]
    return plan.default
