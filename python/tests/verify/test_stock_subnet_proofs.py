"""Each stock subnet proved on its own through libpetri's harness (VER table, README).

The harness feeds the input port exactly K tokens (``arrivals_between(K, K)``)
and observes each output port on ``harness_out_<port>``. Every subnet must be
deadlock-free with the outputs as its only sinks and come to rest with
exactly K outcomes across them: one per input, none lost, none duplicated.

The composed LLM agents have a ``turnAbort`` input port the harness must not
feed, so they are verified as composed nets. Actions are bound to stubs that
are never invoked; only structure is encoded.
"""

from __future__ import annotations

from typing import Any

import libpetri as lp
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._spec import NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.subnet import (
    llm_agent,
    llm_step,
    persist_state,
    router,
    tool_dispatch,
    transfer_router,
)
from adk_libpetri.verify import budget_place_bounded
from support.fake_llm import ScriptedLlm
from support.smt_proofs import assert_all_proven, assert_each_proven, requires_z3

pytestmark = requires_z3

K = 2


def _outs(*ports: str) -> list[str]:
    return [f"harness_out_{p}" for p in ports]


def assert_one_outcome_per_input(
    spec: NetSpec, actions: dict[str, Any], in_port: str, supplier: Any, outs: list[str]
) -> None:
    harness = (
        lp.VerificationHarness()
        .input(in_port, supplier)
        .property(lp.deadlock_free())
        .property(lp.quiescent_count(outs, K, K))
    )
    result = lp.verify_subnet(
        spec.subnet_def(actions),
        harness,
        environment_mode=lp.arrivals(K, K),
        sink_places=outs,
    )
    assert_all_proven(result)


def test_llm_step_turns_every_request_into_exactly_one_response() -> None:
    assert_one_outcome_per_input(
        llm_step.DEF,
        llm_step.action_bindings(ScriptedLlm.of()),
        "llmRequest",
        lambda: LlmRequest(),
        _outs("llmResponse"),
    )


def test_router_routes_every_response_to_exactly_one_branch() -> None:
    assert_one_outcome_per_input(
        router.DEF,
        router.action_bindings(router.Config("agent", lambda: "inv")),
        "llmResponse",
        lambda: LlmResponse(),
        _outs("toolCalls", "transfer", "eventOut"),
    )


def test_tool_dispatch_answers_every_call_batch_with_exactly_one_result_batch() -> None:
    assert_one_outcome_per_input(
        tool_dispatch.DEF,
        tool_dispatch.action_bindings({}),
        "toolCalls",
        lambda: C.ToolCalls((types.FunctionCall(name="t"),)),
        _outs("toolResults"),
    )


def test_transfer_router_delivers_every_transfer_to_exactly_one_target_or_unknown() -> None:
    known = ["billing", "tech_support"]
    assert_one_outcome_per_input(
        transfer_router.def_(known),
        transfer_router.action_bindings(known, transfer_router.Config("agent")),
        "transfer",
        lambda: C.TransferTarget("billing"),
        _outs("eventOut", *(f"target/{n}" for n in known), "target/_unknown"),
    )


def test_persist_state_takes_every_write() -> None:
    """A pure consumer: no outcome to count, only that every write is taken.

    The transition is untimed (its bound on ``append_event`` is an action
    timeout the verifier does not model), so no reaping is involved.
    """
    config = persist_state.Config("agent", session_service=None, session_supplier=lambda: None)  # type: ignore[arg-type]
    harness = (
        lp.VerificationHarness()
        .input("legacySessionWrite", lambda: C.LegacySessionWrite({}))
        .property(lp.deadlock_free())
    )
    result = lp.verify_subnet(
        persist_state.DEF.subnet_def(persist_state.action_bindings(config)),
        harness,
        environment_mode=lp.arrivals(K, K),
    )
    assert_all_proven(result)


# ----------------------------------------------------------------------------
#  The composed LLM agent: one turn at a time, under its permit
# ----------------------------------------------------------------------------
#
# USER_IN is the environment place, TURN_ABORT an ordinary one that only the
# failure model below marks, and TURN_PERMIT carries the one token
# PetriRunner seeds. None of the proofs assumes atomic firing.


def agent_net(spec: NetSpec = llm_agent.DEF) -> lp.BuiltNet:
    config = llm_agent.Config(name="agent", model="fake-model", reask_budget=2)
    return spec.build(llm_agent.action_bindings(ScriptedLlm.of(), config))


def _agent_opts(
    *env: Place[Any], mode: Any, sinks: list[Place[Any]] | None = None
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "initial_marking": {C.TURN_PERMIT.name: 1},
        "environment_places": [p.name for p in env],
        "environment_mode": mode,
    }
    if sinks is not None:
        opts["sink_places"] = [p.name for p in sinks]
    return opts


def test_llm_agent_runs_one_turn_at_a_time_without_assuming_atomic_firing() -> None:
    """Two inputs, the second free to arrive at any point of the first turn,
    never share a turn. Without the permit all three are violated (E2)."""
    assert_each_proven(
        agent_net(),
        {
            "one turn in flight: place_bound(TURN_ACTIVE, 1)": lp.place_bound(
                llm_agent.TURN_ACTIVE.name, 1
            ),
            "one conversation: place_bound(CONVERSATION, 1)": lp.place_bound(
                llm_agent.CONVERSATION.name, 1
            ),
            "reask budget never stacks: budget_place_bounded(REASK_BUDGET, 1)": (
                budget_place_bounded(llm_agent.REASK_BUDGET, 1)
            ),
        },
        **_agent_opts(C.USER_IN, mode=lp.arrivals(K)),
    )


def test_llm_agent_turns_every_user_input_into_exactly_one_outcome() -> None:
    """Every input becomes one egress event or one transfer, and the agent
    comes to rest holding its permit and nothing else of any turn."""
    assert_each_proven(
        agent_net(),
        {
            "deadlock_free": lp.deadlock_free(),
            "one outcome per input: quiescent_count(eventOut + transfer) == K": lp.quiescent_count(
                [C.EVENT_OUT.name, C.TRANSFER.name], K, K
            ),
        },
        **_agent_opts(
            C.USER_IN,
            mode=lp.arrivals(K, K),
            sinks=[C.EVENT_OUT, C.TRANSFER, C.TURN_PERMIT],
        ),
    )


def with_failures(spec: NetSpec, resting_points: list[Place[Any]]) -> NetSpec:
    """``spec`` plus, per resting place, ``Fail_<place>``: consume and signal TURN_ABORT.

    That is the consuming transition failing (input gone, no output),
    followed by PetriAgent's abort.
    """
    return spec.with_transitions(
        *(TransitionSpec(f"Fail_{p.name}", (one(p),), out(C.TURN_ABORT)) for p in resting_points),
        name=f"{spec.name}-with-failures",
    )


def test_llm_agent_recovers_from_a_failure_at_any_step_of_a_turn() -> None:
    resting = [
        llm_agent.TURN_INPUT,
        C.LLM_REQUEST,
        llm_step.Places.READY_TO_CALL,
        llm_step.Places.RAW_RESPONSE,
        llm_step.Places.LLM_ERROR,
        C.LLM_RESPONSE,
        C.TOOL_CALLS,
        C.TOOL_RESULTS,
    ]
    spec = with_failures(llm_agent.DEF, resting)
    config = llm_agent.Config(name="agent", model="fake-model", reask_budget=2)
    actions = dict(llm_agent.action_bindings(ScriptedLlm.of(), config))
    for p in resting:

        def fail(ctx: Any, p: Place[Any] = p) -> None:
            ctx.input(p)
            ctx.signal(C.TURN_ABORT)

        actions[f"Fail_{p.name}"] = fail
    assert_each_proven(
        spec.build(actions),
        {
            "deadlock_free": lp.deadlock_free(),
            "one turn in flight: place_bound(TURN_ACTIVE, 1)": lp.place_bound(
                llm_agent.TURN_ACTIVE.name, 1
            ),
            "one conversation: place_bound(CONVERSATION, 1)": lp.place_bound(
                llm_agent.CONVERSATION.name, 1
            ),
        },
        **_agent_opts(
            C.USER_IN,
            mode=lp.arrivals(K, K),
            sinks=[C.EVENT_OUT, C.TRANSFER, C.TURN_PERMIT],
        ),
    )


def test_llm_agent_never_mints_a_second_permit_however_aborts_arrive() -> None:
    assert_each_proven(
        agent_net(),
        {
            "place_bound(TURN_PERMIT, 1)": lp.place_bound(C.TURN_PERMIT.name, 1),
            "one turn in flight: place_bound(TURN_ACTIVE, 1)": lp.place_bound(
                llm_agent.TURN_ACTIVE.name, 1
            ),
        },
        **_agent_opts(C.USER_IN, C.TURN_ABORT, mode=lp.arrivals(K)),
    )


def test_streaming_llm_agent_runs_one_turn_at_a_time() -> None:
    """The SSE agent shares the composition, permit included. Its chunks enter
    through an env place the verifier cannot tie to the request that streamed
    them, so only the safety half is claimed."""
    from adk_libpetri.runner.petri_runner import HandleRef
    from adk_libpetri.subnet import llm_streaming_step, streaming_llm_agent

    config = streaming_llm_agent.Config(name="agent", model="fake-model", reask_budget=2)
    net = streaming_llm_agent.DEF.build(
        streaming_llm_agent.action_bindings(ScriptedLlm.of(), config, HandleRef())
    )
    assert_each_proven(
        net,
        {
            "one turn in flight: place_bound(TURN_ACTIVE, 1)": lp.place_bound(
                llm_agent.TURN_ACTIVE.name, 1
            ),
            "place_bound(TURN_PERMIT, 1)": lp.place_bound(C.TURN_PERMIT.name, 1),
            "one conversation: place_bound(CONVERSATION, 1)": lp.place_bound(
                llm_agent.CONVERSATION.name, 1
            ),
        },
        **_agent_opts(C.USER_IN, llm_streaming_step.Places.CHUNK, mode=lp.arrivals(K)),
    )
