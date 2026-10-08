"""Stock subnet for one LLM call with Before / After / Error callback transitions.

    [LLM_REQUEST]   --LlmStep_BeforeModel--> xor([READY_TO_CALL], [LLM_RESPONSE])
    [READY_TO_CALL] --LlmStep_LlmCall------> xor([RAW_RESPONSE], [LLM_ERROR])
    [RAW_RESPONSE]  --LlmStep_AfterModel---> [LLM_RESPONSE]
    [LLM_ERROR]     --LlmStep_OnModelError-> [LLM_RESPONSE]

The continue/short-circuit and success/error decisions live in the XOR
structure: the action picks the branch, the executor validates exactly one.
Defaults: ``BeforeModel`` and ``AfterModel`` forward unchanged, ``LlmCall``
calls ``BaseLlm.generate_content_async`` (non-streaming) on the asyncio loop,
and ``OnModelError`` fails the transition unless a recovery callback is bound.

Callbacks may be sync or ``async``; a coroutine result is awaited on the loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from .. import colours as C
from .._aio import on_loop
from .._spec import Action, Ctx, NetSpec, Place, Port, TransitionSpec, one, out, xor
from ._common import call_user, exception_type
from .actions import bind

NAME = "LlmStep"


class Transitions:
    BEFORE_MODEL = f"{NAME}_BeforeModel"
    LLM_CALL = f"{NAME}_LlmCall"
    AFTER_MODEL = f"{NAME}_AfterModel"
    ON_MODEL_ERROR = f"{NAME}_OnModelError"


@dataclass(frozen=True, slots=True)
class LlmError:
    """Typed error colour for the LLM-error branch."""

    message: str
    exception_type: str


class Places:
    READY_TO_CALL: Place[LlmRequest] = Place(f"{NAME}_readyToCall", LlmRequest)
    RAW_RESPONSE: Place[LlmResponse] = Place(f"{NAME}_rawResponse", LlmResponse)
    LLM_ERROR: Place[LlmError] = Place(f"{NAME}_llmError", LlmError)


BeforeModelCallback = Callable[
    [LlmRequest], Union[LlmResponse, Awaitable[LlmResponse | None], None]  # noqa: UP007
]
AfterModelCallback = Callable[[LlmResponse], Union[LlmResponse, Awaitable[LlmResponse]]]  # noqa: UP007
OnModelErrorCallback = Callable[[LlmError], Union[LlmResponse, Awaitable[LlmResponse]]]  # noqa: UP007


@dataclass(frozen=True)
class Callbacks:
    """Optional model callbacks.

    ``before_model`` returns a response to short-circuit the call or ``None``
    to proceed. ``after_model`` maps the raw response. ``on_model_error``
    returns a recovery response; raise from it to fail the transition.
    """

    before_model: BeforeModelCallback | None = None
    after_model: AfterModelCallback | None = None
    on_model_error: OnModelErrorCallback | None = None

    @staticmethod
    def none() -> Callbacks:
        return Callbacks()


DEF = NetSpec(
    NAME,
    (
        TransitionSpec(
            Transitions.BEFORE_MODEL,
            (one(C.LLM_REQUEST),),
            xor(Places.READY_TO_CALL, C.LLM_RESPONSE),
        ),
        TransitionSpec(
            Transitions.LLM_CALL,
            (one(Places.READY_TO_CALL),),
            xor(Places.RAW_RESPONSE, Places.LLM_ERROR),
        ),
        TransitionSpec(Transitions.AFTER_MODEL, (one(Places.RAW_RESPONSE),), out(C.LLM_RESPONSE)),
        TransitionSpec(Transitions.ON_MODEL_ERROR, (one(Places.LLM_ERROR),), out(C.LLM_RESPONSE)),
    ),
    ports=(Port("llmRequest", "in", C.LLM_REQUEST), Port("llmResponse", "out", C.LLM_RESPONSE)),
)


_NO_KEY = (
    "No API key was provided",
    "Missing key inputs argument",
    "API key not valid",
    "API_KEY_INVALID",
)


class ModelKeyMissing(RuntimeError):
    """The model refused the call: no (valid) Gemini API key. The message leads with the fix."""


def key_hint(message: str) -> str:
    """The fix for a model error that says no (valid) Gemini API key is set; else ""."""
    if not any(m in message for m in _NO_KEY):
        return ""
    folder = Path.cwd().name or "."
    return (
        f"No Gemini API key: put GOOGLE_API_KEY=... in {folder}/.env, the folder the "
        "server runs from (or set GOOGLE_GENAI_USE_VERTEXAI=1 with a Vertex project), "
        "and restart it."
    )


def _first_sentence(message: str) -> str:
    head = message.strip().split(". ", 1)[0].rstrip(".")
    return head + "."


async def first_response(llm: BaseLlm, request: LlmRequest) -> LlmResponse:
    """The first response of a non-streaming ``generate_content_async``."""
    agen = llm.generate_content_async(request, stream=False)
    try:
        async for response in agen:
            return response
    finally:
        await agen.aclose()
    raise RuntimeError(f"{type(llm).__name__}.generate_content_async yielded no response")


def action_bindings(llm: BaseLlm, callbacks: Callbacks | None = None) -> dict[str, Action]:
    cb = callbacks or Callbacks.none()

    async def before_model(ctx: Ctx) -> None:
        request = ctx.input(C.LLM_REQUEST)
        short = await call_user(cb.before_model, request) if cb.before_model else None
        if short is not None:
            ctx.output(C.LLM_RESPONSE, short)
        else:
            ctx.output(Places.READY_TO_CALL, request)

    async def llm_call(ctx: Ctx) -> None:
        request = ctx.input(Places.READY_TO_CALL)
        try:
            response = await on_loop(first_response(llm, request))
        except Exception as err:  # a model failure is a value on the error branch
            ctx.output(Places.LLM_ERROR, LlmError(str(err), exception_type(err)))
            return
        ctx.output(Places.RAW_RESPONSE, response)

    async def after_model(ctx: Ctx) -> None:
        raw = ctx.input(Places.RAW_RESPONSE)
        ctx.output(C.LLM_RESPONSE, await call_user(cb.after_model, raw) if cb.after_model else raw)

    async def on_model_error(ctx: Ctx) -> None:
        error = ctx.input(Places.LLM_ERROR)
        if cb.on_model_error is None:
            fix = key_hint(error.message)
            if fix:
                # The fix first: ADK's error snackbar cuts a long message short.
                raise ModelKeyMissing(f"{fix} (The model said: {_first_sentence(error.message)})")
            raise RuntimeError(
                f"{Transitions.ON_MODEL_ERROR} fired with no recovery callback bound: "
                f"{error.message} ({error.exception_type})"
            )
        ctx.output(C.LLM_RESPONSE, await call_user(cb.on_model_error, error))

    return bind(
        DEF,
        {
            Transitions.BEFORE_MODEL: before_model,
            Transitions.LLM_CALL: llm_call,
            Transitions.AFTER_MODEL: after_model,
            Transitions.ON_MODEL_ERROR: on_model_error,
        },
    )
