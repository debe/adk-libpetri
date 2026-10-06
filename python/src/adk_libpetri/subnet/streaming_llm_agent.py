"""SSE counterpart of ``LlmAgentSubnet`` (``@experimental``).

The same LLM-and-tool loop and turn permit, composed with
``LlmStreamingStepSubnet`` so model chunks surface as partial ADK events; an
abort also resets the stream's queued chunks. Wire it through
:func:`runner_factory`, which gives each session its own handle ref and
declares ``USER_IN`` and ``CHUNK`` as env places. Run it under
``StreamingMode.SSE``; normal mode returns only the turn's final event.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from google.adk.models.base_llm import BaseLlm
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from .._experimental import experimental
from .._spec import Action, NetSpec
from . import llm_agent, llm_step, llm_streaming_step, tool_dispatch
from ._common import IdSupplier, random_id
from .actions import bind, merge

if TYPE_CHECKING:
    from ..runner.petri_runner import Builder, HandleRef, PetriRunner
    from ..runner.session_key import SessionKey

NAME = "StreamingLlmAgent"

DEF: NetSpec = llm_agent.build_composed_def(NAME, llm_streaming_step.DEF)


@experimental
@dataclass(frozen=True)
class Config:
    name: str
    model: str
    system_instruction: str | None = None
    tools: Mapping[str, BaseTool] = field(default_factory=dict)
    reask_budget: int = 3
    fallback_content: types.Content = field(
        default_factory=lambda: llm_agent.DEFAULT_FALLBACK.model_copy(deep=True)
    )
    invocation_id_supplier: IdSupplier = field(default=random_id)
    callbacks: llm_step.Callbacks = field(default_factory=llm_step.Callbacks.none)
    tool_context_supplier: tool_dispatch.ToolContextSupplier = lambda: None

    def agent_config(self) -> llm_agent.Config:
        return llm_agent.Config(
            name=self.name,
            model=self.model,
            system_instruction=self.system_instruction,
            tools=self.tools,
            reask_budget=self.reask_budget,
            fallback_content=self.fallback_content,
            invocation_id_supplier=self.invocation_id_supplier,
            callbacks=self.callbacks,
            tool_context_supplier=self.tool_context_supplier,
        )


def action_bindings(llm: BaseLlm, config: Config, handle_ref: HandleRef) -> dict[str, Action]:
    """Full binding map. ``handle_ref`` must be per session (see :func:`runner_factory`)."""
    streaming = llm_streaming_step.Config(config.name, handle_ref, config.invocation_id_supplier)
    return bind(
        DEF,
        merge(
            llm_streaming_step.action_bindings(llm, streaming),
            tool_dispatch.action_bindings(config.tools, config.tool_context_supplier),
            llm_agent.own_actions(config.agent_config()),
        ),
    )


def runner_factory(
    llm: BaseLlm,
    config: Config,
    customize: Callable[[SessionKey, Builder], Any],
) -> Callable[[SessionKey], Awaitable[PetriRunner]]:
    """A ``PetriAgent`` runner factory: per session a fresh handle ref, actions
    bound to it, ``USER_IN`` and ``CHUNK`` declared, then started.

    ``customize`` must at least set ``orchestrator(...)``; it is also where an
    event store or ``resume_from(store, key)`` go. It runs first, so declaring
    ``USER_IN`` or ``CHUNK`` itself fails the start.
    """
    from ..colours import USER_IN
    from ..runner.petri_runner import HandleRef, PetriRunner

    async def factory(key: SessionKey) -> PetriRunner:
        ref = HandleRef()
        builder = PetriRunner.builder(DEF, action_bindings(llm, config, ref))
        result = customize(key, builder)
        if inspect.isawaitable(result):
            await result
        return await (
            builder.environment_place(USER_IN)
            .environment_place(llm_streaming_step.Places.CHUNK)
            .handle_ref(ref)
            .astart()
        )

    return factory


__all__ = ["DEF", "NAME", "Config", "action_bindings", "runner_factory"]
