"""Port of ``ToolDispatchSubnetTest.java``."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import libpetri as lp
import pytest
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._spec import NetSpec
from adk_libpetri.subnet import tool_dispatch

ToolImpl = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


class FakeTool(BaseTool):
    """A ``BaseTool`` whose ``run_async`` delegates to ``impl`` (sync or async)."""

    def __init__(self, name: str, impl: ToolImpl) -> None:
        super().__init__(name=name, description="test")
        self._impl = impl

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        result = self._impl(args)
        if asyncio.iscoroutine(result):
            result = await result
        return result


def fake_tool(name: str, impl: ToolImpl) -> BaseTool:
    return FakeTool(name, impl)


def throwing_tool(name: str, to_throw: Exception) -> BaseTool:
    def impl(args: dict[str, Any]) -> dict[str, Any]:
        raise to_throw

    return FakeTool(name, impl)


@dataclass
class Fixture:
    responses: list[types.FunctionResponse]
    events: list[lp.NetEvent]

    def failures(self) -> list[lp.NetEvent]:
        return [e for e in self.events if e.type == "TransitionFailed"]


async def run(
    tools: Mapping[str, BaseTool],
    calls: C.ToolCalls,
    supplier: tool_dispatch.ToolContextSupplier = lambda: None,
) -> Fixture:
    net = NetSpec.compose("test-net", tool_dispatch.DEF).build(
        tool_dispatch.action_bindings(tools, supplier)
    )
    store = lp.InMemoryEventStore()
    marking = await lp.run_async(net, initial={C.TOOL_CALLS.name: [calls]}, event_store=store)
    responses = [r for batch in marking.tokens(C.TOOL_RESULTS.name) for r in batch.results]
    return Fixture(responses, list(store.events()))


def call_batch(*calls: types.FunctionCall) -> C.ToolCalls:
    return C.ToolCalls(calls)


def call(name: str, args: dict[str, Any], id: str) -> types.FunctionCall:
    return types.FunctionCall(name=name, args=args, id=id)


# ============================================================
#  Happy paths
# ============================================================


async def test_single_tool_call_produces_single_response() -> None:
    weather = fake_tool("get_weather", lambda args: {"temp": f"{args['city']}-warm"})

    fixture = await run(
        {"get_weather": weather}, call_batch(call("get_weather", {"city": "Berlin"}, "c-1"))
    )

    assert len(fixture.responses) == 1
    resp = fixture.responses[0]
    assert resp.name == "get_weather"
    assert resp.id == "c-1"
    assert resp.response == {"temp": "Berlin-warm"}


async def test_two_tool_calls_both_dispatched_and_collected() -> None:
    calc = fake_tool("calculate", lambda args: {"answer": 42})
    weather = fake_tool("get_weather", lambda args: {"temp": "cold"})

    fixture = await run(
        {"calculate": calc, "get_weather": weather},
        call_batch(
            call("calculate", {"expr": "6*7"}, "c-a"),
            call("get_weather", {"city": "Oslo"}, "c-b"),
        ),
    )

    assert len(fixture.responses) == 2
    # Order is preserved (matches input call order).
    first, second = fixture.responses
    assert (first.name, first.id, first.response) == ("calculate", "c-a", {"answer": 42})
    assert (second.name, second.id, second.response) == ("get_weather", "c-b", {"temp": "cold"})


async def test_an_empty_call_batch_fails_the_firing_and_produces_no_results() -> None:
    """A batch with no calls fails the firing and produces nothing: a model
    turn rebuilt from no calls would have zero parts."""
    fixture = await run({}, call_batch())

    assert fixture.responses == []
    assert [e.transition_name for e in fixture.failures()] == [tool_dispatch.Transitions.DISPATCH]


def test_tool_results_require_the_model_turn() -> None:
    """Results always carry the model turn: the re-ask cannot rebuild it from them."""
    # Java NullPointerException -> Python TypeError from ToolResults.__post_init__.
    with pytest.raises(TypeError):
        C.ToolResults((), None)  # type: ignore[arg-type]


# ============================================================
#  Error handling -- each per-call failure is isolated to that
#  call's FunctionResponse; sibling calls still succeed.
# ============================================================


async def test_unknown_tool_name_produces_error_response() -> None:
    fixture = await run({}, call_batch(call("nonexistent_tool", {}, "c-x")))

    assert len(fixture.responses) == 1
    resp = fixture.responses[0]
    assert resp.name == "nonexistent_tool"
    assert resp.id == "c-x"
    payload = resp.response
    assert payload is not None
    assert "unknown tool" in payload["error"]
    # Java IllegalArgumentException -> ValueError.
    assert payload["exceptionType"] == "ValueError"


async def test_tool_runtime_error_is_captured_in_response_not_transition_failure() -> None:
    broken = throwing_tool("broken", RuntimeError("network down"))

    fixture = await run({"broken": broken}, call_batch(call("broken", {}, "c-1")))

    assert len(fixture.responses) == 1
    resp = fixture.responses[0]
    assert resp.name == "broken"
    assert resp.response == {"error": "network down", "exceptionType": "RuntimeError"}

    # Critical: the *transition* did not fail -- the dispatch must always
    # produce a TOOL_RESULTS token even when individual tools fail.
    assert fixture.failures() == []


async def test_one_tool_succeeds_one_fails_results_carries_both() -> None:
    ok = fake_tool("ok", lambda args: {"status": "good"})
    # Java IllegalStateException has no builtin twin; LookupError keeps the type distinct.
    bad = throwing_tool("bad", LookupError("oops"))

    fixture = await run(
        {"ok": ok, "bad": bad},
        call_batch(call("ok", {}, "c-good"), call("bad", {}, "c-bad")),
    )

    assert len(fixture.responses) == 2
    assert fixture.responses[0].response == {"status": "good"}
    assert fixture.responses[1].response == {"error": "oops", "exceptionType": "LookupError"}


# ============================================================
#  Concurrency -- two tools each wait for the other to start; if
#  the dispatcher were sequential, the first would time out
#  waiting for its sibling.
# ============================================================


async def test_tools_fire_concurrently_not_sequentially() -> None:
    # Java CountDownLatch over virtual threads -> asyncio.Barrier on the loop the tools run on.
    both_started = asyncio.Barrier(2)

    async def wait_for_sibling(args: dict[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.wait_for(both_started.wait(), 2)
        except TimeoutError as err:
            raise AssertionError("sibling never started -- sequential dispatch") from err
        return {"name": args["__id"]}

    fixture = await run(
        {"a": fake_tool("a", wait_for_sibling), "b": fake_tool("b", wait_for_sibling)},
        call_batch(call("a", {"__id": "A"}, "c-A"), call("b", {"__id": "B"}, "c-B")),
    )

    assert len(fixture.responses) == 2
    assert fixture.responses[0].response == {"name": "A"}
    assert fixture.responses[1].response == {"name": "B"}


# ============================================================
#  ToolContext threading
# ============================================================


async def test_tool_receives_supplied_tool_context() -> None:
    captured: list[Any] = []

    class CapturingTool(BaseTool):
        async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
            captured.append(tool_context)
            return {}

    # A real non-None instance, so the pass-through is exercised (Mockito mock -> sentinel).
    sentinel = object()
    fixture = await run(
        {"capture": CapturingTool(name="capture", description="captures context")},
        call_batch(call("capture", {}, "c-1")),
        supplier=lambda: sentinel,  # type: ignore[arg-type,return-value]
    )

    assert len(fixture.responses) == 1
    assert captured[0] is sentinel


# ============================================================
#  Subnet shape + binding validation
# ============================================================


def test_subnet_def_declares_exactly_one_transition_and_two_ports() -> None:
    assert list(tool_dispatch.DEF.transition_names) == [tool_dispatch.Transitions.DISPATCH]
    assert sorted(p.name for p in tool_dispatch.DEF.ports) == ["toolCalls", "toolResults"]


def test_composed_subnet_includes_dispatch_transition_and_both_boundary_places() -> None:
    net = NetSpec.compose("test", tool_dispatch.DEF).build(tool_dispatch.action_bindings({}))

    assert tool_dispatch.Transitions.DISPATCH in [t.name for t in net.transitions]
    assert {C.TOOL_CALLS.name, C.TOOL_RESULTS.name} <= {p.name for p in net.places}
