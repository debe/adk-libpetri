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
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.subnet.LlmStepSubnet;
import org.libpetri.adk.subnet.LlmStreamingStepSubnet;
import org.libpetri.adk.subnet.PersistStateSubnet;
import org.libpetri.adk.subnet.RouterSubnet;
import org.libpetri.adk.subnet.StreamingLlmAgentSubnet;
import org.libpetri.adk.subnet.ToolDispatchSubnet;
import org.libpetri.adk.subnet.TransferRouterSubnet;
import org.libpetri.analysis.EnvironmentAnalysisMode;
import org.libpetri.core.Arc;
import org.libpetri.core.EnvironmentPlace;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.SubnetVerifyOptions;
import org.libpetri.core.Token;
import org.libpetri.core.Transition;
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
 * <p>The composed LLM agents are the exception: their {@code turnAbort} port
 * must not receive the harness's arrivals, so they are verified as composed
 * nets (see the section on them below).
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
                "toolCalls", () -> Token.of(new AdkColours.ToolCalls(List.of(
                        com.google.genai.types.FunctionCall.builder().name("t").build()))),
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

    // ============================================================
    //  The composed LLM agent: one turn at a time, under its permit
    // ============================================================
    //
    // The agent has a turnAbort input port, and the harness would feed every
    // input port the same arrivals, so these proofs verify the composed net
    // directly: USER_IN is the environment place, TURN_ABORT an ordinary one
    // that only the failure model below ever marks, and TURN_PERMIT carries
    // the one token PetriRunner seeds. None of them assumes atomic firing.

    /**
     * Two user inputs, the second free to arrive at any point of the first
     * turn, never share a turn: at most one turn is in flight, the
     * conversation place never holds two conversations, and the reask budget
     * never holds two seeds (design commitment 6: it bounds the loop only if
     * it never accumulates). libpetri has no weighted output arc, so the
     * verifier models the N-permit seed as one token, and the bound is stated
     * in seeds.
     *
     * <p>Without the permit all three are violated: a second {@code BuildPrompt}
     * started while the first turn loops (review experiment E2). An inhibitor
     * on a turn-active place, which needs no seeded token, proves none of them
     * either: the verifier lets a transition start again while its earlier
     * firing is in flight (VER-004), so two starts both see the place empty.
     */
    @Test
    @EnabledIf("z3Available")
    void llm_agent_runs_one_turn_at_a_time_without_assuming_atomic_firing() {
        SmtProofs.assertEachProven(agentNet(),
                v -> v.initialMarking(b -> b.tokens(AdkColours.TURN_PERMIT, 1))
                        .environmentPlaces(EnvironmentPlace.of(AdkColours.USER_IN))
                        .environmentMode(EnvironmentAnalysisMode.arrivals(K)),
                Map.of(
                        "one turn in flight: placeBound(TURN_ACTIVE, 1)",
                        SmtProperty.placeBound(LlmAgentSubnet.TURN_ACTIVE, 1),
                        "one conversation: placeBound(CONVERSATION, 1)",
                        SmtProperty.placeBound(LlmAgentSubnet.CONVERSATION, 1),
                        "reask budget never stacks: budgetPlaceBounded(REASK_BUDGET, 1)",
                        AdkNetInvariants.budgetPlaceBounded(LlmAgentSubnet.REASK_BUDGET, 1)));
    }

    /**
     * The composed LLM agent turns every user input into exactly one turn
     * outcome: an egress event (the answer, or the reask-exhausted fallback)
     * or a transfer, and comes to rest holding its permit and nothing else of
     * any turn. Exactly {@value #K} inputs arrive ({@code arrivals(K, K)}).
     *
     * <p>The permit is the only place excused as a sink. Every turn's
     * conversation and unspent budget are cleared by the transition that ends
     * it, so a token left on either is a stranded turn.
     */
    @Test
    @EnabledIf("z3Available")
    void llm_agent_turns_every_user_input_into_exactly_one_outcome() {
        var outcomes = List.<Place<?>>of(AdkColours.EVENT_OUT, AdkColours.TRANSFER);
        SmtProofs.assertEachProven(agentNet(),
                v -> v.initialMarking(b -> b.tokens(AdkColours.TURN_PERMIT, 1))
                        .environmentPlaces(EnvironmentPlace.of(AdkColours.USER_IN))
                        .environmentMode(EnvironmentAnalysisMode.arrivals(K, K))
                        .sinkPlaces(AdkColours.EVENT_OUT, AdkColours.TRANSFER, AdkColours.TURN_PERMIT),
                Map.of(
                        "deadlockFree",
                        SmtProperty.deadlockFree(),
                        "one outcome per input: quiescentCount(eventOut + transfer) == K",
                        SmtProperty.quiescentCount(outcomes, K, OptionalInt.of(K))));
    }

    /**
     * A failed transition consumes its inputs and produces nothing. The
     * failure model adds, for every place a turn's single in-flight token can
     * rest on, a transition that takes it and signals
     * {@link AdkColours#TURN_ABORT}, which is what {@code PetriAgent} does on
     * a failure. With failures possible at every step of every turn, the agent
     * still comes to rest holding its permit, and still never runs two turns
     * or holds two conversations.
     *
     * <p>A model error with a recovery callback, and a {@code BeforeModel}
     * short-circuit, are not failures: they land on {@code LLM_RESPONSE} and
     * end the turn through the router like any answer, which the proofs above
     * already cover.
     */
    @Test
    @EnabledIf("z3Available")
    void llm_agent_recovers_from_a_failure_at_any_step_of_a_turn() {
        var net = withFailures(agentNet(), List.of(
                LlmAgentSubnet.TURN_INPUT,
                AdkColours.LLM_REQUEST,
                LlmStepSubnet.Places.READY_TO_CALL,
                LlmStepSubnet.Places.RAW_RESPONSE,
                LlmStepSubnet.Places.LLM_ERROR,
                AdkColours.LLM_RESPONSE,
                AdkColours.TOOL_CALLS,
                AdkColours.TOOL_RESULTS));
        SmtProofs.assertEachProven(net,
                v -> v.initialMarking(b -> b.tokens(AdkColours.TURN_PERMIT, 1))
                        .environmentPlaces(EnvironmentPlace.of(AdkColours.USER_IN))
                        .environmentMode(EnvironmentAnalysisMode.arrivals(K, K))
                        .sinkPlaces(AdkColours.EVENT_OUT, AdkColours.TRANSFER, AdkColours.TURN_PERMIT),
                Map.of(
                        "deadlockFree",
                        SmtProperty.deadlockFree(),
                        "one turn in flight: placeBound(TURN_ACTIVE, 1)",
                        SmtProperty.placeBound(LlmAgentSubnet.TURN_ACTIVE, 1),
                        "one conversation: placeBound(CONVERSATION, 1)",
                        SmtProperty.placeBound(LlmAgentSubnet.CONVERSATION, 1)));
    }

    /**
     * The permit holds whatever the aborts do: with {@link AdkColours#TURN_ABORT}
     * an environment place that receives aborts at any moment, a turn in
     * flight or not, there is still never a second permit and never a second
     * turn. ({@code DropAbort} takes an abort that finds no turn.) What an
     * abort that lands while an action of the turn is still running does to
     * that action's late output is not covered here; ADR 0005 states that
     * limit.
     */
    @Test
    @EnabledIf("z3Available")
    void llm_agent_never_mints_a_second_permit_however_aborts_arrive() {
        SmtProofs.assertEachProven(agentNet(),
                v -> v.initialMarking(b -> b.tokens(AdkColours.TURN_PERMIT, 1))
                        .environmentPlaces(EnvironmentPlace.of(AdkColours.USER_IN),
                                EnvironmentPlace.of(AdkColours.TURN_ABORT))
                        .environmentMode(EnvironmentAnalysisMode.arrivals(K)),
                Map.of(
                        "placeBound(TURN_PERMIT, 1)",
                        SmtProperty.placeBound(AdkColours.TURN_PERMIT, 1),
                        "one turn in flight: placeBound(TURN_ACTIVE, 1)",
                        SmtProperty.placeBound(LlmAgentSubnet.TURN_ACTIVE, 1)));
    }

    /**
     * The SSE agent shares the composition, permit included. Its chunks reach
     * the net through an environment place the verifier cannot tie to the
     * request that streamed them, so only the safety half is claimed here.
     */
    @Test
    @EnabledIf("z3Available")
    void streaming_llm_agent_runs_one_turn_at_a_time() {
        var config = StreamingLlmAgentSubnet.Config.builder("agent", "fake-model")
                .reaskBudget(2)
                .dispatchExecutor(EXECUTOR)
                .executorRef(new AtomicReference<>())
                .build();
        var net = PetriNet.builder("streaming-agent")
                .compose(StreamingLlmAgentSubnet.DEF)
                .build()
                .bindActions(StreamingLlmAgentSubnet.actionBindings(stubLlm(), config));
        SmtProofs.assertEachProven(net,
                v -> v.initialMarking(b -> b.tokens(AdkColours.TURN_PERMIT, 1))
                        .environmentPlaces(EnvironmentPlace.of(AdkColours.USER_IN),
                                EnvironmentPlace.of(LlmStreamingStepSubnet.Places.CHUNK))
                        .environmentMode(EnvironmentAnalysisMode.arrivals(K)),
                Map.of(
                        "one turn in flight: placeBound(TURN_ACTIVE, 1)",
                        SmtProperty.placeBound(LlmAgentSubnet.TURN_ACTIVE, 1),
                        "placeBound(TURN_PERMIT, 1)",
                        SmtProperty.placeBound(AdkColours.TURN_PERMIT, 1),
                        "one conversation: placeBound(CONVERSATION, 1)",
                        SmtProperty.placeBound(LlmAgentSubnet.CONVERSATION, 1)));
    }

    /** {@link LlmAgentSubnet#DEF} composed alone, with its actions bound. */
    private static PetriNet agentNet() {
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .reaskBudget(2)
                .dispatchExecutor(EXECUTOR)
                .build();
        return PetriNet.builder("agent")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(stubLlm(), config));
    }

    /**
     * {@code net} plus, for each place in {@code restingPoints}, a transition
     * {@code Fail_<place>} that takes a token from it and signals
     * {@link AdkColours#TURN_ABORT}: the consuming transition failing (its
     * input gone, no output), followed by {@code PetriAgent}'s abort.
     */
    private static PetriNet withFailures(PetriNet net, List<Place<?>> restingPoints) {
        var builder = PetriNet.builder(net.name() + "-with-failures");
        net.places().forEach(builder::place);
        net.transitions().forEach(builder::transition);
        for (var place : restingPoints) {
            builder.transition(Transition.builder("Fail_" + place.name())
                    .inputs(Arc.In.one(place))
                    .outputs(Arc.Out.place(AdkColours.TURN_ABORT))
                    .action(ctx -> {
                        ctx.input(place);
                        ctx.output(AdkColours.TURN_ABORT, (Void) null);
                        return CompletableFuture.completedFuture(null);
                    })
                    .build());
        }
        return builder.build();
    }

    // ============================================================
    //  Helpers
    // ============================================================

    /**
     * Feeds exactly {@value #K} inputs to {@code inPort} and proves the subnet
     * deadlock-free with {@code outs} as its only sinks, and at rest with
     * exactly {@value #K} tokens across them.
     */
    private static void assertOneOutcomePerInput(
            SubnetDef<Void> def, String inPort, Supplier<Token<?>> input, Place<?>... outs) {
        var result = def.verify(
                VerificationHarness.builder()
                        .input(inPort, input)
                        .property(SmtProperty.deadlockFree())
                        .property(SmtProperty.quiescentCount(List.of(outs), K, OptionalInt.of(K)))
                        .build(),
                SubnetVerifyOptions.DEFAULT
                        .withEnvironmentMode(EnvironmentAnalysisMode.arrivals(K, K))
                        .withConfigure((v, synthetic) -> v.sinkPlaces(outs)));
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
