"""Port of Java ``LlmStepSubnetInstancingTest``.

The stock ``LlmStep`` actions name their places by the declared constants
(``ctx.input(C.LLM_REQUEST)``); libpetri resolves those to the per-instance
bound or prefixed places (MOD-031), so two instances with distinct bound
places run in one net with no cross-talk.

Java binds late (``DEF.instantiate(prefix).bindActions(...)``). The Python
stock actions take a typed ``Ctx`` that only ``NetSpec.build``/``subnet_def``
wrap them into, so they are bound first, ``DEF.subnet_def(actions)``, then
instantiated; the compose path (``compose_instance`` + port bindings) is the
same.
"""

from __future__ import annotations

import libpetri as lp
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_libpetri.subnet import llm_step
from support.fake_llm import ScriptedLlm, text


async def test_two_instances_with_distinct_bound_places_do_not_cross_talk() -> None:
    intent_request = request("intent-q")
    guard_request = request("guard-q")
    intent_response = text("intent-a")
    guard_response = text("guard-a")

    # One shared BaseLlm; each request object maps to its own response.
    # Cross-talk between the instances would land the wrong response on a place.
    table = {id(intent_request): intent_response, id(guard_request): guard_response}

    def answer(req: LlmRequest) -> LlmResponse:
        return table[id(req)]

    llm = ScriptedLlm.of(answer, answer)

    # Two instances of the SAME stock subnet, distinct prefixes + bound places.
    intent = llm_step.DEF.subnet_def(llm_step.action_bindings(llm)).instantiate("intentLlm")
    guard = llm_step.DEF.subnet_def(llm_step.action_bindings(llm)).instantiate("guardLlm")

    net = (
        lp.NetBuilder("two-llm-net")
        .compose_instance(
            intent, {"llmRequest": lp.Place("intentReq"), "llmResponse": lp.Place("intentResp")}
        )
        .compose_instance(
            guard, {"llmRequest": lp.Place("guardReq"), "llmResponse": lp.Place("guardResp")}
        )
        .build()
    )

    marking = await lp.run_async(
        net, initial={"intentReq": [intent_request], "guardReq": [guard_request]}
    )

    assert marking.tokens("intentResp") == (intent_response,)
    assert marking.tokens("guardResp") == (guard_response,)


async def test_single_instance_round_trips_via_instantiate_bindport() -> None:
    response = text("pong")
    llm = ScriptedLlm.of(response)

    net = (
        lp.NetBuilder("probe-net")
        .compose_instance(
            llm_step.DEF.subnet_def(llm_step.action_bindings(llm)).instantiate("probe"),
            {"llmRequest": lp.Place("probeReq"), "llmResponse": lp.Place("probeResp")},
        )
        .build()
    )

    marking = await lp.run_async(net, initial={"probeReq": [request("ping")]})

    assert marking.tokens("probeResp") == (response,)


def request(user_text: str) -> LlmRequest:
    return LlmRequest(
        model="fake-model",
        contents=[types.Content(role="user", parts=[types.Part(text=user_text)])],
    )
