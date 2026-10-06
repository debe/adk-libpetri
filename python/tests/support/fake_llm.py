"""Scripted ``BaseLlm`` fakes for tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr


def text(t: str, *, partial: bool | None = None) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=t)]), partial=partial
    )


def call(name: str, args: dict[str, Any] | None = None, call_id: str | None = None) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args or {}))
            ],
        )
    )


def calls(*fcs: tuple[str, dict[str, Any]]) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name=n, args=a)) for n, a in fcs],
        )
    )


def transfer(agent: str) -> LlmResponse:
    return call("transfer_to_agent", {"agent_name": agent})


Script = LlmResponse | BaseException | Callable[[LlmRequest], Any]


class ScriptedLlm(BaseLlm):
    """Answers each call with the next scripted item; records every request.

    An item is a response, an exception to raise, or a callable taking the
    request. ``stream_chunks`` maps a call index to a list of responses to
    stream for ``stream=True``.
    """

    model: str = "scripted"
    _script: list[Script] = PrivateAttr(default_factory=list)
    _requests: list[LlmRequest] = PrivateAttr(default_factory=list)
    _delay: float = PrivateAttr(default=0.0)
    _streams: dict[int, list[LlmResponse]] = PrivateAttr(default_factory=dict)

    @classmethod
    def of(cls, *script: Script, delay: float = 0.0) -> ScriptedLlm:
        llm = cls()
        llm._script = list(script)
        llm._delay = delay
        return llm

    def stream(self, index: int, chunks: list[LlmResponse]) -> ScriptedLlm:
        self._streams[index] = chunks
        return self

    @property
    def requests(self) -> list[LlmRequest]:
        return self._requests

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        index = len(self._requests)
        self._requests.append(llm_request)
        if self._delay:
            await asyncio.sleep(self._delay)
        if stream and index in self._streams:
            for chunk in self._streams[index]:
                yield chunk
            return
        if index >= len(self._script):
            raise AssertionError(f"ScriptedLlm: no scripted response for call {index}")
        item = self._script[index]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(llm_request)
            if asyncio.iscoroutine(item):
                item = await item
        yield item
