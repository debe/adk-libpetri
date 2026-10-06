"""Exemplar ``BaseLlm`` that calls google.genai's async client directly.

The Python counterpart of Java's ``SyncGeminiLlm``. You own the call site:
one caller-owned ``genai.Client`` (build once, share, close at shutdown), the
request sent as ADK built it, and each streamed chunk mapped straight
through.

**What carries over from Java, and what does not.** ``SyncGeminiLlm`` exists
because ADK Java's ``Gemini`` and genai Java's async client hop to
``ForkJoinPool.commonPool()``, a JVM-global executor the project forbids, and
the sync genai facade avoids both hops. ADK Python's ``Gemini`` has no such
hop: it awaits ``client.aio.models.generate_content`` on the running loop.
So this exemplar is optional in Python (ADR 0006), and what it shows is
the Python form of the same rule: the whole model call, HTTP I/O and response
mapping included, runs on the loop the action awaits it on. In adk-libpetri
that is the orchestrator loop: an action runs on a libpetri Tokio thread with
no asyncio loop, and ``LlmStep_LlmCall`` hops the call there with
``on_loop``. genai's async client is httpx/anyio underneath, which cannot
run on the Tokio thread itself, so a direct call needs that hop and no other.

**Streaming.** ADK's ``Gemini`` folds the stream through its
``StreamingResponseAggregator``, which also drops the bare empty-text part
Gemini 3 ends a stream with. This exemplar bypasses the aggregator (the
streaming subnet merges the chunks itself), so it filters the terminator
itself: without the filter it reaches the net as a chunk and is emitted as a
spurious empty partial ``Event``.

Usage::

    client = genai.Client(api_key=key)          # build once, share, close on shutdown
    llm = DirectGenaiLlm.of("gemini-2.5-flash", client)
    actions = llm_step.action_bindings(llm)     # or llm_agent.action_bindings(llm, config)

Live/BIDI (:meth:`connect`) is unsupported: it is full-duplex and enters the
net via env-place injection, not through this adapter.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from google import genai
from google.adk.models.base_llm import BaseLlm
from google.adk.models.base_llm_connection import BaseLlmConnection
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr


class DirectGenaiLlm(BaseLlm):
    _client: genai.Client = PrivateAttr()

    @classmethod
    def of(cls, model: str, client: genai.Client) -> DirectGenaiLlm:
        if client is None:
            raise TypeError("client is required")
        llm = cls(model=model)
        llm._client = client
        return llm

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        # The same last-turn fix-up ADK's Gemini applies, from the BaseLlm it shares.
        self._maybe_append_user_content(llm_request)
        model = llm_request.model or self.model
        contents = cast(list[types.ContentUnion], llm_request.contents)
        if stream:
            chunks = await self._client.aio.models.generate_content_stream(
                model=model, contents=contents, config=llm_request.config
            )
            async for chunk in chunks:
                if is_stream_terminator(chunk):
                    continue
                response = LlmResponse.create(chunk)
                response.partial = True
                yield response
            return
        response = await self._client.aio.models.generate_content(
            model=model, contents=contents, config=llm_request.config
        )
        yield LlmResponse.create(response)

    def connect(self, llm_request: LlmRequest) -> AbstractAsyncContextManager[BaseLlmConnection]:
        raise NotImplementedError(
            "DirectGenaiLlm covers generate_content (unary and server streaming). "
            "Live/BIDI uses the connect() path and env-place injection."
        )


def is_stream_terminator(chunk: types.GenerateContentResponse) -> bool:
    """The bare empty-text part Gemini 3 ends a stream with.

    Matched by rebuilding rather than against a literal, so a terminator that
    also carries an explicit ``thought=False`` is still recognised.
    """
    parts: Any = chunk.parts
    if not parts or len(parts) != 1:
        return False
    part = parts[0]
    if part.text != "":
        return False
    return types.Part(text="", thought=part.thought) == part
