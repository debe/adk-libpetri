"""``PetriWorkflow``: a compiled ADK workflow served as an ADK node (``@experimental``).

Drop-in for ``Runner(node=workflow)``::

    node = PetriWorkflow.from_workflow(workflow, orchestrator=OrchestratorLoop.shared())
    runner = InMemoryRunner(node=node, app_name="app")

It is a ``BaseNode``, not a ``BaseAgent``, on purpose: ADK 2.11 still runs a
``BaseAgent`` root on its legacy path, which has no node ``Context`` to run
child nodes with. As a node it also nests inside another ``Workflow``.

Each session gets one long-lived net (one turn at a time under its permit).
Every compiled node transition runs its ADK node inside the current turn's
invocation, so the node's events reach the session as under ADK's own
``Workflow``; the net adds the scheduling and the turn's final event: the
terminal output, the pending interrupt ids, or a typed error event.

A turn that answers pending ``adk_request_input`` interrupts (ADK passes the
answers as ``ctx.resume_inputs``) is a *resume*: they go to the net's
``wf/resumeIn`` place instead of ``USER_IN``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterable, Mapping
from typing import Any

from google.adk.workflow import BaseNode, Workflow
from google.genai import types
from pydantic import PrivateAttr

from .. import colours as C
from .._aio import OrchestratorLoop
from .._experimental import experimental
from ..runner.petri_runner import PetriRunner
from ..runner.session_key import SessionKey
from ..runner.session_registry import SessionExecutorRegistry
from ..runner.turn import abort_turns_on_failure, run_turn
from .compiler import (
    RESUME_IN,
    CompiledWorkflow,
    MultiRoute,
    StateMode,
    TurnScope,
    compile_workflow,
)
from .tokens import Resumed

_SCOPE = "adk_libpetri.workflow.turn_scope"


def _content(node_input: Any) -> types.Content | None:
    if node_input is None or isinstance(node_input, types.Content):
        return node_input
    return types.Content(role="user", parts=[types.Part(text=str(node_input))])


@experimental
class PetriWorkflow(BaseNode):
    rerun_on_resume: bool = True

    _compiled: CompiledWorkflow = PrivateAttr()
    _registry: SessionExecutorRegistry = PrivateAttr()
    _orchestrator: OrchestratorLoop = PrivateAttr()
    _event_store: Any = PrivateAttr(default=None)

    @classmethod
    def from_workflow(
        cls,
        workflow: Workflow,
        *,
        orchestrator: OrchestratorLoop,
        registry: SessionExecutorRegistry | None = None,
        event_store: Any = None,
        interruptible: Iterable[str] = (),
        back_edge_budget: Mapping[tuple[str, str], int] | None = None,
        multi_route: MultiRoute = "reject",
        state: StateMode = "reject",
    ) -> PetriWorkflow:
        """Compile ``workflow`` and wrap it; raises ``WorkflowTranslationError``."""
        compiled = compile_workflow(
            workflow,
            interruptible=interruptible,
            back_edge_budget=back_edge_budget,
            multi_route=multi_route,
            state=state,
        )
        return cls.from_compiled(
            compiled, orchestrator=orchestrator, registry=registry, event_store=event_store
        )

    @classmethod
    def from_compiled(
        cls,
        compiled: CompiledWorkflow,
        *,
        orchestrator: OrchestratorLoop,
        registry: SessionExecutorRegistry | None = None,
        event_store: Any = None,
    ) -> PetriWorkflow:
        node = cls(name=compiled.name, description=compiled.workflow.description)
        node._compiled = compiled
        node._registry = registry or SessionExecutorRegistry.strong_owned()
        node._orchestrator = orchestrator
        node._event_store = event_store
        return node

    @property
    def compiled(self) -> CompiledWorkflow:
        return self._compiled

    @property
    def registry(self) -> SessionExecutorRegistry:
        return self._registry

    async def _start_runner(self, key: SessionKey) -> PetriRunner:
        compiled = self._compiled
        scope = TurnScope()
        builder = (
            PetriRunner.builder(compiled.spec, compiled.actions(scope))
            .environment_place(C.USER_IN)
            .initial_marking(compiled.initial_marking())
            .orchestrator(self._orchestrator)
        )
        if compiled.spec.has_place(RESUME_IN):
            builder.environment_place(RESUME_IN)
        if self._event_store is not None:
            builder.event_store(self._event_store)
        runner = await builder.astart()
        runner.attachments[_SCOPE] = scope
        return runner

    async def _run_impl(self, *, ctx: Any, node_input: Any) -> AsyncGenerator[Any, None]:
        ic = ctx.get_invocation_context()
        key = SessionKey.of(ic.session, scope=self.name)
        runner = await self._registry.aget_or_create(key, self._start_runner)
        abort_turns_on_failure(runner)
        scope: TurnScope = runner.attachments[_SCOPE]
        scope.ctx = ctx
        scope.loop = asyncio.get_running_loop()
        ctx.event_author = self.name

        resume_inputs = ctx.resume_inputs
        if resume_inputs:
            answers = [Resumed(iid, resp) for iid, resp in resume_inputs.items()]

            def inject() -> bool:
                return runner.inject_many(RESUME_IN, answers)

        else:
            content = _content(node_input) or ic.user_content
            if content is None:
                return

            def inject() -> bool:
                return runner.inject(C.USER_IN, content)

        async for event in run_turn(
            ic.invocation_id,
            runner,
            inject,
            abort_signal=getattr(ic, "_abort_signal", None),
        ):
            if event.output is not None or event.long_running_tool_ids or event.error_code:
                yield event
