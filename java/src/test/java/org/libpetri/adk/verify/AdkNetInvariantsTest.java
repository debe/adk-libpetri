package org.libpetri.adk.verify;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.sessions.InMemorySessionService;
import com.microsoft.z3.Context;
import io.reactivex.rxjava3.core.Flowable;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.analysis.MarkingState;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Transition;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.subnet.PersistStateSubnet;
import org.libpetri.adk.subnet.TransferRouterSubnet;
import org.libpetri.smt.SmtProperty;
import org.libpetri.smt.SmtVerifier;

class AdkNetInvariantsTest {

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    // ============================================================
    //  z3 detection — gates the SMT-using tests
    // ============================================================

    static boolean z3Available() {
        try {
            new Context().close();
            return true;
        } catch (UnsatisfiedLinkError | NoClassDefFoundError _) {
            return false;
        }
    }

    // ============================================================
    //  stateWriteSinglePersister — structural
    // ============================================================

    @Test
    void real_persist_state_subnet_passes_single_persister_check() {
        var net = PetriNet.builder("only-persist")
                .compose(PersistStateSubnet.DEF)
                .build();

        assertThat(AdkNetInvariants.singleLegacySessionWriter(net)).isEmpty();
    }

    @Test
    void net_with_no_state_consumers_passes_vacuously() {
        // Empty topology, just declaring the place.
        var net = PetriNet.builder("no-consumers")
                .place(AdkColours.LEGACY_SESSION_WRITE)
                .build();
        assertThat(AdkNetInvariants.singleLegacySessionWriter(net)).isEmpty();
    }

    @Test
    void net_with_two_persisters_reports_violation() {
        var consumerA = Transition.builder("PersistA")
                .inputs(Arc.In.one(AdkColours.LEGACY_SESSION_WRITE))
                .build();
        var consumerB = Transition.builder("PersistB")
                .inputs(Arc.In.one(AdkColours.LEGACY_SESSION_WRITE))
                .build();

        var net = PetriNet.builder("two-persisters")
                .transition(consumerA)
                .transition(consumerB)
                .build();

        var violations = AdkNetInvariants.singleLegacySessionWriter(net);
        assertThat(violations).hasSize(1);
        assertThat(violations.get(0).invariant()).isEqualTo("singleLegacySessionWriter");
        assertThat(violations.get(0).message()).contains("PersistA");
        assertThat(violations.get(0).message()).contains("PersistB");
    }

    // ============================================================
    //  endInvocationInhibitsAll — structural
    // ============================================================

    @Test
    void all_advancing_transitions_have_inhibitor_passes() {
        var t1 = Transition.builder("Advance1")
                .inputs(Arc.In.one(AdkColours.LLM_REQUEST))
                .inhibitor(AdkColours.END_INVOCATION)
                .outputs(Arc.Out.place(AdkColours.LLM_RESPONSE))
                .build();
        var t2 = Transition.builder("Advance2")
                .inputs(Arc.In.one(AdkColours.LLM_RESPONSE))
                .inhibitor(AdkColours.END_INVOCATION)
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();

        var net = PetriNet.builder("guarded")
                .transition(t1).transition(t2)
                .place(AdkColours.END_INVOCATION)
                .build();

        var violations = AdkNetInvariants.endInvocationInhibitsAll(net, Set.of("Advance1", "Advance2"));
        assertThat(violations).isEmpty();
    }

    @Test
    void missing_inhibitor_on_advancing_reports_violation() {
        var guarded = Transition.builder("Guarded")
                .inputs(Arc.In.one(AdkColours.LLM_REQUEST))
                .inhibitor(AdkColours.END_INVOCATION)
                .outputs(Arc.Out.place(AdkColours.LLM_RESPONSE))
                .build();
        var unguarded = Transition.builder("Unguarded")
                .inputs(Arc.In.one(AdkColours.LLM_RESPONSE))
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();

        var net = PetriNet.builder("mixed")
                .transition(guarded).transition(unguarded)
                .place(AdkColours.END_INVOCATION)
                .build();

        var violations = AdkNetInvariants.endInvocationInhibitsAll(net, Set.of("Guarded", "Unguarded"));
        assertThat(violations).hasSize(1);
        assertThat(violations.get(0).message()).contains("Unguarded");
        assertThat(violations.get(0).message()).doesNotContain("Guarded");
    }

    @Test
    void unknown_advancing_name_reports_violation() {
        var net = PetriNet.builder("empty").place(AdkColours.END_INVOCATION).build();
        var violations = AdkNetInvariants.endInvocationInhibitsAll(net, Set.of("DoesNotExist"));
        assertThat(violations).hasSize(1);
        assertThat(violations.get(0).message()).contains("DoesNotExist");
        assertThat(violations.get(0).message()).contains("not in net");
    }

    // ============================================================
    //  transferDemuxHasUnknownFallback — structural
    // ============================================================

    @Test
    void real_transfer_router_subnet_has_unknown_fallback() {
        var net = PetriNet.builder("with-transfer")
                .compose(TransferRouterSubnet.def(Set.of("billing", "sales")))
                .build();

        assertThat(AdkNetInvariants.transferDemuxHasUnknownFallback(net)).isEmpty();
    }

    @Test
    void net_without_transfer_demux_passes_vacuously() {
        var net = PetriNet.builder("no-transfer").place(AdkColours.USER_IN).build();
        assertThat(AdkNetInvariants.transferDemuxHasUnknownFallback(net)).isEmpty();
    }

    @Test
    void unknown_target_without_consumer_reports_violation() {
        // Manually wire just the unknown place with no consumer.
        var net = PetriNet.builder("orphan-unknown")
                .place(TransferRouterSubnet.UNKNOWN_TARGET)
                .build();

        var violations = AdkNetInvariants.transferDemuxHasUnknownFallback(net);
        assertThat(violations).hasSize(1);
        assertThat(violations.get(0).message()).contains("UNKNOWN_TARGET");
        assertThat(violations.get(0).message()).contains("no consumer");
    }

    // ============================================================
    //  Real LlmAgentSubnet passes all relevant structural checks
    // ============================================================

    @Test
    void llm_agent_subnet_passes_state_writer_check() {
        // LlmAgentSubnet doesn't write to STATE_DELTA itself (it's a
        // boundary place the agent exposes for a parent's PersistStateSubnet
        // to consume). So state-writer count is 0, which is fine.
        var net = buildAgentNet();
        assertThat(AdkNetInvariants.singleLegacySessionWriter(net)).isEmpty();
    }

    @Test
    void llm_agent_subnet_composed_with_persist_still_has_single_writer() {
        var llm = neverCalledLlm();
        var session = new InMemorySessionService()
                .createSession("app", "u", (Map<String, Object>) null, "s").blockingGet();
        var net = PetriNet.builder("agent+persist")
                .compose(LlmAgentSubnet.DEF)
                .compose(PersistStateSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm,
                        LlmAgentSubnet.Config.builder("a", "m")
                                .dispatchExecutor(EXECUTOR)
                                .build()))
                .bindActions(PersistStateSubnet.actionBindings(
                        PersistStateSubnet.Config.builder("a",
                                new InMemorySessionService(), () -> session).build()));

        assertThat(AdkNetInvariants.singleLegacySessionWriter(net)).isEmpty();
    }

    // ============================================================
    //  SMT property factory validations (don't need Z3)
    // ============================================================

    @Test
    void reask_budget_is_bounded_factory_produces_correct_property() {
        var budget = Place.of("budget", Void.class);
        var prop = AdkNetInvariants.reaskBudgetIsBounded(budget, 3);
        assertThat(prop).isInstanceOf(SmtProperty.PlaceBound.class);
        var pb = (SmtProperty.PlaceBound) prop;
        assertThat(pb.place()).isEqualTo(budget);
        assertThat(pb.bound()).isEqualTo(3);
    }

    @Test
    void event_out_bounded_factory_produces_correct_property() {
        var prop = AdkNetInvariants.eventOutBounded(5);
        assertThat(prop).isInstanceOf(SmtProperty.PlaceBound.class);
        var pb = (SmtProperty.PlaceBound) prop;
        assertThat(pb.place()).isEqualTo(AdkColours.EVENT_OUT);
        assertThat(pb.bound()).isEqualTo(5);
    }

    @Test
    void no_fire_after_end_invocation_factory_produces_mutex() {
        var prop = AdkNetInvariants.noFireAfterEndInvocation(AdkColours.LLM_REQUEST);
        assertThat(prop).isInstanceOf(SmtProperty.MutualExclusion.class);
        var mx = (SmtProperty.MutualExclusion) prop;
        assertThat(mx.p1()).isEqualTo(AdkColours.END_INVOCATION);
        assertThat(mx.p2()).isEqualTo(AdkColours.LLM_REQUEST);
    }

    @Test
    void factory_rejects_invalid_bounds() {
        var budget = Place.of("budget", Void.class);
        Assertions.assertThrows(
                IllegalArgumentException.class,
                () -> AdkNetInvariants.reaskBudgetIsBounded(budget, 0));
        Assertions.assertThrows(
                IllegalArgumentException.class,
                () -> AdkNetInvariants.eventOutBounded(0));
    }

    // ============================================================
    //  SMT verification — requires Z3 native libs at runtime
    // ============================================================

    @Test
    @EnabledIf("z3Available")
    void budget_bound_verifies_on_passing_net() {
        // Small synthetic net: one transition consumes from "in" and produces
        // one token to "budget". Budget never exceeds 1.
        var in     = Place.of("in", Void.class);
        var budget = Place.of("budget", Void.class);
        var t = Transition.builder("Seed")
                .inputs(Arc.In.one(in))
                .outputs(Arc.Out.place(budget))
                .build();
        var net = PetriNet.builder("budget-net")
                .transition(t)
                .build();

        var result = SmtVerifier.forNet(net)
                .initialMarking(b -> b.tokens(in, 1))
                .property(AdkNetInvariants.reaskBudgetIsBounded(budget, 1))
                .verify();

        // The property is provable: max one token can sit on "budget"
        // (Seed fires once, consuming the only token in "in"). Z3
        // Spacer should return Proven via the IC3/PDR pipeline.
        assertThat(result.isProven()).isTrue();
        assertThat(result.isViolated()).isFalse();
    }

    @Test
    @EnabledIf("z3Available")
    void budget_bound_violation_yields_counterexample() {
        // Net that fires "Seed" repeatedly via a self-loop, growing the
        // budget unboundedly. PlaceBound(budget, 1) should NOT hold.
        var in     = Place.of("in", Void.class);
        var budget = Place.of("budget", Void.class);
        // Seed: consume "in", produce one token to BOTH "budget" and back to "in"
        var seed = Transition.builder("Seed")
                .inputs(Arc.In.one(in))
                .outputs(Arc.Out.and(budget, in))
                .build();
        var net = PetriNet.builder("unbounded")
                .transition(seed)
                .build();

        var result = SmtVerifier.forNet(net)
                .initialMarking(b -> b.tokens(in, 1))
                .property(AdkNetInvariants.reaskBudgetIsBounded(budget, 1))
                .verify();

        // The budget grows without bound — PlaceBound(1) is violated.
        assertThat(result.isViolated()).isTrue();
        assertThat(result.isProven()).isFalse();
    }

    // ============================================================
    //  Fixtures
    // ============================================================

    private static PetriNet buildAgentNet() {
        return PetriNet.builder("agent")
                .compose(LlmAgentSubnet.DEF)
                .build();
    }

    private static BaseLlm neverCalledLlm() {
        return new BaseLlm("none") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.never();
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    @SuppressWarnings("unused")
    private static MarkingState emptyMarking() {
        return MarkingState.builder().build();
    }
}
