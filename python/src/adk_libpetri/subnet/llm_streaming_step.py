"""Streaming variant of ``LlmStepSubnet`` with true incremental chunk injection (``@experimental``).

Collecting the stream and producing N chunk tokens with ``ctx.output`` would
publish nothing until the call completes: a transition's outputs land when
its action finishes. ``LlmCallStream`` instead injects each partial onto the
``CHUNK`` env place of its own net as the model produces it, then a terminal
merged response once every partial was accepted. ``EmitChunk`` turns partial
markers into partial ``Event``s and releases the merged response to the
router.

**Order.** ``EmitChunk`` is a *sync* action at priority 20 (above the
router): libpetri-py may start an async transition again while an earlier
firing is in flight, which Java's executor never does (CONC-002), but a sync
action completes inside its firing, so chunks leave in arrival order.

**Executor wiring.** ``LlmCallStream`` needs the running executor's handle.
Give the subnet config a :class:`~adk_libpetri.runner.HandleRef` and pass the
same ref to ``PetriRunner.builder(...).handle_ref(ref)``, one per session;
``StreamingLlmAgentSubnet.runner_factory`` does this for you.

    [LLM_REQUEST] --LlmCallStream--> (side effect: inject N partials + 1 terminal into CHUNK)
    [CHUNK]env    --EmitChunk (prio 20)--> xor([EVENT_OUT], [LLM_RESPONSE])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .. import colours as C
from .._aio import on_loop
from .._experimental import experimental
from .._spec import Action, Ctx, NetSpec, Place, Port, TransitionSpec, one, xor
from ._common import IdSupplier, random_id
from .actions import bind

if TYPE_CHECKING:
    from ..runner.petri_runner import HandleRef

NAME = "LlmStreamingStep"


class Transitions:
    LLM_CALL_STREAM = f"{NAME}_LlmCallStream"
    EMIT_CHUNK = f"{NAME}_EmitChunk"


@dataclass(frozen=True, slots=True)
class LlmResponseChunk:
    """A partial response, or (``terminal``) the merged response of the stream."""

    partial: LlmResponse
    terminal: bool = False


class Places:
    CHUNK: Place[LlmResponseChunk] = Place(f"{NAME}_chunk", LlmResponseChunk)
    """Per-chunk arrival queue; declare it as an env place on the runner."""


@experimental
@dataclass(frozen=True)
class Config:
    author: str
    handle_ref: HandleRef
    invocation_id_supplier: IdSupplier = field(default=random_id)


DEF = NetSpec(
    NAME,
    (
        TransitionSpec(Transitions.LLM_CALL_STREAM, (one(C.LLM_REQUEST),)),
        TransitionSpec(
            Transitions.EMIT_CHUNK,
            (one(Places.CHUNK),),
            xor(C.EVENT_OUT, C.LLM_RESPONSE),
            priority=20,
        ),
    ),
    extra_places=(C.LLM_RESPONSE, C.EVENT_OUT, Places.CHUNK),
    ports=(
        Port("llmRequest", "in", C.LLM_REQUEST),
        Port("eventOut", "out", C.EVENT_OUT),
        Port("llmResponse", "out", C.LLM_RESPONSE),
    ),
)


def merge_chunks(chunks: list[LlmResponse]) -> LlmResponse:
    """Concatenate the chunks' parts in stream order into one ``turn_complete`` response."""
    parts: list[types.Part] = []
    for c in chunks:
        if c.content is not None and c.content.parts:
            parts.extend(c.content.parts)
    return LlmResponse(content=types.Content(role="model", parts=parts), turn_complete=True)


async def stream_chunks(llm: BaseLlm, request: LlmRequest, ref: HandleRef) -> None:
    """One streaming call: inject each chunk as it arrives, then the merged terminal."""
    handle = ref.get()
    name = Places.CHUNK.name
    collected: list[LlmResponse] = []
    agen = llm.generate_content_async(request, stream=True)
    try:
        async for chunk in agen:
            collected.append(chunk)
            if not handle.inject(name, LlmResponseChunk(chunk)):
                raise RuntimeError("chunk injection was rejected before streaming completed")
    finally:
        await agen.aclose()
    if not collected:
        raise RuntimeError("BaseLlm streaming call yielded no chunks")
    if not handle.inject(name, LlmResponseChunk(merge_chunks(collected), terminal=True)):
        raise RuntimeError("terminal chunk injection was rejected before streaming completed")


def action_bindings(llm: BaseLlm, config: Config) -> dict[str, Action]:
    async def llm_call_stream(ctx: Ctx) -> None:
        if not config.handle_ref.is_set:
            raise RuntimeError(
                "Config.handle_ref has not been populated. Register the same HandleRef with "
                "PetriRunner.builder(...).handle_ref(ref)."
            )
        request = ctx.input(C.LLM_REQUEST)
        await on_loop(stream_chunks(llm, request, config.handle_ref))

    def emit_chunk(ctx: Ctx) -> None:
        chunk = ctx.input(Places.CHUNK)
        if chunk.terminal:
            ctx.output(C.LLM_RESPONSE, chunk.partial)
        else:
            ctx.output(
                C.EVENT_OUT,
                Event(
                    invocation_id=config.invocation_id_supplier(),
                    author=config.author,
                    content=chunk.partial.content,
                    partial=True,
                ),
            )

    return bind(
        DEF,
        {Transitions.LLM_CALL_STREAM: llm_call_stream, Transitions.EMIT_CHUNK: emit_chunk},
    )
