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

It also loads from ADK's own YAML agent config, so ``adk web`` and ``adk run``
serve the compiled net::

    # root_agent.yaml
    agent_class: adk_libpetri.workflow.PetriWorkflow
    name: root_agent
    state: legacy_read                 # compile options, as for compile_workflow
    back_edge_budget: [[route_headline, generate_headline, 3]]
    edges:                             # exactly a Workflow's edges
      - [START, .agent.process_input, generate_headline.yaml]
      - [generate_headline, {unrelated: generate_headline.yaml}]

ADK's loader resolves the edges (code references, nested YAML files, route
maps) as it does for ``agent_class: Workflow``; the node then compiles itself
on :meth:`OrchestratorLoop.shared`. A ``WorkflowTranslationError`` fails the
load.

A turn that answers pending ``adk_request_input`` interrupts (ADK passes the
answers as ``ctx.resume_inputs``) is a *resume*: they go to the net's
``wf/resumeIn`` place instead of ``USER_IN``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Mapping
from typing import Any

from google.adk.workflow import Workflow
from google.adk.workflow._graph import EdgeItem
from pydantic import Field, PrivateAttr

from .._aio import OrchestratorLoop
from .._experimental import experimental
from .._net_node import NetNodeBase
from ..runner.petri_runner import Builder, PetriRunner
from ..runner.session_registry import SessionExecutorRegistry
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


@experimental
class PetriWorkflow(NetNodeBase):
    rerun_on_resume: bool = True

    # Set from YAML (or by keyword): the Workflow to compile, and how.
    edges: list[EdgeItem] = Field(default_factory=list)
    """A ``Workflow``'s edges; ADK's config loader resolves them by this type."""
    max_concurrency: int | None = None
    interruptible: list[str] = Field(default_factory=list)
    back_edge_budget: list[tuple[str, str, int]] = Field(default_factory=list)
    """``[from, to, budget]`` per budgeted back edge (ADR 0007)."""
    state: StateMode = "reject"
    multi_route: MultiRoute = "reject"

    _compiled: CompiledWorkflow = PrivateAttr()

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
        node._serve_on(orchestrator, registry, event_store)
        return node

    def _net_spec(self) -> Any:
        compiled = getattr(self, "_compiled", None)
        return compiled.spec if compiled is not None else None

    def model_post_init(self, context: Any, /) -> None:
        super().model_post_init(context)
        if not self.edges:
            return  # built by from_compiled
        workflow = Workflow(
            name=self.name,
            description=self.description,
            edges=self.edges,
            max_concurrency=self.max_concurrency,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
        )
        self._compiled = compile_workflow(
            workflow,
            interruptible=self.interruptible,
            back_edge_budget={(a, b): k for a, b, k in self.back_edge_budget},
            multi_route=self.multi_route,
            state=self.state,
        )
        self._serve_on(OrchestratorLoop.shared())

    @classmethod
    def from_config(
        cls,
        config_path: str,
        *,
        orchestrator: OrchestratorLoop | None = None,
        registry: SessionExecutorRegistry | None = None,
        event_store: Any = None,
        **compile_options: Any,
    ) -> PetriWorkflow:
        """Load an ADK YAML agent config and serve it compiled.

        ``agent_class: Workflow`` is compiled with ``compile_options``;
        ``agent_class: adk_libpetri.workflow.PetriWorkflow`` carries its own
        options in the YAML. ``orchestrator`` defaults to the shared loop.
        """
        from google.adk.agents.config_agent_utils import from_config

        node = from_config(config_path)
        loop = orchestrator or OrchestratorLoop.shared()
        if isinstance(node, PetriWorkflow):
            if compile_options:
                raise TypeError("a PetriWorkflow YAML sets its compile options itself")
            node._serve_on(loop, registry if registry is not None else node.registry, event_store)
            return node
        if not isinstance(node, Workflow):
            raise TypeError(
                f"{config_path} defines a {type(node).__name__}, not a Workflow; "
                "an agent root runs on ADK (or PetriAgent) directly"
            )
        return cls.from_workflow(
            node,
            orchestrator=loop,
            registry=registry,
            event_store=event_store,
            **compile_options,
        )

    @property
    def compiled(self) -> CompiledWorkflow:
        return self._compiled

    def _runner_builder(self, scope: TurnScope) -> Builder:
        compiled = self._compiled
        builder = (
            PetriRunner.builder(compiled.spec, compiled.actions(scope))
            .environment_place(INPUT)
            .initial_marking(compiled.initial_marking())
        )
        if compiled.spec.has_place(RESUME_IN):
            builder.environment_place(RESUME_IN)
        if self._event_store is not None:
            builder.event_store(self._event_store)
        return builder

    async def _run_impl(self, *, ctx: Any, node_input: Any) -> AsyncGenerator[Any, None]:
        runner, scope = await self._open_turn(ctx)
        ic = ctx.get_invocation_context()

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
        async for _ in self._turn(ctx, runner, inject):
            pass  # the net's marker event; its result is on the scope
        result = scope.result
        if result is None:
            return  # aborted
        self._settle(ctx, result)
        return
        yield  # an async generator, as BaseNode._run_impl must be


def compiled_resumes(compiled: CompiledWorkflow) -> bool:
    return compiled.spec.has_place(RESUME_IN)
