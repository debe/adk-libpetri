"""Stock subnet for the trivial tool-dispatch case.

    [TOOL_CALLS] --ToolDispatch_Dispatch--> [TOOL_RESULTS]

One ``Dispatch`` firing runs every call of the batch concurrently on the
asyncio loop and AND-joins the results into one ``ToolResults`` token. Use it
when the tools are independent; anything richer (dependencies, rate-limit
pools, mutexes) belongs in a net of your own where each tool is a subnet.

A per-call failure (the tool raised, or no tool has that name) is *not* a
transition failure: it becomes that call's ``FunctionResponse`` with
``{"error": message, "exceptionType": type}``, so the model sees it and
decides how to recover, as in ADK's own function handling. A batch with no
calls *is* a transition failure: there is nothing to answer, and the model
turn a re-ask would send back would have no parts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .. import colours as C
from .._aio import on_loop
from .._spec import Action, Ctx, NetSpec, Port, TransitionSpec, one, out
from ._common import exception_type
from .actions import bind

NAME = "ToolDispatch"

ToolContextSupplier = Callable[[], "ToolContext | None"]


class Transitions:
    DISPATCH = f"{NAME}_Dispatch"


DEF = NetSpec(
    NAME,
    (TransitionSpec(Transitions.DISPATCH, (one(C.TOOL_CALLS),), out(C.TOOL_RESULTS)),),
    ports=(Port("toolCalls", "in", C.TOOL_CALLS), Port("toolResults", "out", C.TOOL_RESULTS)),
)


def model_turn_of(calls: tuple[types.FunctionCall, ...]) -> types.Content:
    """Fallback model turn for a batch whose producer did not carry the original."""
    return types.Content(role="model", parts=[types.Part(function_call=c) for c in calls])


def _response(call: types.FunctionCall, payload: dict[str, Any]) -> types.FunctionResponse:
    return types.FunctionResponse(id=call.id, name=call.name or "", response=payload)


async def dispatch_one(
    tools: Mapping[str, BaseTool], call: types.FunctionCall, tool_context: ToolContext | None
) -> types.FunctionResponse:
    name = call.name or ""
    tool = tools.get(name)
    if tool is None:
        return _response(call, {"error": f"unknown tool: '{name}'", "exceptionType": "ValueError"})
    try:
        result = await tool.run_async(args=dict(call.args or {}), tool_context=tool_context)  # type: ignore[arg-type]
    except Exception as err:  # per-call failure -> structured error response
        return _response(call, {"error": str(err), "exceptionType": exception_type(err)})
    return _response(call, result if isinstance(result, dict) else {"result": result})


def action_bindings(
    tools: Mapping[str, BaseTool],
    tool_context_supplier: ToolContextSupplier = lambda: None,
) -> dict[str, Action]:
    tools = dict(tools)

    async def dispatch(ctx: Ctx) -> None:
        batch = ctx.input(C.TOOL_CALLS)
        if not batch.calls:
            raise ValueError(f"{Transitions.DISPATCH} got a tool-call batch with no calls")
        model_turn = (
            batch.model_turn if batch.model_turn is not None else model_turn_of(batch.calls)
        )
        contexts = [tool_context_supplier() for _ in batch.calls]

        async def run_all() -> list[types.FunctionResponse]:
            return list(
                await asyncio.gather(
                    *(
                        dispatch_one(tools, c, tc)
                        for c, tc in zip(batch.calls, contexts, strict=True)
                    )
                )
            )

        responses = await on_loop(run_all())
        ctx.output(C.TOOL_RESULTS, C.ToolResults(tuple(responses), model_turn))

    return bind(DEF, {Transitions.DISPATCH: dispatch})
