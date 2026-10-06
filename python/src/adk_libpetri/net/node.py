"""``PetriNet``: a net blueprint written in ADK's YAML, served as an ADK node (``@experimental``).

::

    # root_agent.yaml
    agent_class: adk_libpetri.net.PetriNet
    name: speculative_answer
    nodes:                       # ADK node refs, resolved by ADK's loader
      - [.agent.fast_answer]
      - [.agent.slow_answer]
    places:
      goFast: {}
      goSlow: {}
      draft:  {type: str}
      permit: {}
      won:    {}
      late:   {type: str}
    transitions:
      Race_Start:
        in: [userIn]
        out: {and: [goFast, goSlow, permit]}
        reset: [goFast, goSlow, draft, permit, won, late]
      Race_RunFast: {in: [goFast], out: draft, inhibit: [won], node: fast_answer}
      Race_RunSlow: {in: [goSlow], out: draft, inhibit: [won], node: slow_answer}
      Race_Commit:  {in: [draft, permit], out: {and: [eventOut, won]}, priority: 10, action: emit}
      Race_Late:    {in: [draft], out: late, read: [won], priority: -10}
    prove:
      options: {sinks: [eventOut, won, late], sinks_when: {won: [goFast, goSlow]}}
      claims: [deadlock_free, {place_bound: {place: won, bound: 1}}]

``adk web`` and ``adk run`` load it through ADK's loader, which resolves
``nodes:`` (a field typed as a ``Workflow``'s edges, the one place ADK resolves
code and file references relative to the YAML), then the node parses the rest
with :func:`~adk_libpetri.net.blueprint.parse_blueprint` and starts its
session nets on :meth:`OrchestratorLoop.shared`.

**The turn** follows ``PetriAgent``'s protocol: the invocation's input (the
node input, a ``str`` made a user ``Content`` when ``userIn`` takes one, else
the user's message) is injected on ``userIn``; the first non-partial token on
``eventOut`` ends the turn. An ``Event`` token is yielded as this node's
event; any other value becomes the node's output. A ``node:`` transition runs
its ADK node inside the turn's invocation; a node that fails with no
``error`` branch fails this node as it fails a ``Workflow``. A session's net
serves one turn at a time; a node transition that fires between turns runs
in the next turn's invocation.

**Leading-dot types** (``type: .agent.Draft``) resolve against the package of
the YAML file being loaded (its directory name, ADK's rule for ``.agent.fn``
node refs). The node finds that file on ADK's loader frames; a ``PetriNet``
built in Python has none, and needs fully qualified type names.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncGenerator, Mapping
from typing import Any, ClassVar, cast

from google.adk.events.event import Event
from google.adk.workflow import BaseNode
from google.adk.workflow._graph import EdgeItem
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .. import colours as C
from .._aio import HotStream, OrchestratorLoop
from .._experimental import experimental
from .._net_node import SCOPE_ATTACHMENT, NetNodeBase
from ..bridge import TransitionFailure
from ..runner.petri_runner import Builder, PetriRunner
from ..runner.session_registry import SessionExecutorRegistry
from ..workflow.compiler import TurnScope
from .blueprint import (
    TOP_KEYS,
    Blueprint,
    BlueprintError,
    NetRunError,
    NetScope,
    package_of,
    parse_blueprint,
)
from .proofs import NetProof, verify_blueprint

_LOADER = "google.adk.agents.config_agent_utils"


# ----------------------------------------------------------------------------
#  Where ADK's loader is (the YAML file a node is being built from)
# ----------------------------------------------------------------------------


def _loader_paths() -> list[str]:
    """The YAML files ADK's loader is building, innermost first.

    Read off the loader's own frames: ``from_config`` holds ``abs_path``, and
    an inline node is built by the mapper of its file. The walk stops at an
    import: a node a Python module builds while ADK imports it is no YAML's.
    """
    paths: list[str] = []
    f = sys._getframe(1)
    while f is not None:
        if f.f_code.co_filename.startswith("<frozen importlib"):
            break
        if f.f_globals.get("__name__") == _LOADER:
            loc = f.f_locals
            path = loc.get("abs_path") if f.f_code.co_name == "from_config" else None
            if path is None and f.f_code.co_name in ("_build", "_resolve_node_like", "map"):
                path = getattr(loc.get("self"), "abs_path", None)
            if isinstance(path, str) and (not paths or paths[-1] != path):
                paths.append(path)
        f = f.f_back
    return paths


def _nearest_source() -> str | None:
    paths = _loader_paths()
    return paths[0] if paths else None


class _PetriNetConfig(BaseModel):
    """ADK's loader validates a file against this before it resolves ``nodes:``.

    It rejects keys ``PetriNet`` does not have (ADK would only log them) and a
    file that refers back to itself through ``nodes:`` (ADK would recurse
    until the stack runs out).
    """

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _check(cls, data: Any) -> Any:
        paths = _loader_paths()
        source = paths[0] if paths else None
        if paths and paths[0] in paths[1:]:
            chain = list(reversed(paths[: paths.index(paths[0], 1) + 1]))
            raise BlueprintError(
                "nodes",
                "ref cycle: " + " -> ".join(os.path.basename(p) for p in chain),
                "a blueprint cannot mount itself, directly or through its children",
                source,
            )
        if isinstance(data, Mapping):
            _check_node_entries(cast(Mapping[str, Any], data).get("nodes"), source)
            fields = PetriNet.model_fields
            for key in data:
                k = str(key)
                base = k.removesuffix("_code") if k.endswith("_code") else k
                if k == "agent_class" or base in fields:
                    continue
                import difflib

                close = difflib.get_close_matches(k, [*fields, *TOP_KEYS], n=1)
                raise BlueprintError(
                    k,
                    f"unknown key {k!r} for a PetriNet",
                    f"did you mean {close[0]!r}?" if close else f"the keys are {list(TOP_KEYS)}",
                    source,
                )
        return data


def _check_node_entries(items: Any, source: str | None) -> None:
    """``nodes:`` entries ADK would not resolve, before pydantic buries them in errors."""
    if not isinstance(items, list):
        return
    for i, item in enumerate(cast(list[Any], items)):
        if isinstance(item, str):
            raise BlueprintError(
                f"nodes[{i}]",
                f"a bare node ref {item!r}: ADK resolves a nodes: entry only inside a list",
                f"write - [{item}]",
                source,
            )
        if isinstance(item, Mapping):
            cls = cast(Mapping[str, Any], item).get("agent_class", "...")
            raise BlueprintError(
                f"nodes[{i}]",
                "an inline node goes inside a list: ADK resolves a nodes: entry only there",
                f"write - [{{agent_class: {cls}, ...}}], or put the node in its own file "
                "and write - [file.yaml]",
                source,
            )


# ----------------------------------------------------------------------------
#  The node
# ----------------------------------------------------------------------------


class _EgressTap:
    """Event-store link that hands every ``EVENT_OUT`` token (``Event`` or not) to the turn."""

    captures_tokens = True

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.stream: HotStream[Any] = HotStream()

    def is_enabled(self) -> bool:
        return True

    def append(self, event: Any) -> None:
        t = event.type
        if t == "TokenAdded" and event.place_name == C.EVENT_OUT.name:
            self.stream.publish(getattr(event, "token", None))
        elif t == "ExecutionCompleted":
            self.stream.complete()
        if self._delegate is not None:
            self._delegate.append(event)

    def events(self, **filters: Any) -> list[Any]:
        return self._delegate.events(**filters) if self._delegate is not None else []


_NOTHING: Any = object()


@experimental
class PetriNet(NetNodeBase):
    """A Petri-net blueprint as an ADK node; see the module docstring for the format."""

    config_type: ClassVar[type[BaseModel]] = _PetriNetConfig
    rerun_on_resume: bool = True
    """ADK runs a node's children dynamically only under a node that reruns on resume."""

    nodes: list[EdgeItem] = Field(default_factory=list)
    """ADK nodes the transitions (``node:``) and subnets (``net:``, ``from:``)
    name. Typed as a ``Workflow``'s edges so ADK's loader resolves each entry
    (``- [agent.yaml]``, ``- [.agent.fn]``, an inline node) relative to the file."""
    places: dict[str, Any] = Field(default_factory=dict)
    transitions: dict[str, Any] = Field(default_factory=dict)
    env: list[str] = Field(default_factory=list)
    ports: dict[str, Any] | None = None
    subnets: dict[str, Any] = Field(default_factory=dict)
    prove: dict[str, Any] | None = None

    _blueprint: Blueprint = PrivateAttr()

    def model_post_init(self, context: Any, /) -> None:
        super().model_post_init(context)
        source = _nearest_source()
        nodes = _named_nodes(self.nodes, source)
        data: dict[str, Any] = {
            "places": self.places,
            "transitions": self.transitions,
            "env": self.env,
            "subnets": self.subnets,
            "prove": self.prove,
        }
        if self.ports is not None:
            data["ports"] = self.ports
        self._blueprint = parse_blueprint(
            self.name, data, nodes=nodes, package=package_of(source), source=source
        )
        if self._blueprint.proof.on_load:
            failed = [p for p in self.verify() if not p.proven]
            if failed:
                raise BlueprintError(
                    "prove",
                    "claims not proven: "
                    + "; ".join(f"{p.label}: {p.result.verdict}" for p in failed),
                    "fix the net, or read each claim's counterexample with PetriNet.verify()",
                    source,
                )

    @classmethod
    def from_config(
        cls,
        config_path: str,
        *,
        orchestrator: OrchestratorLoop | None = None,
        registry: SessionExecutorRegistry | None = None,
        event_store: Any = None,
    ) -> PetriNet:
        """Load a blueprint YAML through ADK's loader and serve it on ``orchestrator``."""
        from google.adk.agents.config_agent_utils import from_config

        node = from_config(config_path)
        if not isinstance(node, PetriNet):
            raise TypeError(
                f"{config_path} defines a {type(node).__name__}, not a PetriNet "
                "(agent_class: adk_libpetri.net.PetriNet)"
            )
        if orchestrator is not None:
            node._orchestrator = orchestrator
        if registry is not None:
            node._registry = registry
        node._event_store = event_store
        return node

    def serve_on(
        self,
        orchestrator: OrchestratorLoop,
        *,
        registry: SessionExecutorRegistry | None = None,
        event_store: Any = None,
    ) -> PetriNet:
        """Serve sessions on ``orchestrator`` (ADK's loader leaves the shared loop)."""
        self._serve_on(orchestrator, registry, event_store)
        return self

    @property
    def blueprint(self) -> Blueprint:
        return self._blueprint

    @property
    def spec(self) -> Any:
        return self._blueprint.spec

    def verify(self, k: int | None = None, **verify_options: Any) -> list[NetProof]:
        """Check the ``prove:`` claims, one ``libpetri.verify`` each."""
        return verify_blueprint(self._blueprint, k, **verify_options)

    # -- serving -------------------------------------------------------------

    def inject(self, session: Any, place: str, value: Any = None) -> bool:
        """Put ``value`` on ``env:`` place ``place`` of ``session``'s net (thread-safe).

        For a signal from outside the turn's input: an approval, a webhook, a
        sensor. ``session`` is the ADK ``Session`` (``ctx.session``, or the one a
        ``Runner``'s session service returns). The net starts with the
        session's first turn: before it, this raises. A turn blocks until
        ``eventOut``, so a value it waits for comes from another task, during
        the turn. ``adk web`` and ``adk run`` have no way to call this.
        Returns ``False`` once the session's net is closing.
        """
        bp = self._blueprint
        if place not in bp.env or place == C.USER_IN.name:
            raise ValueError(
                f"{place!r} is not an env: place of PetriNet {self.name!r} "
                f"(env places: {[p for p in bp.env if p != C.USER_IN.name]}); "
                "a turn's input goes through the Runner, not inject()"
            )
        runner = self._registry.get(self.session_key(session))
        if runner is None:
            raise RuntimeError(
                f"session {getattr(session, 'id', session)!r} has no net for PetriNet "
                f"{self.name!r} yet: it starts with the session's first turn"
            )
        p = bp.spec.place_named(place)
        if p is not None and p.is_unit:
            return runner.signal(place)
        return runner.inject(place, value)

    def _net_spec(self) -> Any:
        return self._blueprint.spec

    def _new_scope(self) -> TurnScope:
        return NetScope()

    async def _start_runner(self, key: Any) -> PetriRunner:
        runner = await super()._start_runner(key)
        scope = runner.attachments[SCOPE_ATTACHMENT]
        assert isinstance(scope, NetScope)
        # A node run waiting for a turn would hold the drain for ever.
        runner.on_drain(scope.close)
        return runner

    def _runner_builder(self, scope: TurnScope) -> Builder:
        assert isinstance(scope, NetScope)
        bp = self._blueprint
        tap = _EgressTap(self._event_store)
        scope.egress = tap.stream
        builder = (
            PetriRunner.builder(bp.spec, bp.actions(scope))
            .initial_marking(bp.initial_marking())
            .event_store(tap)
        )
        for p in bp.env:
            builder.environment_place(p)
        return builder

    async def _run_impl(self, *, ctx: Any, node_input: Any) -> AsyncGenerator[Any, None]:
        if ctx.resume_inputs:
            raise NetRunError(
                f"PetriNet {self.name!r} got answers to interrupts {sorted(ctx.resume_inputs)}; "
                "its nodes cannot interrupt"
            )
        ic = ctx.get_invocation_context()
        start = node_input if node_input is not None else ic.user_content
        if start is None:
            return
        start = self._user_input(start)
        runner = await self._session_runner(ctx)
        scope = runner.attachments[SCOPE_ATTACHMENT]
        assert isinstance(scope, NetScope)

        def inject() -> bool:
            return runner.inject(C.USER_IN.name, start)

        # One turn at a time: a second invocation of this session waits here.
        async with scope.turn_slot(self.name):
            # As Workflow does: child events are attributed to this node.
            ctx.event_author = self.name
            scope.open_turn(ctx, asyncio.get_running_loop())
            item = _NOTHING
            try:
                try:
                    async for x in self._turn(ctx, runner, inject, egress=scope.egress):
                        item = x
                finally:
                    scope.answered()
                if isinstance(item, Event):
                    # A copy that is ours: ADK's node runner stamps author, path and branch.
                    yield item.model_copy(update={"branch": None}, deep=True)
                elif item is not _NOTHING:
                    ctx.output = item
            except TransitionFailure:
                result = scope.result
                if result is None or result.failure is None:
                    raise
                self._fail(ctx, result.failure)
            finally:
                # The node runs this turn started run inside its invocation: let
                # them finish (a race's loser, say) before the invocation ends.
                await scope.drain()

    def _user_input(self, value: Any) -> Any:
        """The token the turn puts on ``userIn``: ``value``, of userIn's type."""
        from pydantic import BaseModel

        p = self._blueprint.spec.place_named(C.USER_IN.name)
        if p is None:
            raise NetRunError(f"PetriNet {self.name!r} has no userIn place for the turn's input")
        t = p.token_type
        if p.is_unit or not isinstance(t, type) or t is object or isinstance(value, t):
            return value
        if t is types.Content and isinstance(value, str):
            return types.Content(role="user", parts=[types.Part(text=value)])
        if issubclass(t, BaseModel) and isinstance(value, Mapping):
            return t.model_validate(value)
        raise NetRunError(
            f"PetriNet {self.name!r} got a {type(value).__name__} as its input, but its "
            f"userIn takes {p.type_name}. Fix: declare userIn: "
            f"{{type: {type(value).__name__}}} (or object), or convert the input in the node "
            "before this one"
        )


def _named_nodes(items: list[Any], source: str | None) -> dict[str, BaseNode]:
    from google.adk.workflow._base_node import START

    nodes: dict[str, BaseNode] = {}
    for i, item in enumerate(items):
        elements = item if isinstance(item, list | tuple) else (item,)
        for j, el in enumerate(elements):
            path = f"nodes[{i}]" if len(elements) == 1 else f"nodes[{i}][{j}]"
            if el is START or not isinstance(el, BaseNode):
                raise BlueprintError(
                    path,
                    f"nodes lists ADK nodes, got {el!r}",
                    "write - [file.yaml] or - [.module.function]; edges and routes belong "
                    "to transitions",
                    source,
                )
            prior = nodes.get(el.name)
            if prior is not None and prior is not el:
                raise BlueprintError(path, f"two nodes are named {el.name!r}", None, source)
            nodes[el.name] = el
    return nodes


__all__ = ["PetriNet"]
