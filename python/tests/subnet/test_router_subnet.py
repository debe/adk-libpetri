"""Port of ``RouterSubnetTest.java``."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import libpetri as lp
from google.adk.events.event import Event
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._spec import NetSpec
from adk_libpetri.subnet import router


@dataclass
class Fixture:
    events: list[Event]
    tool_calls: list[C.ToolCalls]
    transfers: list[C.TransferTarget]


def router_config(author: str) -> router.Config:
    return router.Config(author, lambda: "invocation-fixed")


def text_response(t: str) -> LlmResponse:
    return LlmResponse(content=types.Content(parts=[types.Part(text=t)]))


def response_with_calls(*calls: types.FunctionCall) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(function_call=c) for c in calls])
    )


def fc(
    name: str, args: dict[str, object] | None = None, id: str | None = None
) -> types.FunctionCall:
    return types.FunctionCall(name=name, args=args or {}, id=id)


async def run(config: router.Config, *responses: LlmResponse) -> Fixture:
    net = NetSpec.compose("test", router.DEF).build(router.action_bindings(config))
    marking = await lp.run_async(
        net, initial={C.LLM_RESPONSE.name: list(responses)}, event_store=lp.InMemoryEventStore()
    )
    return Fixture(
        list(marking.tokens(C.EVENT_OUT.name)),
        list(marking.tokens(C.TOOL_CALLS.name)),
        list(marking.tokens(C.TRANSFER.name)),
    )


# ============================================================
#  Text-only responses -> EVENT_OUT
# ============================================================


async def test_text_only_response_routes_to_event_out_wrapping_content() -> None:
    fixture = await run(router_config("test-agent"), text_response("hello world"))

    assert len(fixture.events) == 1
    assert fixture.tool_calls == []
    assert fixture.transfers == []

    event = fixture.events[0]
    assert event.author == "test-agent"
    assert event.invocation_id == "invocation-fixed"
    assert event.content is not None
    assert event.content.parts[0].text == "hello world"  # type: ignore[index]


async def test_empty_response_still_routes_to_event_out_with_null_content() -> None:
    fixture = await run(router_config("agent"), LlmResponse())

    assert len(fixture.events) == 1
    assert fixture.events[0].content is None


# ============================================================
#  Function calls -> TOOL_CALLS
# ============================================================


async def test_single_function_call_routes_to_tool_calls() -> None:
    call = fc("get_weather", {"city": "Oslo"}, "c1")
    fixture = await run(router_config("agent"), response_with_calls(call))

    assert fixture.events == []
    assert fixture.transfers == []
    assert len(fixture.tool_calls) == 1
    assert fixture.tool_calls[0].calls == (call,)


async def test_multiple_function_calls_bundle_into_one_tool_calls_token() -> None:
    c1, c2 = fc("a"), fc("b")
    fixture = await run(router_config("agent"), response_with_calls(c1, c2))

    assert len(fixture.tool_calls) == 1
    assert fixture.tool_calls[0].calls == (c1, c2)


# ============================================================
#  transfer_to_agent -> TRANSFER (precedence over other calls)
# ============================================================


async def test_transfer_to_agent_call_routes_to_transfer_place() -> None:
    transfer_call = fc(router.TRANSFER_TO_AGENT_FN, {router.TRANSFER_AGENT_NAME_ARG: "billing"})
    fixture = await run(router_config("router"), response_with_calls(transfer_call))

    assert fixture.tool_calls == []
    assert fixture.events == []
    assert len(fixture.transfers) == 1
    assert fixture.transfers[0].agent_name == "billing"


async def test_transfer_takes_precedence_over_sibling_function_calls() -> None:
    transfer_call = fc(router.TRANSFER_TO_AGENT_FN, {router.TRANSFER_AGENT_NAME_ARG: "sales"})
    sibling = fc("other_tool")
    fixture = await run(router_config("router"), response_with_calls(sibling, transfer_call))

    assert len(fixture.transfers) == 1
    assert fixture.transfers[0].agent_name == "sales"
    assert fixture.tool_calls == []


async def test_transfer_with_missing_agent_name_arg_yields_empty_string() -> None:
    bad = fc(router.TRANSFER_TO_AGENT_FN, {})
    fixture = await run(router_config("router"), response_with_calls(bad))

    assert len(fixture.transfers) == 1
    assert fixture.transfers[0].agent_name == ""


# ============================================================
#  Structural -- XOR enforcement and topology
# ============================================================


async def test_each_fire_produces_to_exactly_one_xor_child() -> None:
    with_call = response_with_calls(fc("t"))
    with_xfer = response_with_calls(
        fc(router.TRANSFER_TO_AGENT_FN, {router.TRANSFER_AGENT_NAME_ARG: "x"})
    )
    fixture = await run(router_config("agent"), text_response("hi"), with_call, with_xfer)

    assert len(fixture.events) == 1
    assert len(fixture.tool_calls) == 1
    assert len(fixture.transfers) == 1


def test_subnet_def_declares_one_transition_and_four_ports() -> None:
    assert list(router.DEF.transition_names) == [router.Transitions.ROUTE]
    assert sorted(p.name for p in router.DEF.ports) == [
        "eventOut",
        "llmResponse",
        "toolCalls",
        "transfer",
    ]


async def test_invocation_id_supplier_called_per_event_emission() -> None:
    counter = itertools.count(1)
    config = router.Config("agent", lambda: f"inv-{next(counter)}")

    fixture = await run(config, text_response("a"), text_response("b"))

    assert len(fixture.events) == 2
    assert sorted(e.invocation_id for e in fixture.events) == ["inv-1", "inv-2"]
