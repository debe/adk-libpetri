"""The ADK node a session's net serves through (``PetriWorkflow``, ``PetriNet``).

Both nodes own one long-lived net per session, kept in a
:class:`~adk_libpetri.runner.SessionExecutorRegistry` under the node's
:meth:`~NetNodeBase.session_key`,
and both run every ADK node a transition names inside the current turn's
invocation, through a :class:`~adk_libpetri.workflow.compiler.TurnScope`.
This base holds that shared part:

* the session runner, started on the node's orchestrator loop the first time
  the session runs the node, with the scope attached. Its teardown route is
  the registry's ``close``/``close_all``; a node that keeps its own registry
  (the default, and every node ADK's loader builds) also closes its runners
  when the node itself is collected (a reloaded agent, say);
* the turn: point the scope at this invocation, inject, wait with
  :func:`~adk_libpetri.runner.turn.run_turn`;
* the result: a :class:`~adk_libpetri.workflow.compiler.TurnResult` mapped
  onto the node's context as ``Workflow`` leaves it (output, pending
  interrupts, or the failure).

The subclass says how its runner is built (:meth:`NetNodeBase._runner_builder`)
and what a turn injects.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import threading
import weakref
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any

from google.adk.workflow import BaseNode
from pydantic import PrivateAttr

from ._aio import HotStream, OrchestratorLoop
from .runner.petri_runner import Builder, PetriRunner
from .runner.session_key import SessionKey
from .runner.session_registry import SessionExecutorRegistry
from .runner.turn import abort_turns_on_failure, run_turn

if TYPE_CHECKING:
    from ._spec import NetSpec

    # Not at import time: the workflow package imports this module.
    from .workflow.compiler import TurnResult, TurnScope
    from .workflow.tokens import WorkflowFailure

SCOPE_ATTACHMENT = "adk_libpetri.workflow.turn_scope"


def _close_all_quietly(registry: SessionExecutorRegistry) -> None:
    """A collected node's runners: torn down, off the collecting thread.

    A finalizer runs on whatever thread collects the node, the orchestrator's
    own among them, and closing a runner waits for its run to end there.
    """

    def close() -> None:
        with contextlib.suppress(Exception):
            registry.close_all()

    threading.Thread(target=close, name="petri-net-node-collected", daemon=True).start()


class NetNodeBase(BaseNode):
    """A ``BaseNode`` served by one long-lived net per session (internal base)."""

    _registry: SessionExecutorRegistry = PrivateAttr(
        default_factory=SessionExecutorRegistry.strong_owned
    )
    _orchestrator: OrchestratorLoop | None = PrivateAttr(default=None)
    _event_store: Any = PrivateAttr(default=None)
    _finalizer: weakref.finalize[Any, Any] | None = PrivateAttr(default=None)

    def model_post_init(self, context: Any, /) -> None:
        super().model_post_init(context)
        self._own_registry(self._registry)

    def _own_registry(self, registry: SessionExecutorRegistry) -> None:
        """Close ``registry``'s runners when this node is collected.

        Bound to this instance, not to the copies ADK makes of a node for each
        run (they share its private attributes, this finalizer among them).
        """
        if self._finalizer is not None:
            self._finalizer.detach()
        self._finalizer = weakref.finalize(self, _close_all_quietly, registry)
        self._finalizer.atexit = False  # at exit, the process takes the runners down

    def _serve_on(
        self,
        orchestrator: OrchestratorLoop,
        registry: SessionExecutorRegistry | None = None,
        event_store: Any = None,
    ) -> None:
        """Where the session runners start, which registry keeps them, what they record."""
        self._orchestrator = orchestrator
        self._use_registry(registry)
        self._event_store = event_store

    def _use_registry(self, registry: SessionExecutorRegistry | None) -> None:
        """A caller's registry (the caller closes it), or a private one (closed with us)."""
        if registry is self._registry:
            return
        if registry is None:
            registry = SessionExecutorRegistry.strong_owned()
            self._own_registry(registry)
        else:
            if self._finalizer is not None:
                self._finalizer.detach()
                self._finalizer = None
        self._registry = registry

    @property
    def registry(self) -> SessionExecutorRegistry:
        return self._registry

    @property
    def orchestrator(self) -> OrchestratorLoop:
        """The loop session runners start on; :meth:`OrchestratorLoop.shared` unless set."""
        if self._orchestrator is None:
            self._orchestrator = OrchestratorLoop.shared()
        return self._orchestrator

    # -- the session runner ----------------------------------------------------

    def _net_spec(self) -> NetSpec | None:
        """The net a session runs (its structure keys the session's runner)."""
        return None

    def session_scope(self) -> str:
        """The ``SessionKey.scope`` this node's runners are kept under.

        The node's name and a digest of its net: two nodes may share a name
        (ADK allows it in nested ``Workflow``s) and a registry, and must not
        share a runner.
        """
        spec = self._net_spec()
        if spec is None:
            return self.name
        digest = hashlib.sha256(spec.fingerprint_json().encode()).hexdigest()
        return f"{self.name}#{digest[:12]}"

    def session_key(self, session: Any) -> SessionKey:
        """The registry key of ``session``'s runner for this node."""
        return SessionKey.of(session, scope=self.session_scope())

    def _new_scope(self) -> TurnScope:
        from .workflow.compiler import TurnScope

        return TurnScope()

    def _runner_builder(self, scope: TurnScope, event_store: Any) -> Builder:
        """The session's runner, bound to ``scope``; the base sets the orchestrator.

        ``event_store`` is the session's observability chain (or ``None``).
        """
        raise NotImplementedError

    def _initial_counts(self) -> dict[str, int]:
        """Seed tokens per place of a new session's net."""
        return {}

    def _session_event_store(self, key: SessionKey) -> Any:
        """The node's event store, or its link for ``key`` if it keeps one per session
        (``for_session(key, initial_counts)``, as ``MarkingTraces`` does)."""
        store = self._event_store
        per_session = getattr(store, "for_session", None)
        return per_session(key, self._initial_counts()) if callable(per_session) else store

    async def _start_runner(self, key: SessionKey) -> PetriRunner:
        scope = self._new_scope()
        store = self._session_event_store(key)
        builder = self._runner_builder(scope, store).orchestrator(self.orchestrator)
        runner = await builder.astart()
        runner.attachments[SCOPE_ATTACHMENT] = scope
        return runner

    async def _session_runner(self, ctx: Any) -> PetriRunner:
        """This invocation's session runner, started on first use."""
        ic = ctx.get_invocation_context()
        key = self.session_key(ic.session)
        runner = await self._registry.aget_or_create(key, self._start_runner)
        abort_turns_on_failure(runner)
        return runner

    async def _open_turn(self, ctx: Any) -> tuple[PetriRunner, TurnScope]:
        """The session's runner, with its scope pointed at this invocation."""
        runner = await self._session_runner(ctx)
        scope: TurnScope = runner.attachments[SCOPE_ATTACHMENT]
        scope.ctx = ctx
        scope.loop = asyncio.get_running_loop()
        # As Workflow does: child events are attributed to this node.
        ctx.event_author = self.name
        return runner, scope

    @staticmethod
    def _turn(
        ctx: Any,
        runner: PetriRunner,
        inject: Callable[[], bool],
        egress: HotStream[Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        ic = ctx.get_invocation_context()
        return run_turn(
            ic.invocation_id,
            runner,
            inject,
            abort_signal=getattr(ic, "_abort_signal", None),
            egress=egress,
        )

    # -- the result ------------------------------------------------------------

    @staticmethod
    def _fail(ctx: Any, failure: WorkflowFailure) -> None:
        """Fail the node as ``Workflow`` fails: a node's error, or raise the net's."""
        if failure.from_node and failure.error is not None:
            # Its runner has recorded the error event: fail as Workflow does.
            ctx._error = failure.error
            ctx._error_node_path = failure.node_path
        else:
            raise failure.error or RuntimeError(failure.message)

    @classmethod
    def _settle(cls, ctx: Any, result: TurnResult) -> None:
        """Leave ``result`` on ``ctx`` as ``Workflow._finalize`` would."""
        if result.kind == "output":
            # The terminal node ran with use_as_output, so its event is this
            # node's output event already (Workflow._finalize does the same).
            ctx.output = result.output
            ctx._output_delegated = True
        elif result.kind == "waiting":
            ctx._interrupt_ids = set(result.interrupt_ids)
        elif result.kind == "failed":
            assert result.failure is not None
            cls._fail(ctx, result.failure)
