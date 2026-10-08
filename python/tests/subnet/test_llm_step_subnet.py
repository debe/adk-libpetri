"""Port of ``LlmStepSubnetTest.java``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import libpetri as lp
import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._spec import NetSpec
from adk_libpetri.subnet import llm_step
from adk_libpetri.subnet.actions import bind
from support.fake_llm import ScriptedLlm, text

T = llm_step.Transitions


@dataclass
class Fixture:
    responses: list[LlmResponse]
    events: list[lp.NetEvent]

    def fired_transition_names(self) -> list[str]:
        return [e.transition_name for e in self.events if e.type == "TransitionStarted"]

    def failures(self) -> list[lp.NetEvent]:
        return [e for e in self.events if e.type == "TransitionFailed"]


async def run(llm: Any, callbacks: llm_step.Callbacks, *requests: LlmRequest) -> Fixture:
    # Java composes DEF into a host "test-net"; NetSpec.compose is flat, so the same net.
    spec = NetSpec.compose("test-net", llm_step.DEF)
    net = spec.build(llm_step.action_bindings(llm, callbacks))
    store = lp.InMemoryEventStore()
    marking = await lp.run_async(
        net, initial={C.LLM_REQUEST.name: list(requests)}, event_store=store
    )
    return Fixture(list(marking.tokens(C.LLM_RESPONSE.name)), list(store.events()))


def simple_request(user_text: str) -> LlmRequest:
    return LlmRequest(
        model="fake-model",
        contents=[types.Content(role="user", parts=[types.Part(text=user_text)])],
    )


def texts(responses: list[LlmResponse]) -> list[str]:
    return [r.content.parts[0].text for r in responses]  # type: ignore[union-attr,index]


# ============================================================
#  Happy path
# ============================================================


async def test_llm_call_routes_response_to_output_port() -> None:
    response = text("hello world")
    fixture = await run(ScriptedLlm.of(response), llm_step.Callbacks.none(), simple_request("hi"))

    assert fixture.responses == [response]
    assert fixture.fired_transition_names() == [T.BEFORE_MODEL, T.LLM_CALL, T.AFTER_MODEL]


async def test_multiple_initial_requests_each_produce_one_response() -> None:
    r1, r2 = text("first"), text("second")
    fixture = await run(
        ScriptedLlm.of(r1, r2),
        llm_step.Callbacks.none(),
        simple_request("q1"),
        simple_request("q2"),
    )

    assert sorted(texts(fixture.responses)) == ["first", "second"]


# ============================================================
#  Before-model short-circuit
# ============================================================


async def test_before_model_short_circuit_skips_llm_call_and_after_model() -> None:
    canned = text("canned")
    llm = ScriptedLlm.of(text("from model"))
    callbacks = llm_step.Callbacks(before_model=lambda req: canned)

    fixture = await run(llm, callbacks, simple_request("anything"))

    assert fixture.responses == [canned]
    assert llm.requests == []
    assert fixture.fired_transition_names() == [T.BEFORE_MODEL]


async def test_before_model_returning_empty_optional_continues_to_llm_call() -> None:
    model_response = text("model said")
    saw_before: list[LlmRequest] = []

    def before(req: LlmRequest) -> None:
        saw_before.append(req)
        return None  # Java Optional.empty() -> None

    fixture = await run(
        ScriptedLlm.of(model_response),
        llm_step.Callbacks(before_model=before),
        simple_request("user prompt"),
    )

    assert fixture.responses == [model_response]
    assert saw_before
    assert T.LLM_CALL in fixture.fired_transition_names()
    assert T.AFTER_MODEL in fixture.fired_transition_names()


async def test_before_model_async_callback_is_awaited() -> None:
    # Python-only: callbacks may be coroutines, awaited on the loop.
    canned = text("async canned")

    async def before(req: LlmRequest) -> LlmResponse:
        return canned

    fixture = await run(
        ScriptedLlm.of(), llm_step.Callbacks(before_model=before), simple_request("q")
    )

    assert fixture.responses == [canned]


# ============================================================
#  After-model mutation
# ============================================================


async def test_after_model_can_replace_the_response() -> None:
    replacement = text("replaced")
    fixture = await run(
        ScriptedLlm.of(text("raw")),
        llm_step.Callbacks(after_model=lambda r: replacement),
        simple_request("q"),
    )

    assert fixture.responses == [replacement]


async def test_after_model_default_forwards_unchanged() -> None:
    raw = text("raw")
    fixture = await run(ScriptedLlm.of(raw), llm_step.Callbacks.none(), simple_request("q"))

    assert fixture.responses == [raw]


# ============================================================
#  Error path
# ============================================================


async def test_llm_error_with_recovery_callback_emits_fallback_response() -> None:
    fallback = text("sorry, try again")
    saw_error: list[llm_step.LlmError] = []

    def on_error(err: llm_step.LlmError) -> LlmResponse:
        saw_error.append(err)
        return fallback

    fixture = await run(
        ScriptedLlm.of(RuntimeError("boom")),
        llm_step.Callbacks(on_model_error=on_error),
        simple_request("q"),
    )

    assert fixture.responses == [fallback]
    assert saw_error[0].message == "boom"
    # Java "java.lang.RuntimeException" -> builtins are unqualified.
    assert saw_error[0].exception_type == "RuntimeError"
    assert fixture.fired_transition_names() == [T.BEFORE_MODEL, T.LLM_CALL, T.ON_MODEL_ERROR]
    assert T.AFTER_MODEL not in fixture.fired_transition_names()


async def test_llm_error_without_recovery_callback_fails_on_model_error_transition() -> None:
    fixture = await run(
        ScriptedLlm.of(RuntimeError("network down")),
        llm_step.Callbacks.none(),
        simple_request("q"),
    )

    assert fixture.responses == []
    failed = fixture.failures()
    assert len(failed) == 1
    assert failed[0].transition_name == T.ON_MODEL_ERROR
    assert "network down" in failed[0].payload()["error"]


async def test_a_missing_api_key_fails_with_the_fix_first() -> None:
    from adk_libpetri.bridge import TransitionFailure

    said = "No API key was provided. Please pass a valid API key. Learn how to create one."
    fixture = await run(
        ScriptedLlm.of(ValueError(said)), llm_step.Callbacks.none(), simple_request("q")
    )
    [failed] = fixture.failures()
    failure = TransitionFailure.from_event(failed)
    assert failure is not None
    message = str(failure)
    # ADK's snackbar cuts a long message: the fix leads, the transition trails.
    assert message.startswith("No Gemini API key: put GOOGLE_API_KEY=... in ")
    assert message.endswith(f"(The model said: No API key was provided.) ({T.ON_MODEL_ERROR})")
    assert "Learn how" not in message and "no recovery callback" not in message


# ============================================================
#  Subnet shape + binding validation
# ============================================================


def test_subnet_def_declares_exactly_the_expected_ports() -> None:
    assert sorted(p.name for p in llm_step.DEF.ports) == ["llmRequest", "llmResponse"]


def test_subnet_def_declares_exactly_four_transitions() -> None:
    assert sorted(llm_step.DEF.transition_names) == [
        T.AFTER_MODEL,
        T.BEFORE_MODEL,
        T.LLM_CALL,
        T.ON_MODEL_ERROR,
    ]


def _noop(ctx: Any) -> None:
    return None


def test_subnet_actions_bind_rejects_missing_keys() -> None:
    partial = {T.BEFORE_MODEL: _noop, T.LLM_CALL: _noop}
    with pytest.raises(ValueError) as ex:
        bind(llm_step.DEF, partial)
    msg = str(ex.value)
    assert "missing keys" in msg
    assert T.AFTER_MODEL in msg
    assert T.ON_MODEL_ERROR in msg


def test_subnet_actions_bind_rejects_extra_keys() -> None:
    extra = {
        T.BEFORE_MODEL: _noop,
        T.LLM_CALL: _noop,
        T.AFTER_MODEL: _noop,
        T.ON_MODEL_ERROR: _noop,
        "BogusTransition": _noop,
    }
    with pytest.raises(ValueError) as ex:
        bind(llm_step.DEF, extra)
    msg = str(ex.value)
    assert "extra keys" in msg
    assert "BogusTransition" in msg


# ============================================================
#  Composition + per-token isolation
# ============================================================


def test_composed_subnet_routes_inputs_to_correct_subnet_internal_places() -> None:
    net = NetSpec.compose("test-net", llm_step.DEF).build(
        llm_step.action_bindings(ScriptedLlm.of(text("ok")))
    )

    transition_names = {t.name for t in net.transitions}
    assert {T.BEFORE_MODEL, T.LLM_CALL, T.AFTER_MODEL, T.ON_MODEL_ERROR} <= transition_names
    place_names = [p.name for p in net.places]
    expected = [
        C.LLM_REQUEST,
        C.LLM_RESPONSE,
        llm_step.Places.READY_TO_CALL,
        llm_step.Places.RAW_RESPONSE,
        llm_step.Places.LLM_ERROR,
    ]
    assert {p.name for p in expected} <= set(place_names)
    # Composing must not duplicate the boundary places.
    assert len(place_names) == len(set(place_names))
