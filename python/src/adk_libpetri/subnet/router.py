"""Stock subnet routing an ``LlmResponse`` to tool calls, a transfer or an answer.

    [LLM_RESPONSE] --Router_Route--> xor([TOOL_CALLS], [TRANSFER], [EVENT_OUT])

First match wins:

1. a ``transfer_to_agent`` function call -> ``TransferTarget(agent_name)``;
2. any other function calls -> one ``ToolCalls`` bundle (with the model turn);
3. otherwise -> a final ``Event`` carrying the response content.

Transfer takes precedence over other calls in the same response; the route is
structurally exclusive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from google.adk.events.event import Event
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .. import colours as C
from .._spec import Action, Ctx, NetSpec, Place, Port, TransitionSpec, one, xor
from ._common import IdSupplier, random_id
from .actions import bind

NAME = "Router"
TRANSFER_TO_AGENT_FN = "transfer_to_agent"
TRANSFER_AGENT_NAME_ARG = "agent_name"


class Transitions:
    ROUTE = f"{NAME}_Route"


@dataclass(frozen=True)
class Config:
    author: str
    invocation_id_supplier: IdSupplier = field(default=random_id)


def route_transition(transfer: Place[C.TransferTarget], answer: Place[Event]) -> TransitionSpec:
    """The ``Route`` transition aimed at ``transfer`` and ``answer``.

    ``LlmAgentSubnet`` aims them at places of its own so the transitions that
    end its turn hand the permit back as they emit; :data:`DEF` aims them at
    the boundary places.
    """
    return TransitionSpec(
        Transitions.ROUTE, (one(C.LLM_RESPONSE),), xor(C.TOOL_CALLS, transfer, answer)
    )


DEF = NetSpec(
    NAME,
    (route_transition(C.TRANSFER, C.EVENT_OUT),),
    ports=(
        Port("llmResponse", "in", C.LLM_RESPONSE),
        Port("toolCalls", "out", C.TOOL_CALLS),
        Port("transfer", "out", C.TRANSFER),
        Port("eventOut", "out", C.EVENT_OUT),
    ),
)


def function_calls(response: LlmResponse) -> list[types.FunctionCall]:
    content = response.content
    if content is None or not content.parts:
        return []
    return [p.function_call for p in content.parts if p.function_call is not None]


def route_action(config: Config, transfer: Place[C.TransferTarget], answer: Place[Event]) -> Action:
    def route(ctx: Ctx) -> None:
        response = ctx.input(C.LLM_RESPONSE)
        calls = function_calls(response)
        transfer_call = next((c for c in calls if c.name == TRANSFER_TO_AGENT_FN), None)
        if transfer_call is not None:
            name = (transfer_call.args or {}).get(TRANSFER_AGENT_NAME_ARG)
            ctx.output(transfer, C.TransferTarget("" if name is None else str(name)))
        elif calls:
            ctx.output(C.TOOL_CALLS, C.ToolCalls(tuple(calls), response.content))
        else:
            ctx.output(
                answer,
                Event(
                    invocation_id=config.invocation_id_supplier(),
                    author=config.author,
                    content=response.content,
                ),
            )

    return route


def action_bindings(config: Config) -> dict[str, Action]:
    return bind(DEF, {Transitions.ROUTE: route_action(config, C.TRANSFER, C.EVENT_OUT)})
