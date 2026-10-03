package org.libpetri.adk.verify;

import static com.google.common.truth.Truth.assertWithMessage;

import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.sessions.BaseSessionService;
import com.google.adk.sessions.Session;
import io.reactivex.rxjava3.core.Flowable;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.OptionalInt;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Supplier;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.subnet.LlmStepSubnet;
import org.libpetri.adk.subnet.PersistStateSubnet;
import org.libpetri.adk.subnet.RouterSubnet;
import org.libpetri.adk.subnet.ToolDispatchSubnet;
import org.libpetri.adk.subnet.TransferRouterSubnet;
import org.libpetri.analysis.EnvironmentAnalysisMode;
import org.libpetri.core.Place;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.SubnetVerifyOptions;
import org.libpetri.core.Token;
import org.libpetri.smt.SmtProperty;
import org.libpetri.smt.SmtVerifier;
import org.libpetri.verification.VerificationHarness;
import org.libpetri.verification.VerificationResult;

/**
 * Each stock subnet proved on its own, through libpetri's harness
 * ({@link SubnetDef#verify(VerificationHarness, SubnetVerifyOptions)}).
 *
 * <p>The harness feeds the subnet's input port exactly {@value #K} tokens
 * ({@code arrivals(K, K)}, a total over the whole run, not a refill) and
 * observes each output port on a {@code harness_out_<port>} place. Every
 * subnet must then be deadlock-free with the outputs as its only sinks, and
 * come to rest with exactly {@value #K} outcomes across them: one per input,
 * none lost, none duplicated.
 *
 * <p>Actions are bound (CORE-043) to stubs that are never invoked; only the
 * structure is encoded.
 */
class StockSubnetProofsTest {

    private static final int K = 2;

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    static boolean z3Available() {
        return SmtVerifier.z3Available();
    }

    @Test
    @EnabledIf("z3Available")
    void llm_step_turns_every_request_into_exactly_one_response() {
        assertOneOutcomePerInput(
                LlmStepSubnet.DEF.bindActions(LlmStepSubnet.actionBindings(stubLlm())),
                "llmRequest", () -> Token.of(LlmRequest.builder().build()),
                out("llmResponse", LlmResponse.class));
    }

    @Test
    @EnabledIf("z3Available")
    void router_routes_every_response_to_exactly_one_branch() {
        assertOneOutcomePerInput(
                RouterSubnet.DEF.bindActions(RouterSubnet.actionBindings(
                        new RouterSubnet.Config("agent", () -> "inv"))),
                "llmResponse", () -> Token.of(LlmResponse.builder().build()),
                out("toolCalls", AdkColours.ToolCalls.class),
                out("transfer", AdkColours.TransferTarget.class),
                out("eventOut", com.google.adk.events.Event.class));
    }

    @Test
    @EnabledIf("z3Available")
    void tool_dispatch_answers_every_call_batch_with_exactly_one_result_batch() {
        assertOneOutcomePerInput(
                ToolDispatchSubnet.DEF.bindActions(ToolDispatchSubnet.actionBindings(
                        Map.of(), () -> null, EXECUTOR)),
                "toolCalls", () -> Token.of(new AdkColours.ToolCalls(List.of())),
                out("toolResults", AdkColours.ToolResults.class));
    }

    @Test
    @EnabledIf("z3Available")
    void transfer_router_delivers_every_transfer_to_exactly_one_target_or_unknown() {
        var known = Set.of("billing", "tech_support");
        var outs = new ArrayList<Place<?>>();
        outs.add(out("eventOut", com.google.adk.events.Event.class));
        for (String name : known) {
            outs.add(out("target/" + name, AdkColours.TransferTarget.class));
        }
        outs.add(out("target/_unknown", AdkColours.TransferTarget.class));
        assertOneOutcomePerInput(
                TransferRouterSubnet.def(known).bindActions(TransferRouterSubnet.actionBindings(
                        known, TransferRouterSubnet.Config.of("agent"))),
                "transfer", () -> Token.of(new AdkColours.TransferTarget("billing")),
                outs.toArray(new Place<?>[0]));
    }

    /**
     * Persist is a pure consumer: there is no outcome to count, only that
     * every write is taken. The transition is untimed (its bound on
     * {@code appendEvent} is an action timeout, which the verifier does not
     * model), so no reaping is involved and no {@code assumeNoReaping} is
     * needed.
     */
    @Test
    @EnabledIf("z3Available")
    void persist_state_takes_every_write() {
        var def = PersistStateSubnet.DEF.bindActions(PersistStateSubnet.actionBindings(
                PersistStateSubnet.Config.builder("agent",
                        org.mockito.Mockito.mock(BaseSessionService.class),
                        () -> org.mockito.Mockito.mock(Session.class)).build()));
        var result = def.verify(
                VerificationHarness.builder()
                        .input("legacySessionWrite",
                                () -> Token.of(new AdkColours.LegacySessionWrite(Map.of())))
                        .property(SmtProperty.deadlockFree())
                        .build(),
                SubnetVerifyOptions.DEFAULT.withEnvironmentMode(
                        EnvironmentAnalysisMode.arrivals(K, K)));
        assertAllProven(result);
    }

    /**
     * Design commitment 6: the reask budget bounds the LLM↔tool loop, which
     * only holds if the budget never accumulates. A second user input that
     * arrives while the first turn is still looping must not stack a second
     * budget on top of the first: BuildPrompt resets the place before it seeds.
     *
     * <p>libpetri has no weighted output arc, so the verifier models the seed
     * as one token however many the action writes. The bound is therefore
     * stated in seeds: the place never holds more than one seed's worth.
     * Without the reset arc this is violated after two arrivals.
     *
     * <p>{@code assumeAtomicFiring(true)} is exact here, not a convenience.
     * Synchronous actions do not close the gap: the executor deposits a
     * completed future's outputs at the end of the firing pass, and a reset
     * or inhibitor earlier in that pass does not see them (EXEC-003 AC5). What
     * makes the assumption exact is that, without it, the only counterexample
     * is BuildPrompt starting again while its earlier firing is in flight.
     * libpetri's report flags that one (CONC-002): the Java executor never
     * restarts a transition while it is in flight.
     */
    @Test
    @EnabledIf("z3Available")
    void llm_agent_reask_budget_never_stacks_across_user_inputs() {
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .reaskBudget(2)
                .dispatchExecutor(EXECUTOR)
                .build();
        var def = LlmAgentSubnet.DEF.bindActions(LlmAgentSubnet.actionBindings(stubLlm(), config));
        var budgetPlace = Place.of("sut/" + LlmAgentSubnet.REASK_BUDGET.name(), Void.class);
        var result = def.verify(
                VerificationHarness.builder()
                        .input("userIn", () -> Token.of(com.google.genai.types.Content.fromParts(
                                com.google.genai.types.Part.fromText("hi"))))
                        .property(AdkNetInvariants.budgetPlaceBounded(budgetPlace, 1))
                        .build(),
                SubnetVerifyOptions.DEFAULT
                        .withEnvironmentMode(EnvironmentAnalysisMode.arrivals(K))
                        .withConfigure((v, synthetic) -> v.assumeAtomicFiring(true)));
        assertAllProven(result);
    }

    /**
     * The composed LLM agent turns every user input into exactly one turn
     * outcome: an egress event (the answer, or the reask-exhausted fallback)
     * or a transfer. Proved without {@code assumeAtomicFiring}.
     *
     * <p>Each turn leaves its conversation and any unspent reask budget at
     * rest; the next BuildPrompt resets both. They are excused as sinks
     * unconditionally, which cannot hide a stalled turn: a turn stuck with
     * either one would be missing its outcome, and the count would fail.
     */
    @Test
    @EnabledIf("z3Available")
    void llm_agent_turns_every_user_input_into_exactly_one_outcome() {
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .reaskBudget(2)
                .dispatchExecutor(EXECUTOR)
                .build();
        assertOneOutcomePerInput(
                LlmAgentSubnet.DEF.bindActions(LlmAgentSubnet.actionBindings(stubLlm(), config)),
                "userIn", () -> Token.of(com.google.genai.types.Content.fromParts(
                        com.google.genai.types.Part.fromText("hi"))),
                new Place<?>[] {
                        out("eventOut", com.google.adk.events.Event.class),
                        out("transfer", AdkColours.TransferTarget.class)},
                Place.of("sut/" + LlmAgentSubnet.REASK_BUDGET.name(), Void.class),
                Place.of("sut/" + LlmAgentSubnet.CONVERSATION.name(),
                        LlmAgentSubnet.Conversation.class));
    }

    // ============================================================
    //  Helpers
    // ============================================================

    private static void assertOneOutcomePerInput(
            SubnetDef<Void> def, String inPort, Supplier<Token<?>> input, Place<?>... outs) {
        assertOneOutcomePerInput(def, inPort, input, outs, new Place<?>[0]);
    }

    /**
     * As above, with {@code atRest} places also excused as sinks: state a
     * subnet keeps between inputs by design.
     */
    private static void assertOneOutcomePerInput(
            SubnetDef<Void> def, String inPort, Supplier<Token<?>> input,
            Place<?>[] outs, Place<?>... atRest) {
        var sinks = new ArrayList<Place<?>>(List.of(outs));
        sinks.addAll(List.of(atRest));
        var result = def.verify(
                VerificationHarness.builder()
                        .input(inPort, input)
                        .property(SmtProperty.deadlockFree())
                        .property(SmtProperty.quiescentCount(List.of(outs), K, OptionalInt.of(K)))
                        .build(),
                SubnetVerifyOptions.DEFAULT
                        .withEnvironmentMode(EnvironmentAnalysisMode.arrivals(K, K))
                        .withConfigure((v, synthetic) ->
                                v.sinkPlaces(sinks.toArray(new Place<?>[0]))));
        assertAllProven(result);
    }

    private static void assertAllProven(VerificationResult result) {
        var failures = new LinkedHashMap<String, String>();
        result.perProperty().forEach((property, r) -> {
            if (!r.isProven()) failures.put(property.toString(), r.verdict() + "\n" + r.report());
        });
        assertWithMessage("every property must be Proven: %s", failures)
                .that(failures)
                .isEmpty();
    }

    /** The harness's observation place for output port {@code port}. */
    private static <T> Place<T> out(String port, Class<T> type) {
        return Place.of("harness_out_" + port, type);
    }

    private static BaseLlm stubLlm() {
        return new BaseLlm("stub") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.error(new UnsupportedOperationException("structure only"));
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException("structure only");
            }
        };
    }

}
