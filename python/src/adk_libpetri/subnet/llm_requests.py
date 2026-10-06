"""The one ``LlmRequest`` factory the stock subnets share. Not public API."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from google.adk.models.llm_request import LlmRequest
from google.adk.tools.base_tool import BaseTool
from google.genai import types


def build(
    model: str,
    system_instruction: str | None,
    tools: Mapping[str, BaseTool],
    contents: Sequence[types.Content],
) -> LlmRequest:
    req = LlmRequest(model=model, contents=list(contents))
    if system_instruction is not None:
        req.config = types.GenerateContentConfig(
            system_instruction=types.Content(parts=[types.Part(text=system_instruction)])
        )
    if tools:
        req.append_tools(list(tools.values()))
    return req
