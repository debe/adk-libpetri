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
``Workflow``. The net adds the scheduling, and its turn end leaves the result
where ``Workflow`` would: the terminal output on the node's context (the
terminal node's own event already carries it), the pending interrupt ids, or
the failure. A failing ADK node fails this node as it fails a ``Workflow``;
a failure of the net itself (a spent back-edge budget, two terminal outputs)
is raised as a typed :class:`~adk_libpetri.workflow.report.WorkflowRunError`.

A turn that answers pending ``adk_request_input`` interrupts (ADK passes the
answers as ``ctx.resume_inputs``) is a *resume*: they go to the net's
``wf/resumeIn`` place instead of ``USER_IN``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterable, Mapping
from typing import Any

from google.adk.workflow import BaseNode, Workflow
from pydantic import PrivateAttr

from .._aio import OrchestratorLoop
from .._experimental import experimental
from ..runner.petri_runner import PetriRunner
from ..runner.session_key import SessionKey
from ..runner.session_registry import SessionExecutorRegistry
from ..runner.turn import abort_turns_on_failure, run_turn
from .compiler import (
    INPUT,
    RESUME_IN,
    CompiledWorkflow,
    MultiRoute,
    StateMode,
    TurnScope,
    compile_workflow,
)
from .report import NotInterruptibleError
from .tokens import Resumed

_SCOPE = "adk_libpetri.workflow.turn_scope"


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
        wf = compiled.workflow
        # The schemas let it serve where the Workflow would: as an agent's
        # tool (NodeTool needs input_schema), or under a parent's validation.
        node = cls(
            name=compiled.name,
            description=wf.description,
            input_schema=wf.input_schema,
            output_schema=wf.output_schema,
        )
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
            .environment_place(INPUT)
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
            if not compiled_resumes(self._compiled):
                raise NotInterruptibleError(
                    f"workflow {self.name!r} got answers to interrupts "
                    f"{sorted(resume_inputs)}, but no node was compiled interruptible"
                )
            answers = [Resumed(iid, resp) for iid, resp in resume_inputs.items()]

            def inject() -> bool:
                return runner.inject_many(RESUME_IN, answers)

        else:
            # As Workflow seeds START's successors: node_input unchanged (a
            # parent's output, a tool's arguments), else the user's message.
            start = node_input if node_input is not None else ic.user_content
            if start is None:
                return

            def inject() -> bool:
                return runner.inject(INPUT, start)

        scope.result = None
        async for _ in run_turn(
            ic.invocation_id,
            runner,
            inject,
            abort_signal=getattr(ic, "_abort_signal", None),
        ):
            pass  # the net's marker event; its result is on the scope
        result = scope.result
        if result is None:
            return  # aborted
        if result.kind == "output":
            # The terminal node ran with use_as_output, so its event is this
            # node's output event already (Workflow._finalize does the same).
            ctx.output = result.output
            ctx._output_delegated = True
        elif result.kind == "waiting":
            ctx._interrupt_ids = set(result.interrupt_ids)
        elif result.kind == "failed":
            failure = result.failure
            assert failure is not None
            if failure.from_node and failure.error is not None:
                # Its runner has recorded the error event: fail as Workflow does.
                ctx._error = failure.error
                ctx._error_node_path = failure.node_path
            else:
                raise failure.error or RuntimeError(failure.message)
        return
        yield  # an async generator, as BaseNode._run_impl must be


def compiled_resumes(compiled: CompiledWorkflow) -> bool:
    return compiled.spec.has_place(RESUME_IN)
