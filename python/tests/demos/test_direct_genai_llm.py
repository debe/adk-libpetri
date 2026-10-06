"""Port of Java ``SyncGeminiLlmTest`` for the Python exemplar :class:`DirectGenaiLlm`.

Java proves its sync ``BaseLlm`` keeps the model call on the caller's thread
and off ``ForkJoinPool.commonPool()``. The Python form of that claim: the
model call, HTTP I/O and response mapping included, runs on the orchestrator
loop, where ``LlmStep_LlmCall`` hops it with ``on_loop``. Not on the libpetri
Tokio thread the action started on (genai's httpx client cannot run there),
and not on a worker thread.

* ``test_model_call_runs_on_the_orchestrator_loop_through_the_subnet``: the
  architectural guard, through the real ``LlmStep`` subnet.
* ``test_direct_genai_llm_runs_a_real_genai_call_on_the_orchestrator_loop``:
  the real exemplar against respx-mocked HTTP (Java: a localhost stub).
* ``test_streamed_chunks_map_straight_through_without_the_terminator``:
  Python-only, the streaming path end to end over mocked SSE.
* ``test_gemini3_stream_terminator_is_dropped_but_real_text_is_kept`` and
  ``test_connect_is_unsupported``.

No real network: respx intercepts every httpx request genai makes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import libpetri as lp
import pytest
import respx
from google import genai
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import NetSpec
from adk_libpetri.subnet import llm_step
from demos.direct_genai_llm import DirectGenaiLlm, is_stream_terminator

MODEL = "gemini-2.0-flash"
API = "https://generativelanguage.googleapis.com"

CANNED_RESPONSE = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "direct hello"}]},
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3},
}


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("direct-genai-demo")
    yield loop
    loop.close()


@dataclass
class Where:
    """Where a piece of the call ran, recorded from inside it."""

    on_orchestrator_thread: bool | None = None
    on_orchestrator_loop: bool | None = None

    def record(self, orch: OrchestratorLoop) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        self.on_orchestrator_thread = orch.on_thread
        self.on_orchestrator_loop = running is orch.loop


def simple_request(user_text: str) -> LlmRequest:
    return LlmRequest(
        model=MODEL,
        contents=[types.Content(role="user", parts=[types.Part(text=user_text)])],
    )


def first_text(response: LlmResponse) -> str:
    assert response.content is not None and response.content.parts, response
    text = response.content.parts[0].text
    assert text is not None, response
    return text


async def run_llm_step(orch: OrchestratorLoop, llm: BaseLlm, request: LlmRequest) -> list[Any]:
    """Run a net of just ``LlmStep`` to quiescence, started on the orchestrator loop."""
    net = NetSpec.compose("direct-guard", llm_step.DEF).build(llm_step.action_bindings(llm))
    marking = await orch.run(lp.run_async(net, initial={C.LLM_REQUEST.name: [request]}))
    return list(marking.tokens(C.LLM_RESPONSE.name))


# ============================================================
#  Test A: the architectural guard, through the subnet
# ============================================================


class DirectPatternLlm(BaseLlm):
    """Produces its response inside ``generate_content_async`` itself, the
    shape :class:`DirectGenaiLlm` has, recording where it ran."""

    model: str = "direct-pattern"

    def bind(self, response: LlmResponse, where: Where, orch: OrchestratorLoop) -> DirectPatternLlm:
        self._response, self._where, self._orch = response, where, orch
        return self

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._where.record(self._orch)
        await asyncio.sleep(0.001)  # a real await: only a real loop can serve it
        yield self._response


async def test_model_call_runs_on_the_orchestrator_loop_through_the_subnet(
    orch: OrchestratorLoop,
) -> None:
    where = Where()
    response = LlmResponse(content=types.Content(role="model", parts=[types.Part(text="hi")]))
    llm = DirectPatternLlm().bind(response, where, orch)

    responses = await asyncio.wait_for(run_llm_step(orch, llm, simple_request("q")), 15)

    assert responses == [response]
    assert where.on_orchestrator_thread is True
    assert where.on_orchestrator_loop is True


# ============================================================
#  Test B: the real DirectGenaiLlm against mocked HTTP
# ============================================================


async def test_direct_genai_llm_runs_a_real_genai_call_on_the_orchestrator_loop(
    orch: OrchestratorLoop,
) -> None:
    io = Where()
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        io.record(orch)
        seen.append(request)
        return httpx.Response(200, json=CANNED_RESPONSE)

    client = genai.Client(api_key="test-key")
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(url__regex=rf"^{API}/.*models/{MODEL}:generateContent").mock(
                side_effect=answer
            )
            llm = DirectGenaiLlm.of(MODEL, client)
            responses = await asyncio.wait_for(run_llm_step(orch, llm, simple_request("hi")), 15)
    finally:
        await orch.run(client.aio.aclose())
        client.close()

    assert [first_text(r) for r in responses] == ["direct hello"]
    # The HTTP exchange ran on the orchestrator loop's own thread.
    assert io.on_orchestrator_thread is True
    assert io.on_orchestrator_loop is True
    # The request went out as ADK built it, under the caller's client.
    (request,) = seen
    assert request.headers["x-goog-api-key"] == "test-key"
    body = json.loads(request.content)
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


# ============================================================
#  Streaming over mocked SSE (Python-only)
# ============================================================


def sse(*chunks: dict[str, object]) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\r\n\r\n".encode() for c in chunks)


def chunk_json(text: str, finish: str | None = None) -> dict[str, object]:
    candidate: dict[str, object] = {"content": {"role": "model", "parts": [{"text": text}]}}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate]}


async def test_streamed_chunks_map_straight_through_without_the_terminator(
    orch: OrchestratorLoop,
) -> None:
    body = sse(chunk_json("Hel"), chunk_json("lo"), chunk_json("", finish="STOP"))
    client = genai.Client(api_key="test-key")
    llm = DirectGenaiLlm.of(MODEL, client)

    async def collect() -> list[LlmResponse]:
        return [r async for r in llm.generate_content_async(simple_request("hi"), stream=True)]

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(url__regex=rf"^{API}/.*models/{MODEL}:streamGenerateContent").mock(
                return_value=httpx.Response(
                    200, content=body, headers={"content-type": "text/event-stream"}
                )
            )
            responses = await asyncio.wait_for(orch.run(collect()), 15)
    finally:
        await orch.run(client.aio.aclose())
        client.close()

    assert [first_text(r) for r in responses] == ["Hel", "lo"]
    assert all(r.partial for r in responses)


# ============================================================
#  Gemini 3 stream terminator
# ============================================================


def chunk_of(part: types.Part) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))]
    )


def test_gemini3_stream_terminator_is_dropped_but_real_text_is_kept() -> None:
    assert is_stream_terminator(chunk_of(types.Part(text="")))
    # A terminator carrying an explicit thought=False is still a terminator.
    assert is_stream_terminator(chunk_of(types.Part(text="", thought=False)))
    # Real content is never mistaken for one.
    assert not is_stream_terminator(chunk_of(types.Part(text="hello")))
    # Nor is a chunk with no parts.
    assert not is_stream_terminator(types.GenerateContentResponse(candidates=[]))


def test_connect_is_unsupported() -> None:
    client = genai.Client(api_key="test-key")
    try:
        llm = DirectGenaiLlm.of(MODEL, client)
        with pytest.raises(NotImplementedError):
            llm.connect(simple_request("q"))
    finally:
        client.close()
