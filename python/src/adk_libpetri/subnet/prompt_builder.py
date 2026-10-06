"""Stock subnet turning a user ``Content`` into an ``LlmRequest``.

Stateless and single-turn: the request carries exactly the incoming content
plus an optional system instruction. For multi-turn history keep the history
on a typed in-net place and give your own prompt-building transition a read
arc on it (the in-net conversation-place pattern).

    [USER_IN] --PromptBuilder_BuildPrompt--> [LLM_REQUEST]
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from google.adk.tools.base_tool import BaseTool

from .. import colours as C
from .._spec import Action, Ctx, NetSpec, Port, TransitionSpec, one, out
from . import llm_requests
from .actions import bind

NAME = "PromptBuilder"


class Transitions:
    BUILD_PROMPT = f"{NAME}_BuildPrompt"


@dataclass(frozen=True)
class Config:
    model: str
    system_instruction: str | None = None
    tools: Mapping[str, BaseTool] = field(default_factory=dict)


DEF = NetSpec(
    NAME,
    (TransitionSpec(Transitions.BUILD_PROMPT, (one(C.USER_IN),), out(C.LLM_REQUEST)),),
    ports=(Port("userIn", "in", C.USER_IN), Port("llmRequest", "out", C.LLM_REQUEST)),
)


def action_bindings(config: Config) -> dict[str, Action]:
    def build_prompt(ctx: Ctx) -> None:
        content = ctx.input(C.USER_IN)
        ctx.output(
            C.LLM_REQUEST,
            llm_requests.build(config.model, config.system_instruction, config.tools, [content]),
        )

    return bind(DEF, {Transitions.BUILD_PROMPT: build_prompt})
