"""Port of ``AdkNetInvariantsTest.java``.

The Python validators take a :class:`NetSpec` (the IR every net is built
from), so nets here are specs rather than built libpetri nets.
"""

from __future__ import annotations

import libpetri as lp
import pytest
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from adk_libpetri import colours as C
from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, and_, one, out
from adk_libpetri.subnet import llm_agent, persist_state, transfer_router
from adk_libpetri.subnet.actions import bind_composed
from adk_libpetri.verify import (
    budget_place_bounded,
    end_invocation_inhibits_all,
    event_out_bounded,
    single_legacy_session_writer,
    transfer_demux_has_unknown_fallback,
)
from support.fake_llm import ScriptedLlm
from support.smt_proofs import requires_z3

# ============================================================
#  singleLegacySessionWriter -- structural
# ============================================================


def test_real_persist_state_subnet_passes_single_persister_check() -> None:
    net = NetSpec.compose("only-persist", persist_state.DEF)
    assert single_legacy_session_writer(net) == []


def test_net_with_no_state_consumers_passes_vacuously() -> None:
    net = NetSpec("no-consumers", (), extra_places=(C.LEGACY_SESSION_WRITE,))
    assert single_legacy_session_writer(net) == []


def test_net_with_two_persisters_reports_violation() -> None:
    net = NetSpec(
        "two-persisters",
        (
            TransitionSpec("PersistA", (one(C.LEGACY_SESSION_WRITE),)),
            TransitionSpec("PersistB", (one(C.LEGACY_SESSION_WRITE),)),
        ),
    )

    violations = single_legacy_session_writer(net)
    assert len(violations) == 1
    assert violations[0].invariant == "singleLegacySessionWriter"
    assert "PersistA" in violations[0].message
    assert "PersistB" in violations[0].message


# ============================================================
#  endInvocationInhibitsAll -- structural
# ============================================================


def test_all_advancing_transitions_have_inhibitor_passes() -> None:
    net = NetSpec(
        "guarded",
        (
            TransitionSpec(
                "Advance1",
                (one(C.LLM_REQUEST),),
                out(C.LLM_RESPONSE),
                inhibitors=(C.END_INVOCATION,),
            ),
            TransitionSpec(
                "Advance2",
                (one(C.LLM_RESPONSE),),
                out(C.EVENT_OUT),
                inhibitors=(C.END_INVOCATION,),
            ),
        ),
        extra_places=(C.END_INVOCATION,),
    )

    assert end_invocation_inhibits_all(net, {"Advance1", "Advance2"}) == []


def test_missing_inhibitor_on_advancing_reports_violation() -> None:
    net = NetSpec(
        "mixed",
        (
            TransitionSpec(
                "Guarded",
                (one(C.LLM_REQUEST),),
                out(C.LLM_RESPONSE),
                inhibitors=(C.END_INVOCATION,),
            ),
            TransitionSpec("Unguarded", (one(C.LLM_RESPONSE),), out(C.EVENT_OUT)),
        ),
        extra_places=(C.END_INVOCATION,),
    )

    violations = end_invocation_inhibits_all(net, {"Guarded", "Unguarded"})
    assert len(violations) == 1
    assert "Unguarded" in violations[0].message
    assert "'Guarded'" not in violations[0].message


def test_unknown_advancing_name_reports_violation() -> None:
    net = NetSpec("empty", (), extra_places=(C.END_INVOCATION,))

    violations = end_invocation_inhibits_all(net, {"DoesNotExist"})
    assert len(violations) == 1
    assert "DoesNotExist" in violations[0].message
    assert "not in net" in violations[0].message


# ============================================================
#  transferDemuxHasUnknownFallback -- structural
# ============================================================


def test_real_transfer_router_subnet_has_unknown_fallback() -> None:
    net = NetSpec.compose("with-transfer", transfer_router.def_(["billing", "sales"]))
    assert transfer_demux_has_unknown_fallback(net) == []


def test_net_without_transfer_demux_passes_vacuously() -> None:
    net = NetSpec("no-transfer", (), extra_places=(C.USER_IN,))
    assert transfer_demux_has_unknown_fallback(net) == []


def test_unknown_target_without_consumer_reports_violation() -> None:
    net = NetSpec("orphan-unknown", (), extra_places=(transfer_router.UNKNOWN_TARGET,))

    violations = transfer_demux_has_unknown_fallback(net)
    assert len(violations) == 1
    assert "UNKNOWN_TARGET" in violations[0].message
    assert "no consumer" in violations[0].message


# ============================================================
#  Real LlmAgentSubnet passes all relevant structural checks
# ============================================================


def test_llm_agent_subnet_passes_state_writer_check() -> None:
    # LlmAgentSubnet does not consume LEGACY_SESSION_WRITE itself.
    net = NetSpec.compose("agent", llm_agent.DEF)
    assert single_legacy_session_writer(net) == []


async def test_llm_agent_subnet_composed_with_persist_still_has_single_writer() -> None:
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="app", user_id="u", session_id="s")
    spec = NetSpec.compose("agent+persist", llm_agent.DEF, persist_state.DEF)
    # Java's two bindActions calls -> one checked bind_composed over both maps.
    bind_composed(
        spec,
        llm_agent.action_bindings(ScriptedLlm.of(), llm_agent.Config(name="a", model="m")),
        persist_state.action_bindings(persist_state.Config("a", svc, lambda: session)),
    )

    assert single_legacy_session_writer(spec) == []


# ============================================================
#  SMT property factory validations (don't need Z3)
# ============================================================
#
# libpetri-py's SmtProperty is opaque (only description()); compare against
# the place_bound it must wrap instead of Java's PlaceBound record fields.


def test_reask_budget_is_bounded_factory_produces_correct_property() -> None:
    budget: Place[None] = Place("budget")
    prop = budget_place_bounded(budget, 3)
    assert isinstance(prop, lp.SmtProperty)
    assert prop.description() == lp.place_bound("budget", 3).description()
    assert "budget" in prop.description()
    assert "3" in prop.description()


def test_event_out_bounded_factory_produces_correct_property() -> None:
    prop = event_out_bounded(5)
    assert isinstance(prop, lp.SmtProperty)
    assert prop.description() == lp.place_bound(C.EVENT_OUT.name, 5).description()
    assert C.EVENT_OUT.name in prop.description()


def test_factory_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError):
        budget_place_bounded(Place("budget"), 0)
    with pytest.raises(ValueError):
        event_out_bounded(0)


# ============================================================
#  SMT verification -- requires a z3 binary at runtime
# ============================================================

IN: Place[None] = Place("in")
BUDGET: Place[None] = Place("budget")


@requires_z3
def test_budget_bound_verifies_on_passing_net() -> None:
    # Seed consumes "in" and produces one token to "budget": budget never exceeds 1.
    def seed(ctx: Ctx) -> None:
        ctx.input(IN)
        ctx.signal(BUDGET)

    net = NetSpec("budget-net", (TransitionSpec("Seed", (one(IN),), out(BUDGET)),)).build(
        {"Seed": seed}
    )

    result = lp.verify(net, budget_place_bounded(BUDGET, 1), initial_marking={IN.name: 1})

    assert result.is_proven()
    assert not result.is_violated()


@requires_z3
def test_budget_bound_violation_yields_counterexample() -> None:
    # Seed puts its token back on "in" and adds one to "budget": unbounded growth.
    def seed(ctx: Ctx) -> None:
        ctx.input(IN)
        ctx.signal(BUDGET)
        ctx.signal(IN)

    net = NetSpec("unbounded", (TransitionSpec("Seed", (one(IN),), and_(BUDGET, IN)),)).build(
        {"Seed": seed}
    )

    result = lp.verify(net, budget_place_bounded(BUDGET, 1), initial_marking={IN.name: 1})

    assert result.is_violated()
    assert not result.is_proven()
