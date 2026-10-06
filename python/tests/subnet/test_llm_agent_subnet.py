"""Port of Java ``LlmAgentSubnetTest``.

Java's ``dispatchExecutor`` has no Python equivalent (tools run on the asyncio
loop), so the configs here omit it. Java drives the long-lived cases through
``PetriRunner``; the Python ``runner`` package has no runner yet, so those
cases drive ``lp.start_async`` directly with the same env places and inject
``TURN_ABORT`` the way ``PetriAgent`` signals it.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

import libpetri as lp
import pytest
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.genai import types
from pydantic import PrivateAttr

from adk_libpetri import colours as C
from adk_libpetri.subnet import llm_agent as LA
from adk_libpetri.subnet import llm_step, router, tool_dispatch
from support.fake_llm import ScriptedLlm, call, text, transfer

ENV = lp.ExecutorOptions(environment_places=(C.USER_IN.name, C.TURN_ABORT.name))


# ============================================================
#  Hello-world: text-only response, single turn
# ============================================================


async def test_single_turn_text_response_lands_on_event_out() -> None:
    llm = ScriptedLlm.of(text("Hi back!"))
    config = LA.Config(
        name="hello", model="fake-model", system_instruction="Greet the user warmly."
    )

    fixture = await run(llm, config, user_message("hi"))

    assert len(fixture.events) == 1
    assert text_of(fixture.events[0]) == "Hi back!"
    assert fixture.events[0].author == "hello"

    # No tool calls, no transfer.
    assert fixture.tool_calls == []
    assert fixture.transfers == []

    # Sanity on fired transitions: BuildPrompt + LlmStep pipeline + Router; no
    # ReAsk and no fallback.
    fired = fixture.fired_transition_names()
    assert {
        LA.Transitions.BUILD_PROMPT,
        llm_step.Transitions.BEFORE_MODEL,
        llm_step.Transitions.LLM_CALL,
        llm_step.Transitions.AFTER_MODEL,
        router.Transitions.ROUTE,
    } <= set(fired)
    assert LA.Transitions.RE_ASK not in fired
    assert LA.Transitions.RE_ASK_EXHAUSTED_FALLBACK not in fired


# ============================================================
#  Tool-call -> tool-result -> re-ask -> text (two turns)
# ============================================================


async def test_tool_call_then_text_response_two_llm_turns() -> None:
    calc = FakeTool("calculate", lambda args: {"answer": 42})
    llm = ScriptedLlm.of(
        call("calculate", {"expr": "6*7"}, "c1"),
        text("The answer is 42."),
    )
    config = LA.Config(
        name="calc-agent", model="fake-model", tools={"calculate": calc}, reask_budget=3
    )

    fixture = await run(llm, config, user_message("calc 6*7"))

    assert len(fixture.events) == 1
    assert text_of(fixture.events[0]) == "The answer is 42."
    fired = fixture.fired_transition_names()
    assert LA.Transitions.RE_ASK in fired
    assert tool_dispatch.Transitions.DISPATCH in fired
    # The pipeline fired LlmCall twice.
    assert fired.count(llm_step.Transitions.LLM_CALL) == 2


async def test_reask_requests_carry_the_full_conversation_including_the_model_call_turn() -> None:
    """Each re-ask must carry the whole invocation, not the tool responses alone:
    the user turn, then every model function-call turn verbatim (Gemini 3 rejects
    a call turn stripped of its thought signature) followed by its
    function-response turn."""
    calc = FakeTool("calculate", lambda args: {"answer": 42})
    signature = bytes([7, 7, 7])
    first_call = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    thought_signature=signature,
                    function_call=types.FunctionCall(
                        name="calculate", args={"expr": "6*7"}, id="c1"
                    ),
                )
            ],
        )
    )
    second_call = call("calculate", {"expr": "42+0"}, "c2")
    llm = ScriptedLlm.of(first_call, second_call, text("42."))
    config = LA.Config(
        name="calc-agent", model="fake-model", tools={"calculate": calc}, reask_budget=3
    )

    user = user_message("calc 6*7")
    fixture = await run(llm, config, user)

    assert len(fixture.events) == 1
    requests = llm.requests
    assert len(requests) == 3
    assert requests[0].contents == [user]

    first_re_ask = requests[1].contents
    assert len(first_re_ask) == 3
    assert first_re_ask[0] == user
    # The model turn goes back verbatim, signature and all.
    assert first_re_ask[1] == first_call.content
    assert first_re_ask[1].parts[0].thought_signature == signature
    assert first_re_ask[2].role == "user"
    assert first_re_ask[2].parts[0].function_response.id == "c1"

    # The second hop keeps accumulating rather than starting over.
    second_re_ask = requests[2].contents
    assert len(second_re_ask) == 5
    assert second_re_ask[:3] == first_re_ask
    assert second_re_ask[3] == second_call.content
    assert second_re_ask[4].parts[0].function_response.id == "c2"


# ============================================================
#  Reask budget exhaustion -> canned fallback
# ============================================================


async def test_reask_budget_exhaustion_emits_fallback_event_not_indefinite_loop() -> None:
    tool = FakeTool("alwaysCall", lambda args: {"ok": True})
    # LLM always returns a tool call -> would loop forever without the budget.
    llm = EndlesslyToolCallingLlm.calling("alwaysCall")

    fallback = types.Content(parts=[types.Part(text="budget out — sorry")])
    config = LA.Config(
        name="looper",
        model="fake-model",
        tools={"alwaysCall": tool},
        reask_budget=2,
        fallback_content=fallback,
    )

    fixture = await run(llm, config, user_message("loop please"))

    assert len(fixture.events) == 1
    assert text_of(fixture.events[0]) == "budget out — sorry"
    assert fixture.events[0].author == "looper"

    # Budget=2 -> 2 re-asks possible -> LLM fires 3 times total (initial + 2).
    fired = fixture.fired_transition_names()
    assert fired.count(llm_step.Transitions.LLM_CALL) == 3
    assert fired.count(LA.Transitions.RE_ASK) == 2
    assert LA.Transitions.RE_ASK_EXHAUSTED_FALLBACK in fired


async def test_reask_budget_one_means_one_reask_then_fallback() -> None:
    tool = FakeTool("c", lambda args: {})
    llm = EndlesslyToolCallingLlm.calling("c")
    config = LA.Config(name="one-shot", model="fake-model", tools={"c": tool}, reask_budget=1)

    fixture = await run(llm, config, user_message("go"))

    fired = fixture.fired_transition_names()
    assert fired.count(LA.Transitions.RE_ASK) == 1
    assert LA.Transitions.RE_ASK_EXHAUSTED_FALLBACK in fired


# ============================================================
#  Reask-budget reset on a new user message
# ============================================================


async def test_reask_budget_is_reset_on_each_new_user_input() -> None:
    # Two user messages in the same execution. Each should get a fresh budget.
    tool = FakeTool("t", lambda args: {"v": 1})
    llm = ScriptedLlm.of(
        call("t"),
        text("first done"),
        call("t"),
        text("second done"),
    )
    config = LA.Config(name="agent", model="fake-model", tools={"t": tool}, reask_budget=3)

    fixture = await run(llm, config, user_message("msg one"), user_message("msg two"))

    assert len(fixture.events) == 2
    assert text_of(fixture.events[0]) == "first done"
    assert text_of(fixture.events[1]) == "second done"
    # No fallback fired.
    assert LA.Transitions.RE_ASK_EXHAUSTED_FALLBACK not in fixture.fired_transition_names()


# ============================================================
#  Transfer routing (LLM emits transfer_to_agent) — token lands on TRANSFER port
# ============================================================


async def test_transfer_to_agent_routes_to_transfer_output_port() -> None:
    assert router.TRANSFER_TO_AGENT_FN == "transfer_to_agent"
    assert router.TRANSFER_AGENT_NAME_ARG == "agent_name"
    llm = ScriptedLlm.of(transfer("specialist"))
    config = LA.Config(name="router-agent", model="fake-model")

    fixture = await run(llm, config, user_message("send me to the specialist"))

    assert len(fixture.transfers) == 1
    assert fixture.transfers[0].agent_name == "specialist"
    assert fixture.events == []
    assert fixture.tool_calls == []


# ============================================================
#  Interface shape — public agent subnet ports
# ============================================================


def test_subnet_interface_exposes_public_agent_ports() -> None:
    port_names = sorted(p.name for p in LA.DEF.ports)
    assert port_names == ["eventOut", "transfer", "turnAbort", "userIn"]


def test_subnet_def_contains_owned_and_composed_transitions() -> None:
    names = set(LA.DEF.transition_names)
    assert {
        # owned
        LA.Transitions.START_TURN,
        LA.Transitions.BUILD_PROMPT,
        LA.Transitions.RE_ASK,
        LA.Transitions.RE_ASK_EXHAUSTED_FALLBACK,
        LA.Transitions.EMIT_ANSWER,
        LA.Transitions.EMIT_TRANSFER,
        LA.Transitions.ABORT_TURN,
        LA.Transitions.DROP_ABORT,
        # from LlmStepSubnet
        llm_step.Transitions.BEFORE_MODEL,
        llm_step.Transitions.LLM_CALL,
        llm_step.Transitions.AFTER_MODEL,
        llm_step.Transitions.ON_MODEL_ERROR,
        # from RouterSubnet
        router.Transitions.ROUTE,
        # from ToolDispatchSubnet
        tool_dispatch.Transitions.DISPATCH,
    } <= names


# ============================================================
#  Config validation
# ============================================================


def test_config_invalid_reask_budget_zero_throws() -> None:
    with pytest.raises(ValueError, match="reask_budget must be >= 1"):
        LA.Config(name="x", model="y", reask_budget=0)


# ============================================================
#  Turn permit — one turn at a time, every end returns the permit
# ============================================================


async def test_every_way_a_turn_ends_leaves_only_the_permit_behind() -> None:
    """Each way a turn ends (answer, tool loop then answer, reask-exhausted
    fallback, transfer) gives the permit back and leaves nothing else of the
    turn behind: no conversation, no unspent budget."""
    tool = FakeTool("t", lambda args: {"v": 1})
    config = LA.Config(name="agent", model="fake-model", tools={"t": tool}, reask_budget=2)
    only_the_permit = {C.TURN_PERMIT.name: 1}

    answered = await run(ScriptedLlm.of(text("hi")), config, user_message("a"))
    assert answered.resting == only_the_permit
    looped = await run(ScriptedLlm.of(call("t"), text("done")), config, user_message("b"))
    assert looped.resting == only_the_permit
    exhausted = await run(EndlesslyToolCallingLlm.calling("t"), config, user_message("c"))
    assert exhausted.resting == only_the_permit
    handed_off = await run(ScriptedLlm.of(transfer("specialist")), config, user_message("d"))
    assert len(handed_off.transfers) == 1
    assert handed_off.resting == only_the_permit


async def test_an_input_that_arrives_mid_turn_waits_for_that_turn_to_end() -> None:
    """Replays the marking the overlapping-turn bug started from: turn 1 is in
    its tool loop, holding the permit, with its tool results, one budget token
    and its conversation on the places, and turn 2's input has just arrived.
    Turn 2 waits for the permit: turn 1 re-asks with its own conversation and
    answers, then turn 2 starts with nothing but its own input."""
    llm = ScriptedLlm.of(text("turn-1 answer"), text("turn-2 answer"))
    config = LA.Config(name="agent", model="fake-model", reask_budget=2)
    net = LA.DEF.build(LA.action_bindings(llm, config))

    user1 = user_message("turn-1 user")
    user2 = user_message("turn-2 user")
    call1 = types.Content(
        role="model",
        parts=[types.Part(function_call=types.FunctionCall(name="t1", id="c1"))],
    )
    results1 = C.ToolResults(
        (types.FunctionResponse(name="t1", id="c1", response={"r": "turn-1 result"}),), call1
    )

    mid_turn_one = {
        C.USER_IN.name: [user2],
        C.TOOL_RESULTS.name: [results1],
        LA.REASK_BUDGET.name: [None],
        LA.CONVERSATION.name: [LA.Conversation((user1,))],
        LA.TURN_ACTIVE.name: [None],
    }
    marking = await lp.run_async(net, initial=mid_turn_one)

    requests = llm.requests
    assert len(requests) == 2
    turn_one_re_ask = requests[0].contents
    assert len(turn_one_re_ask) == 3
    assert turn_one_re_ask[0] == user1
    assert turn_one_re_ask[1] == call1
    assert requests[1].contents == [user2]

    answers = [text_of(e) for e in marking.tokens(C.EVENT_OUT.name)]
    assert answers == ["turn-1 answer", "turn-2 answer"]
    assert resting_tokens(marking, C.EVENT_OUT.name) == {C.TURN_PERMIT.name: 1}


async def test_overlapping_turns_on_a_running_net_each_keep_their_own_conversation() -> None:
    """The same overlap, live: turn 2's input is injected while turn 1's tool
    call is still running. It waits for turn 1 to answer, and its request
    carries only its own conversation."""
    # asyncio.Event, not a latch: the tool coroutine runs on this test's loop.
    tool_entered = asyncio.Event()
    release_tool = asyncio.Event()

    async def slow_impl(args: dict[str, Any]) -> dict[str, Any]:
        tool_entered.set()
        await release_tool.wait()
        return {"ok": True}

    slow = FakeTool("slow", slow_impl)
    llm = ScriptedLlm.of(call("slow", call_id="s1"), text("turn-1 answer"), text("turn-2 answer"))
    config = LA.Config(name="agent", model="fake-model", tools={"slow": slow})
    net = LA.DEF.build(LA.action_bindings(llm, config))

    user1 = user_message("turn-1 user")
    user2 = user_message("turn-2 user")
    handle, done = lp.start_async(net, initial={C.TURN_PERMIT.name: [None]}, options=ENV)
    try:
        assert handle.inject(C.USER_IN.name, user1)
        await asyncio.wait_for(tool_entered.wait(), 5)
        assert handle.inject(C.USER_IN.name, user2)
        release_tool.set()

        await until(lambda m: m.count(C.EVENT_OUT.name) >= 2, handle)
        marking = (await handle.snapshot()).marking
        assert [text_of(e) for e in marking.tokens(C.EVENT_OUT.name)] == [
            "turn-1 answer",
            "turn-2 answer",
        ]
    finally:
        handle.drain()
        await done

    requests = llm.requests
    assert len(requests) == 3
    assert requests[0].contents == [user1]
    assert len(requests[1].contents) == 3
    assert requests[1].contents[0] == user1
    assert requests[2].contents == [user2]


async def test_a_turn_abort_clears_a_failed_turn_and_the_next_input_runs() -> None:
    """A model error with no recovery callback fails its transition, which
    consumes its inputs and produces nothing, so the turn holds the permit with
    nothing left to end it. A TURN_ABORT clears the turn and returns the
    permit, and the next input runs with a fresh conversation."""
    llm = ScriptedLlm.of(RuntimeError("model exploded"), text("recovered"))
    config = LA.Config(name="agent", model="fake-model")

    await assert_abort_recovers(llm, config, llm_step.Transitions.ON_MODEL_ERROR)


async def test_a_turn_abort_clears_a_turn_that_failed_in_its_tool_loop() -> None:
    """The same recovery from a failure in the middle of the tool loop, where
    the turn has a conversation and budget on the places: the tool context
    supplier throws, so dispatch fails. The abort resets both, and the next
    turn starts with its own."""
    llm = ScriptedLlm.of(call("t", call_id="c1"), text("recovered"))
    tool = FakeTool("t", lambda args: {})
    dispatches = itertools.count()

    def tool_context() -> Any:
        if next(dispatches) == 0:
            raise RuntimeError("no tool context")
        return None

    config = LA.Config(
        name="agent", model="fake-model", tools={"t": tool}, tool_context_supplier=tool_context
    )

    await assert_abort_recovers(llm, config, tool_dispatch.Transitions.DISPATCH)


async def test_a_turn_abort_with_no_turn_in_flight_is_dropped() -> None:
    """An abort with no turn in flight is dropped, not kept for the next turn
    and not turned into a second permit."""
    llm = ScriptedLlm.of(text("fine"))
    config = LA.Config(name="agent", model="fake-model")
    net = LA.DEF.build(LA.action_bindings(llm, config))

    handle, done = lp.start_async(net, initial={C.TURN_PERMIT.name: [None]}, options=ENV)
    try:
        assert handle.inject(C.TURN_ABORT.name, None)
        assert handle.inject(C.USER_IN.name, user_message("go"))
        marking = await until(lambda m: m.count(C.EVENT_OUT.name) >= 1, handle)
        assert text_of(marking.tokens(C.EVENT_OUT.name)[0]) == "fine"
        await await_resting(handle, {C.TURN_PERMIT.name: 1})
    finally:
        handle.drain()
        await done


async def test_a_stale_turn_abort_in_the_same_pass_as_an_input_does_not_abort_it() -> None:
    """A stale abort that lands in the same pass as an input, with the permit at
    rest, is dropped before the input starts its turn."""
    llm = ScriptedLlm.of(text("fine"))
    config = LA.Config(name="agent", model="fake-model")
    net = LA.DEF.build(LA.action_bindings(llm, config))
    initial = {
        C.USER_IN.name: [user_message("go")],
        C.TURN_ABORT.name: [None],
        C.TURN_PERMIT.name: [None],
    }

    marking = await lp.run_async(net, initial=initial)

    answers = marking.tokens(C.EVENT_OUT.name)
    assert len(answers) == 1
    assert text_of(answers[0]) == "fine"
    assert resting_tokens(marking, C.EVENT_OUT.name) == {C.TURN_PERMIT.name: 1}


async def test_an_instantiated_agent_needs_its_prefixed_permit_seeded() -> None:
    """An agent composed through ``instantiate(prefix)`` has its own prefixed
    permit, which the host does not seed; one that seeds it runs."""
    config = LA.Config(name="agent", model="fake-model")
    # Bound before instantiate, not via Instance.bind_actions: the stock actions
    # take a typed Ctx, which only NetSpec.build / subnet_def wrap them into.
    agent = LA.DEF.subnet_def(LA.action_bindings(ScriptedLlm.of(text("fine")), config)).instantiate(
        "billing"
    )
    net = (
        lp.NetBuilder("test-host")
        .compose_instance(
            agent,
            {
                "userIn": lp.Place(C.USER_IN.name),
                "turnAbort": lp.Place(C.TURN_ABORT.name),
                "eventOut": lp.Place(C.EVENT_OUT.name),
                "transfer": lp.Place(C.TRANSFER.name),
            },
        )
        .build()
    )
    permit = f"billing/{C.TURN_PERMIT.name}"
    assert permit in {p.name for p in net.places}

    # Java asserts PetriRunner.start() fails loudly on the unseeded permit;
    # Python has no PetriRunner yet, so assert the failure mode it guards
    # against instead: a bare unseeded run never starts the turn.
    unseeded = await lp.run_async(net, initial={C.USER_IN.name: [user_message("go")]})
    assert unseeded.count(C.USER_IN.name) == 1
    assert unseeded.count(C.EVENT_OUT.name) == 0

    handle, done = lp.start_async(net, initial={permit: [None]}, options=ENV)
    try:
        assert handle.inject(C.USER_IN.name, user_message("go"))
        marking = await until(lambda m: m.count(C.EVENT_OUT.name) >= 1, handle)
        assert text_of(marking.tokens(C.EVENT_OUT.name)[0]) == "fine"
        await await_resting(handle, {permit: 1})
    finally:
        handle.drain()
        await done


async def assert_abort_recovers(
    llm: ScriptedLlm, config: LA.Config, failing_transition: str
) -> None:
    """Drives one failing turn and one recovering turn on a running net,
    signalling the abort the way ``PetriAgent`` does: on the failure."""
    net = LA.DEF.build(LA.action_bindings(llm, config))
    user1 = user_message("fails")
    user2 = user_message("recovers")
    store = lp.InMemoryEventStore()
    handle, done = lp.start_async(
        net, initial={C.TURN_PERMIT.name: [None]}, options=ENV, event_store=store
    )
    try:
        assert handle.inject(C.USER_IN.name, user1)
        await until_true(lambda: len(store.failures()) >= 1)
        assert store.failures()[0].transition_name == failing_transition

        # Wedged: the failed turn still holds the permit, so this input queues.
        assert handle.inject(C.USER_IN.name, user2)
        wedged = (await handle.snapshot()).marking
        assert wedged.count(C.USER_IN.name) == 1
        assert wedged.count(C.TURN_PERMIT.name) == 0

        assert handle.inject(C.TURN_ABORT.name, None)
        marking = await until(lambda m: m.count(C.EVENT_OUT.name) >= 1, handle)
        assert text_of(marking.tokens(C.EVENT_OUT.name)[0]) == "recovered"
        await await_resting(handle, {C.TURN_PERMIT.name: 1})
    finally:
        handle.drain()
        await done
    assert llm.requests[-1].contents == [user2]


# ============================================================
#  Fixtures + helpers
# ============================================================


@dataclass(frozen=True)
class Fixture:
    events: list[Event]
    tool_calls: list[C.ToolCalls]
    transfers: list[C.TransferTarget]
    net_events: list[Any]
    resting: dict[str, int]

    def fired_transition_names(self) -> list[str]:
        return [e.transition_name for e in self.net_events if e.type == "TransitionStarted"]


async def run(llm: BaseLlm, config: LA.Config, *user_messages: types.Content) -> Fixture:
    net = LA.DEF.build(LA.action_bindings(llm, config))
    # A bare executor seeds the turn permit itself; PetriRunner would.
    initial = {C.USER_IN.name: list(user_messages), C.TURN_PERMIT.name: [None]}
    store = lp.InMemoryEventStore()
    marking = await lp.run_async(net, initial=initial, event_store=store)
    return Fixture(
        list(marking.tokens(C.EVENT_OUT.name)),
        list(marking.tokens(C.TOOL_CALLS.name)),
        list(marking.tokens(C.TRANSFER.name)),
        list(store.events()),
        resting_tokens(marking, C.EVENT_OUT.name, C.TRANSFER.name),
    )


def resting_tokens(marking: lp.MarkingView, *egress: str) -> dict[str, int]:
    """Token count per place name, for every marked place but the egress ones."""
    return {
        p: marking.count(p) for p in marking.places() if marking.count(p) > 0 and p not in egress
    }


async def until_true(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


async def until(
    pred: Callable[[lp.MarkingView], bool], handle: lp.ExecutorHandle, timeout: float = 5.0
) -> lp.MarkingView:
    """Poll the running net's marking until ``pred`` holds; return that marking."""
    async with asyncio.timeout(timeout):
        while True:
            marking = (await handle.snapshot()).marking
            if pred(marking):
                return marking
            await asyncio.sleep(0.01)


async def await_resting(handle: lp.ExecutorHandle, expected: dict[str, int]) -> None:
    """Waits until the running net's marking, egress aside, is ``expected``."""
    resting: dict[str, int] = {}
    for _ in range(500):
        resting = resting_tokens((await handle.snapshot()).marking, C.EVENT_OUT.name)
        if resting == expected:
            return
        await asyncio.sleep(0.01)
    assert resting == expected


def user_message(t: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=t)])


def text_of(event: Event) -> str:
    assert event.content is not None
    return "".join(p.text for p in event.content.parts or [] if p.text)


class EndlesslyToolCallingLlm(BaseLlm):
    """Always answers with a call to ``tool_name``."""

    model: str = "looper"
    _tool: str = PrivateAttr(default="")
    _counter: int = PrivateAttr(default=0)

    @classmethod
    def calling(cls, tool_name: str) -> EndlesslyToolCallingLlm:
        llm = cls()
        llm._tool = tool_name
        return llm

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._counter += 1
        yield call(self._tool, {"attempt": self._counter})


class FakeTool(BaseTool):
    """A ``BaseTool`` delegating ``run_async`` to ``impl`` (sync or async)."""

    def __init__(self, name: str, impl: Callable[[dict[str, Any]], Any]) -> None:
        super().__init__(name=name, description="test")
        self._impl = impl
        self.contexts: list[Any] = []

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        self.contexts.append(tool_context)
        result = self._impl(args)
        if asyncio.iscoroutine(result):
            result = await result
        return result


async def test_the_composite_forwards_the_configured_tool_context_supplier() -> None:
    """The composite must forward the tool_context_supplier it is given."""
    marker = object()
    config = LA.Config(name="agent", model="fake-model", tool_context_supplier=lambda: marker)

    assert config.tool_context_supplier() is marker
    # Default stays the documented lambda: None rather than becoming required.
    plain = LA.Config(name="agent", model="fake-model")
    assert plain.tool_context_supplier() is None
    assert plain.callbacks == llm_step.Callbacks.none()

    # And it reaches the tool through the composed net, not just the config.
    tool = FakeTool("t", lambda args: {})
    forwarding = LA.Config(
        name="agent", model="fake-model", tools={"t": tool}, tool_context_supplier=lambda: marker
    )
    await run(ScriptedLlm.of(call("t"), text("done")), forwarding, user_message("go"))
    assert tool.contexts == [marker]
