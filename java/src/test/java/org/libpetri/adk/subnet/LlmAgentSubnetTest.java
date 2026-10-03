package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.tools.BaseTool;
import com.google.adk.tools.ToolContext;
import com.google.genai.types.Content;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.core.Single;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;
import org.libpetri.adk.bridge.TransitionFailure;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.runtime.BitmapNetExecutor;

class LlmAgentSubnetTest {

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
    //  Hello-world: text-only response, single turn
    // ============================================================

    @Test
    void single_turn_text_response_lands_on_event_out() {
        var llm = scriptedLlm(textResponse("Hi back!"));
        var config = LlmAgentSubnet.Config.builder("hello", "fake-model")
                .systemInstruction("Greet the user warmly.")
                .dispatchExecutor(EXECUTOR)
                .build();

        var fixture = run(llm, config, userMessage("hi"));

        assertThat(fixture.events()).hasSize(1);
        assertThat(fixture.events().get(0).content().get().text()).isEqualTo("Hi back!");
        assertThat(fixture.events().get(0).author()).isEqualTo("hello");

        // No tool calls, no transfer.
        assertThat(fixture.toolCalls()).isEmpty();
        assertThat(fixture.transfers()).isEmpty();

        // Sanity on fired transitions: BuildPrompt + LlmStep pipeline + Router; no
        // ReAsk and no fallback.
        assertThat(fixture.firedTransitionNames())
                .containsAtLeast(
                        LlmAgentSubnet.Transitions.BUILD_PROMPT,
                        LlmStepSubnet.Transitions.BEFORE_MODEL,
                        LlmStepSubnet.Transitions.LLM_CALL,
                        LlmStepSubnet.Transitions.AFTER_MODEL,
                        RouterSubnet.Transitions.ROUTE);
        assertThat(fixture.firedTransitionNames())
                .doesNotContain(LlmAgentSubnet.Transitions.RE_ASK);
        assertThat(fixture.firedTransitionNames())
                .doesNotContain(LlmAgentSubnet.Transitions.RE_ASK_EXHAUSTED_FALLBACK);
    }

    // ============================================================
    //  Tool-call → tool-result → re-ask → text (two turns)
    // ============================================================

    @Test
    void tool_call_then_text_response_two_llm_turns() {
        var calc = fakeTool("calculate", args -> Map.of("answer", 42));
        var llm = scriptedLlm(
                responseWithCalls(FunctionCall.builder()
                        .name("calculate").args(Map.of("expr", "6*7")).id("c1").build()),
                textResponse("The answer is 42."));

        var config = LlmAgentSubnet.Config.builder("calc-agent", "fake-model")
                .tools(Map.of("calculate", calc))
                .reaskBudget(3)
                .dispatchExecutor(EXECUTOR)
                .build();

        var fixture = run(llm, config, userMessage("calc 6*7"));

        assertThat(fixture.events()).hasSize(1);
        assertThat(fixture.events().get(0).content().get().text()).isEqualTo("The answer is 42.");
        assertThat(fixture.firedTransitionNames())
                .contains(LlmAgentSubnet.Transitions.RE_ASK);
        assertThat(fixture.firedTransitionNames())
                .contains(ToolDispatchSubnet.Transitions.DISPATCH);
        // The pipeline fired LlmCall twice.
        assertThat(fixture.firedTransitionNames().stream()
                .filter(LlmStepSubnet.Transitions.LLM_CALL::equals)
                .count())
                .isEqualTo(2);
    }

    /**
     * Each re-ask must carry the whole invocation, not the tool responses
     * alone: the user turn, then every model function-call turn verbatim
     * (Gemini 3 rejects a call turn stripped of its thought signature) followed
     * by its function-response turn. The re-ask used to send only the
     * responses, which Gemini rejects because it pairs each response with the
     * preceding call.
     */
    @Test
    void reask_requests_carry_the_full_conversation_including_the_model_call_turn() {
        var calc = fakeTool("calculate", args -> Map.of("answer", 42));
        byte[] signature = {7, 7, 7};
        var firstCall = LlmResponse.builder()
                .content(Content.builder().role("model").parts(List.of(
                        Part.builder().thoughtSignature(signature).functionCall(FunctionCall.builder()
                                .name("calculate").args(Map.of("expr", "6*7")).id("c1").build())
                                .build()))
                        .build())
                .build();
        var secondCall = responseWithCalls(FunctionCall.builder()
                .name("calculate").args(Map.of("expr", "42+0")).id("c2").build());
        var requests = new ArrayList<LlmRequest>();
        var llm = capturingLlm(requests, firstCall, secondCall, textResponse("42."));

        var config = LlmAgentSubnet.Config.builder("calc-agent", "fake-model")
                .tools(Map.of("calculate", calc))
                .reaskBudget(3)
                .dispatchExecutor(EXECUTOR)
                .build();

        var user = userMessage("calc 6*7");
        var fixture = run(llm, config, user);

        assertThat(fixture.events()).hasSize(1);
        assertThat(requests).hasSize(3);
        assertThat(requests.get(0).contents()).containsExactly(user);

        var firstReAsk = requests.get(1).contents();
        assertThat(firstReAsk).hasSize(3);
        assertThat(firstReAsk.get(0)).isEqualTo(user);
        // The model turn goes back verbatim, signature and all.
        assertThat(firstReAsk.get(1)).isEqualTo(firstCall.content().get());
        assertThat(firstReAsk.get(1).parts().get().get(0).thoughtSignature().get())
                .isEqualTo(signature);
        assertThat(firstReAsk.get(2).role()).hasValue("user");
        assertThat(firstReAsk.get(2).parts().get().get(0).functionResponse().get().id())
                .hasValue("c1");

        // The second hop keeps accumulating rather than starting over.
        var secondReAsk = requests.get(2).contents();
        assertThat(secondReAsk).hasSize(5);
        assertThat(secondReAsk.subList(0, 3)).isEqualTo(firstReAsk);
        assertThat(secondReAsk.get(3)).isEqualTo(secondCall.content().get());
        assertThat(secondReAsk.get(4).parts().get().get(0).functionResponse().get().id())
                .hasValue("c2");
    }

    // ============================================================
    //  Reask budget exhaustion → canned fallback
    // ============================================================

    @Test
    void reask_budget_exhaustion_emits_fallback_event_not_indefinite_loop() {
        var tool = fakeTool("alwaysCall", args -> Map.of("ok", true));
        // LLM always returns a tool call → would loop forever without the budget.
        var llm = endlesslyToolCallingLlm("alwaysCall");

        var fallback = Content.fromParts(Part.fromText("budget out — sorry"));
        var config = LlmAgentSubnet.Config.builder("looper", "fake-model")
                .tools(Map.of("alwaysCall", tool))
                .reaskBudget(2)
                .fallbackContent(fallback)
                .dispatchExecutor(EXECUTOR)
                .build();

        var fixture = run(llm, config, userMessage("loop please"));

        assertThat(fixture.events()).hasSize(1);
        assertThat(fixture.events().get(0).content().get().text()).isEqualTo("budget out — sorry");
        assertThat(fixture.events().get(0).author()).isEqualTo("looper");

        // BuildPrompt(1) + LlmCall(initial) + ReAsk(twice — uses both budget tokens)
        //   + LlmCall(twice more after each ReAsk) + ExhaustedFallback(once).
        // Budget=2 → 2 re-asks possible → LLM fires 3 times total (initial + 2).
        var llmCalls = fixture.firedTransitionNames().stream()
                .filter(LlmStepSubnet.Transitions.LLM_CALL::equals)
                .count();
        assertThat(llmCalls).isEqualTo(3);
        var reAsks = fixture.firedTransitionNames().stream()
                .filter(LlmAgentSubnet.Transitions.RE_ASK::equals)
                .count();
        assertThat(reAsks).isEqualTo(2);
        assertThat(fixture.firedTransitionNames())
                .contains(LlmAgentSubnet.Transitions.RE_ASK_EXHAUSTED_FALLBACK);
    }

    @Test
    void reask_budget_one_means_one_reask_then_fallback() {
        var tool = fakeTool("c", args -> Map.of());
        var llm = endlesslyToolCallingLlm("c");
        var config = LlmAgentSubnet.Config.builder("one-shot", "fake-model")
                .tools(Map.of("c", tool))
                .reaskBudget(1)
                .dispatchExecutor(EXECUTOR)
                .build();

        var fixture = run(llm, config, userMessage("go"));

        var reAsks = fixture.firedTransitionNames().stream()
                .filter(LlmAgentSubnet.Transitions.RE_ASK::equals)
                .count();
        assertThat(reAsks).isEqualTo(1);
        assertThat(fixture.firedTransitionNames())
                .contains(LlmAgentSubnet.Transitions.RE_ASK_EXHAUSTED_FALLBACK);
    }

    // ============================================================
    //  Reask-budget reset on a new user message
    // ============================================================

    @Test
    void reask_budget_is_reset_on_each_new_user_input() {
        // Two user messages in the same execution. Each should get a fresh budget.
        // First message: 1 tool call then text. Second message: 1 tool call then text.
        // If budgets leaked across, we'd see different LLM call counts.
        var tool = fakeTool("t", args -> Map.of("v", 1));
        var llm = scriptedLlm(
                responseWithCalls(FunctionCall.builder().name("t").args(Map.of()).build()),
                textResponse("first done"),
                responseWithCalls(FunctionCall.builder().name("t").args(Map.of()).build()),
                textResponse("second done"));

        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .tools(Map.of("t", tool))
                .reaskBudget(3)
                .dispatchExecutor(EXECUTOR)
                .build();

        var fixture = run(llm, config, userMessage("msg one"), userMessage("msg two"));

        assertThat(fixture.events()).hasSize(2);
        assertThat(fixture.events().get(0).content().get().text()).isEqualTo("first done");
        assertThat(fixture.events().get(1).content().get().text()).isEqualTo("second done");
        // No fallback fired.
        assertThat(fixture.firedTransitionNames())
                .doesNotContain(LlmAgentSubnet.Transitions.RE_ASK_EXHAUSTED_FALLBACK);
    }

    // ============================================================
    //  Transfer routing (LLM emits transfer_to_agent) — token lands on TRANSFER port
    // ============================================================

    @Test
    void transfer_to_agent_routes_to_transfer_output_port() {
        var transfer = FunctionCall.builder()
                .name(RouterSubnet.TRANSFER_TO_AGENT_FN)
                .args(Map.of(RouterSubnet.TRANSFER_AGENT_NAME_ARG, "specialist"))
                .build();
        var llm = scriptedLlm(responseWithCalls(transfer));

        var config = LlmAgentSubnet.Config.builder("router-agent", "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build();
        var fixture = run(llm, config, userMessage("send me to the specialist"));

        assertThat(fixture.transfers()).hasSize(1);
        assertThat(fixture.transfers().get(0).agentName()).isEqualTo("specialist");
        assertThat(fixture.events()).isEmpty();
        assertThat(fixture.toolCalls()).isEmpty();
    }

    // ============================================================
    //  Interface shape — public agent subnet ports
    // ============================================================

    @Test
    void subnet_interface_exposes_public_agent_ports() {
        var portNames = LlmAgentSubnet.DEF.iface().ports().stream()
                .map(p -> p.name()).sorted().toList();
        assertThat(portNames).containsExactly(
                "eventOut", "transfer", "turnAbort", "userIn").inOrder();
    }

    @Test
    void subnet_def_contains_owned_and_composed_transitions() {
        var transitionNames = LlmAgentSubnet.DEF.body().transitions().stream()
                .map(t -> t.name()).toList();
        assertThat(transitionNames).containsAtLeast(
                // owned
                LlmAgentSubnet.Transitions.START_TURN,
                LlmAgentSubnet.Transitions.BUILD_PROMPT,
                LlmAgentSubnet.Transitions.RE_ASK,
                LlmAgentSubnet.Transitions.RE_ASK_EXHAUSTED_FALLBACK,
                LlmAgentSubnet.Transitions.EMIT_ANSWER,
                LlmAgentSubnet.Transitions.EMIT_TRANSFER,
                LlmAgentSubnet.Transitions.ABORT_TURN,
                LlmAgentSubnet.Transitions.DROP_ABORT,
                // from LlmStepSubnet
                LlmStepSubnet.Transitions.BEFORE_MODEL,
                LlmStepSubnet.Transitions.LLM_CALL,
                LlmStepSubnet.Transitions.AFTER_MODEL,
                LlmStepSubnet.Transitions.ON_MODEL_ERROR,
                // from RouterSubnet
                RouterSubnet.Transitions.ROUTE,
                // from ToolDispatchSubnet
                ToolDispatchSubnet.Transitions.DISPATCH);
    }

    // ============================================================
    //  Action-binding validation — missing or extra keys rejected
    // ============================================================

    @Test
    void config_invalid_reask_budget_zero_throws() {
        var ex = Assertions.assertThrows(
                IllegalArgumentException.class,
                () -> LlmAgentSubnet.Config.builder("x", "y").reaskBudget(0).build());
        assertThat(ex.getMessage()).contains("reaskBudget must be >= 1");
    }

    // ============================================================
    //  Turn permit — one turn at a time, every end returns the permit
    // ============================================================

    /**
     * Each way a turn ends (answer, tool loop then answer, reask-exhausted
     * fallback, transfer) gives the permit back and leaves nothing else of
     * the turn behind: no conversation, no unspent budget.
     */
    @Test
    void every_way_a_turn_ends_leaves_only_the_permit_behind() {
        var tool = fakeTool("t", args -> Map.of("v", 1));
        var transfer = FunctionCall.builder()
                .name(RouterSubnet.TRANSFER_TO_AGENT_FN)
                .args(Map.of(RouterSubnet.TRANSFER_AGENT_NAME_ARG, "specialist"))
                .build();
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .tools(Map.of("t", tool))
                .reaskBudget(2)
                .dispatchExecutor(EXECUTOR)
                .build();
        var onlyThePermit = Map.of(AdkColours.TURN_PERMIT.name(), 1);

        assertThat(run(scriptedLlm(textResponse("hi")), config, userMessage("a")).resting())
                .isEqualTo(onlyThePermit);
        assertThat(run(scriptedLlm(
                        responseWithCalls(FunctionCall.builder().name("t").build()),
                        textResponse("done")),
                config, userMessage("b")).resting())
                .isEqualTo(onlyThePermit);
        assertThat(run(endlesslyToolCallingLlm("t"), config, userMessage("c")).resting())
                .isEqualTo(onlyThePermit);
        var handedOff = run(scriptedLlm(responseWithCalls(transfer)), config, userMessage("d"));
        assertThat(handedOff.transfers()).hasSize(1);
        assertThat(handedOff.resting()).isEqualTo(onlyThePermit);
    }

    /**
     * Replays the marking the overlapping-turn bug started from (review
     * experiment E2): turn 1 is in its tool loop, holding the permit, with its
     * tool results, one budget token and its conversation on the places, and
     * turn 2's input has just arrived.
     *
     * <p>Turn 2's prompt build used to fire right away and reset the budget
     * and the conversation turn 1 was about to re-ask with. Turn 1's re-ask
     * then took turn 2's conversation, and because a reset only sees the
     * marking a pass started with, both conversations could end up on the
     * place, so turn 2 replayed turn 1's turns. Now turn 2 waits for the
     * permit: turn 1 re-asks with its own conversation and answers, then
     * turn 2 starts with nothing but its own input.
     */
    @Test
    void an_input_that_arrives_mid_turn_waits_for_that_turn_to_end() {
        var requests = new ArrayList<LlmRequest>();
        var llm = capturingLlm(requests, textResponse("turn-1 answer"), textResponse("turn-2 answer"));
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .reaskBudget(2)
                .dispatchExecutor(EXECUTOR)
                .build();
        var net = PetriNet.builder("test-host")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));

        var user1 = userMessage("turn-1 user");
        var user2 = userMessage("turn-2 user");
        var call1 = Content.builder().role("model").parts(List.of(Part.builder()
                .functionCall(FunctionCall.builder().name("t1").id("c1").build()).build())).build();
        var results1 = new AdkColours.ToolResults(List.of(com.google.genai.types.FunctionResponse
                .builder().name("t1").id("c1").response(Map.of("r", "turn-1 result")).build()), call1);

        Map<Place<?>, List<Token<?>>> midTurnOne = Map.of(
                AdkColours.USER_IN, List.of(Token.of(user2)),
                AdkColours.TOOL_RESULTS, List.of(Token.of(results1)),
                LlmAgentSubnet.REASK_BUDGET, List.of(Token.unit()),
                LlmAgentSubnet.CONVERSATION, List.of(Token.of(
                        new LlmAgentSubnet.Conversation(List.of(user1)))),
                LlmAgentSubnet.TURN_ACTIVE, List.of(Token.unit()));
        var marking = BitmapNetExecutor.builder(net, midTurnOne).build().run();

        assertThat(requests).hasSize(2);
        var turnOneReAsk = requests.get(0).contents();
        assertThat(turnOneReAsk).hasSize(3);
        assertThat(turnOneReAsk.get(0)).isEqualTo(user1);
        assertThat(turnOneReAsk.get(1)).isEqualTo(call1);
        assertThat(requests.get(1).contents()).containsExactly(user2);

        var answers = marking.peekTokens(AdkColours.EVENT_OUT).stream()
                .map(t -> t.value().content().get().text()).toList();
        assertThat(answers).containsExactly("turn-1 answer", "turn-2 answer").inOrder();
        assertThat(restingTokens(net, marking, AdkColours.EVENT_OUT))
                .isEqualTo(Map.of(AdkColours.TURN_PERMIT.name(), 1));
    }

    /**
     * The same overlap, live: turn 2's input is injected while turn 1's tool
     * call is still running. It waits for turn 1 to answer, and its request
     * carries only its own conversation.
     */
    @Test
    void overlapping_turns_on_a_running_net_each_keep_their_own_conversation() throws Exception {
        var toolEntered = new CountDownLatch(1);
        var releaseTool = new CountDownLatch(1);
        var slow = fakeTool("slow", args -> {
            toolEntered.countDown();
            try {
                releaseTool.await();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return Map.of("ok", true);
        });
        var requests = new ArrayList<LlmRequest>();
        var llm = capturingLlm(requests,
                responseWithCalls(FunctionCall.builder().name("slow").id("s1").build()),
                textResponse("turn-1 answer"),
                textResponse("turn-2 answer"));
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .tools(Map.of("slow", slow))
                .dispatchExecutor(EXECUTOR)
                .build();
        var net = PetriNet.builder("test-host")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));

        var user1 = userMessage("turn-1 user");
        var user2 = userMessage("turn-2 user");
        try (var runner = PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .orchestratorExecutor(EXECUTOR)
                .start()) {
            var events = runner.adkEvents().test();
            runner.inject(AdkColours.USER_IN, user1).get(5, TimeUnit.SECONDS);
            assertThat(toolEntered.await(5, TimeUnit.SECONDS)).isTrue();
            assertThat(runner.inject(AdkColours.USER_IN, user2).get(5, TimeUnit.SECONDS)).isTrue();
            releaseTool.countDown();

            assertThat(events.awaitCount(2).values().stream()
                    .map(e -> e.content().get().text()).toList())
                    .containsExactly("turn-1 answer", "turn-2 answer").inOrder();
        }

        synchronized (requests) {
            assertThat(requests).hasSize(3);
            assertThat(requests.get(0).contents()).containsExactly(user1);
            assertThat(requests.get(1).contents()).hasSize(3);
            assertThat(requests.get(1).contents().get(0)).isEqualTo(user1);
            assertThat(requests.get(2).contents()).containsExactly(user2);
        }
    }

    /**
     * A transition that fails consumes its inputs and produces nothing, so
     * the turn it belonged to holds the permit with nothing left to end it.
     * A model error with no recovery callback is that case. A
     * {@link AdkColours#TURN_ABORT} clears the turn and returns the permit,
     * and the next input runs with a fresh conversation.
     */
    @Test
    void a_turn_abort_clears_a_failed_turn_and_the_next_input_runs() throws Exception {
        var requests = new ArrayList<LlmRequest>();
        var calls = new AtomicInteger();
        var llm = new BaseLlm("fails-first") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                synchronized (requests) { requests.add(r); }
                return calls.getAndIncrement() == 0
                        ? Flowable.error(new IllegalStateException("model exploded"))
                        : Flowable.just(textResponse("recovered"));
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build();

        assertAbortRecovers(llm, config, requests,
                LlmStepSubnet.Transitions.ON_MODEL_ERROR);
    }

    /**
     * The same recovery from a failure in the middle of the tool loop, where
     * the turn has a conversation and budget on the places: the tool context
     * supplier throws, so dispatch fails. The abort resets both, and the next
     * turn starts with its own.
     */
    @Test
    void a_turn_abort_clears_a_turn_that_failed_in_its_tool_loop() throws Exception {
        var requests = new ArrayList<LlmRequest>();
        var llm = capturingLlm(requests,
                responseWithCalls(FunctionCall.builder().name("t").id("c1").build()),
                textResponse("recovered"));
        var tool = fakeTool("t", args -> Map.of());
        var dispatches = new AtomicInteger();
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .tools(Map.of("t", tool))
                .dispatchExecutor(EXECUTOR)
                .toolContextSupplier(() -> {
                    if (dispatches.getAndIncrement() == 0) {
                        throw new IllegalStateException("no tool context");
                    }
                    return null;
                })
                .build();

        assertAbortRecovers(llm, config, requests, ToolDispatchSubnet.Transitions.DISPATCH);
    }

    /**
     * An abort with no turn in flight is dropped, not kept for the next turn
     * and not turned into a second permit: the next turn runs, and afterwards
     * there is still exactly one permit.
     */
    @Test
    void a_turn_abort_with_no_turn_in_flight_is_dropped() throws Exception {
        var llm = scriptedLlm(textResponse("fine"));
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build();
        var net = PetriNet.builder("test-host")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));

        try (var runner = PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .orchestratorExecutor(EXECUTOR)
                .start()) {
            var events = runner.adkEvents().test();
            runner.signal(AdkColours.TURN_ABORT).get(5, TimeUnit.SECONDS);
            runner.inject(AdkColours.USER_IN, userMessage("go")).get(5, TimeUnit.SECONDS);
            events.awaitCount(1);
            assertThat(events.values().get(0).content().get().text()).isEqualTo("fine");
            awaitResting(runner, Map.of(AdkColours.TURN_PERMIT.name(), 1));
        }
    }

    /**
     * A stale abort that lands in the same pass as an input, with the permit
     * at rest, is dropped before the input starts its turn. If the input won
     * the permit first, the abort would find a turn in flight and wipe it.
     */
    @Test
    void a_stale_turn_abort_in_the_same_pass_as_an_input_does_not_abort_it() {
        var llm = scriptedLlm(textResponse("fine"));
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build();
        var net = PetriNet.builder("test-host")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));
        Map<Place<?>, List<Token<?>>> initial = Map.of(
                AdkColours.USER_IN, List.of(Token.of(userMessage("go"))),
                AdkColours.TURN_ABORT, List.of(Token.unit()),
                AdkColours.TURN_PERMIT, List.of(Token.unit()));

        var marking = BitmapNetExecutor.builder(net, initial).build().run();

        var answers = List.copyOf(marking.peekTokens(AdkColours.EVENT_OUT));
        assertThat(answers).hasSize(1);
        assertThat(answers.get(0).value().content().get().text()).isEqualTo("fine");
        assertThat(restingTokens(net, marking, AdkColours.EVENT_OUT))
                .isEqualTo(Map.of(AdkColours.TURN_PERMIT.name(), 1));
    }

    /**
     * An agent composed through {@code DEF.instantiate(prefix)} has its own
     * prefixed permit, which {@code PetriRunner} does not seed. A fresh start
     * that leaves it empty fails loudly instead of never starting a turn, and
     * one that seeds it runs.
     */
    @Test
    void an_instantiated_agent_needs_its_prefixed_permit_seeded() throws Exception {
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build();
        var agent = LlmAgentSubnet.DEF.instantiate("billing")
                .bindActions(LlmAgentSubnet.actionBindings(scriptedLlm(textResponse("fine")), config));
        var net = PetriNet.builder("test-host")
                .compose(agent, b -> b.bindPort("userIn", AdkColours.USER_IN)
                        .bindPort("turnAbort", AdkColours.TURN_ABORT)
                        .bindPort("eventOut", AdkColours.EVENT_OUT)
                        .bindPort("transfer", AdkColours.TRANSFER))
                .build();
        var permit = Place.of("billing/" + AdkColours.TURN_PERMIT.name(), Void.class);
        assertThat(net.places()).contains(permit);

        var unseeded = PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .orchestratorExecutor(EXECUTOR);
        var thrown = Assertions.assertThrows(IllegalStateException.class, unseeded::start);
        assertThat(thrown).hasMessageThat().contains(permit.name());

        Map<Place<?>, List<Token<?>>> seeded = Map.of(permit, List.of(Token.unit()));
        try (var runner = PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .initialMarking(seeded)
                .orchestratorExecutor(EXECUTOR)
                .start()) {
            var events = runner.adkEvents().test();
            runner.inject(AdkColours.USER_IN, userMessage("go")).get(5, TimeUnit.SECONDS);
            events.awaitCount(1);
            assertThat(events.values().get(0).content().get().text()).isEqualTo("fine");
            awaitResting(runner, Map.of(permit.name(), 1));
        }
    }

    /**
     * Drives one failing turn and one recovering turn on a running net,
     * signalling the abort the way {@code PetriAgent} does: on the failure.
     */
    private static void assertAbortRecovers(BaseLlm llm, LlmAgentSubnet.Config config,
                                            List<LlmRequest> requests, String failingTransition)
            throws Exception {
        var net = PetriNet.builder("test-host")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));
        var user1 = userMessage("fails");
        var user2 = userMessage("recovers");
        try (var runner = PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .orchestratorExecutor(EXECUTOR)
                .start()) {
            var failures = runner.failureSignal().test();
            var events = runner.adkEvents().test();
            runner.inject(AdkColours.USER_IN, user1).get(5, TimeUnit.SECONDS);
            failures.awaitCount(1);
            assertThat(((TransitionFailure) failures.values().get(0)).transitionName())
                    .isEqualTo(failingTransition);

            // Wedged: the failed turn still holds the permit, so this input queues.
            runner.inject(AdkColours.USER_IN, user2).get(5, TimeUnit.SECONDS);
            assertThat(runner.snapshot().marking()).containsKey(AdkColours.USER_IN.name());
            assertThat(runner.snapshot().marking()).doesNotContainKey(AdkColours.TURN_PERMIT.name());

            runner.signal(AdkColours.TURN_ABORT).get(5, TimeUnit.SECONDS);
            events.awaitCount(1);
            assertThat(events.values().get(0).content().get().text()).isEqualTo("recovered");
            awaitResting(runner, Map.of(AdkColours.TURN_PERMIT.name(), 1));
        }
        synchronized (requests) {
            assertThat(requests.getLast().contents()).containsExactly(user2);
        }
    }

    /** Waits until the running net's marking, egress aside, is {@code expected}. */
    private static void awaitResting(PetriRunner runner, Map<String, Integer> expected)
            throws InterruptedException {
        Map<String, Integer> resting = Map.of();
        for (int i = 0; i < 500; i++) {
            resting = new TreeMap<String, Integer>();
            for (var e : runner.snapshot().marking().entrySet()) {
                if (!e.getKey().equals(AdkColours.EVENT_OUT.name()) && !e.getValue().isEmpty()) {
                    resting.put(e.getKey(), e.getValue().size());
                }
            }
            if (resting.equals(expected)) return;
            Thread.sleep(10);
        }
        assertThat(resting).isEqualTo(expected);
    }

    // ============================================================
    //  Fixtures + helpers
    // ============================================================

    private static Fixture run(BaseLlm llm, LlmAgentSubnet.Config config, Content... userMessages) {
        var net = PetriNet.builder("test-host")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));

        List<Token<?>> userInTokens = new ArrayList<>();
        for (var c : userMessages) userInTokens.add(Token.of(c));
        // A bare executor seeds the turn permit itself; PetriRunner would.
        Map<Place<?>, List<Token<?>>> initial = Map.of(
                AdkColours.USER_IN, userInTokens,
                AdkColours.TURN_PERMIT, List.of(Token.unit()));

        var store = EventStore.inMemory();
        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(store)
                .build();
        var marking = executor.run();

        return new Fixture(
                marking.peekTokens(AdkColours.EVENT_OUT).stream().map(Token::value).toList(),
                marking.peekTokens(AdkColours.TOOL_CALLS).stream().map(Token::value).toList(),
                marking.peekTokens(AdkColours.TRANSFER).stream().map(Token::value).toList(),
                store.events(),
                restingTokens(net, marking, AdkColours.EVENT_OUT, AdkColours.TRANSFER));
    }

    /** Token count per place name, for every marked place but the egress ones. */
    private static Map<String, Integer> restingTokens(
            PetriNet net, org.libpetri.runtime.Marking marking, Place<?>... egress) {
        var resting = new TreeMap<String, Integer>();
        for (var place : net.places()) {
            int n = marking.peekTokens(place).size();
            if (n > 0 && !List.of(egress).contains(place)) resting.put(place.name(), n);
        }
        return resting;
    }

    private static Content userMessage(String text) {
        return Content.builder().role("user")
                .parts(List.of(Part.fromText(text))).build();
    }

    private static LlmResponse textResponse(String text) {
        return LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText(text))).build())
                .build();
    }

    private static LlmResponse responseWithCalls(FunctionCall... calls) {
        var parts = new ArrayList<Part>();
        for (var c : calls) parts.add(Part.builder().functionCall(c).build());
        return LlmResponse.builder()
                .content(Content.builder().role("model").parts(parts).build())
                .build();
    }

    private static BaseLlm scriptedLlm(LlmResponse... responses) {
        Deque<LlmResponse> queue = new ArrayDeque<>(List.of(responses));
        return new BaseLlm("scripted") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                var next = queue.poll();
                if (next == null) {
                    return Flowable.error(new IllegalStateException(
                            "scriptedLlm exhausted — test invoked the LLM more times than scripted"));
                }
                return Flowable.just(next);
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    /** Like {@link #scriptedLlm}, but records every request it is sent. */
    private static BaseLlm capturingLlm(List<LlmRequest> sink, LlmResponse... responses) {
        Deque<LlmResponse> queue = new ArrayDeque<>(List.of(responses));
        return new BaseLlm("capturing") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                synchronized (sink) { sink.add(r); }
                var next = queue.poll();
                if (next == null) {
                    return Flowable.error(new IllegalStateException(
                            "capturingLlm exhausted — test invoked the LLM more times than scripted"));
                }
                return Flowable.just(next);
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    private static BaseLlm endlesslyToolCallingLlm(String toolName) {
        var counter = new AtomicInteger();
        return new BaseLlm("looper") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                var call = FunctionCall.builder()
                        .name(toolName)
                        .args(Map.of("attempt", counter.incrementAndGet()))
                        .build();
                return Flowable.just(responseWithCalls(call));
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    private static BaseTool fakeTool(String name,
                                     Function<Map<String, Object>, Map<String, Object>> impl) {
        return new BaseTool(name, "test") {
            @Override public Single<Map<String, Object>> runAsync(Map<String, Object> args, ToolContext c) {
                return Single.fromCallable(() -> impl.apply(args));
            }
        };
    }

    private record Fixture(
            List<Event> events,
            List<AdkColours.ToolCalls> toolCalls,
            List<AdkColours.TransferTarget> transfers,
            List<NetEvent> netEvents,
            Map<String, Integer> resting) {
        List<String> firedTransitionNames() {
            return netEvents.stream()
                    .filter(NetEvent.TransitionStarted.class::isInstance)
                    .map(e -> ((NetEvent.TransitionStarted) e).transitionName())
                    .toList();
        }
    }

    /**
     * The composite must forward the toolContextSupplier it is given.
     *
     * <p>It used to hard-code {@code () -> null} when delegating to
     * {@link ToolDispatchSubnet}, so a tool needing state, artifacts or auth
     * could not be used through {@code LlmAgentSubnet} at all, with no way to
     * override it from the outside.
     */
    @Test
    void the_composite_forwards_the_configured_tool_context_supplier() {
        var marker = org.mockito.Mockito.mock(com.google.adk.tools.ToolContext.class);
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .dispatchExecutor(EXECUTOR)
                .toolContextSupplier(() -> marker)
                .build();

        assertThat(config.toolContextSupplier().get()).isSameInstanceAs(marker);
        // Default stays the documented () -> null rather than becoming required.
        var plain = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build();
        assertThat(plain.toolContextSupplier().get()).isNull();
        assertThat(plain.callbacks()).isEqualTo(LlmStepSubnet.Callbacks.none());
    }
}
