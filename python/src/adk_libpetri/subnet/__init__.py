"""Stock subnets: convenience templates over the composition primitives (commitment 7)."""

from . import (
    actions,
    llm_agent,
    llm_step,
    llm_streaming_step,
    persist_state,
    prompt_builder,
    router,
    streaming_llm_agent,
    tool_dispatch,
    transfer_router,
)
from .actions import bind, bind_composed, merge

__all__ = [
    "actions",
    "bind",
    "bind_composed",
    "llm_agent",
    "llm_step",
    "llm_streaming_step",
    "merge",
    "persist_state",
    "prompt_builder",
    "router",
    "streaming_llm_agent",
    "tool_dispatch",
    "transfer_router",
]
