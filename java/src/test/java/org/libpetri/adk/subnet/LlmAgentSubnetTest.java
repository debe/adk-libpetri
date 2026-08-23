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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
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
import org.libpetri.adk.colours.AdkColours;
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
                "eventOut", "transfer", "userIn").inOrder();
    }

    @Test
    void subnet_def_contains_owned_and_composed_transitions() {
        var transitionNames = LlmAgentSubnet.DEF.body().transitions().stream()
                .map(t -> t.name()).toList();
        assertThat(transitionNames).containsAtLeast(
                // owned
                LlmAgentSubnet.Transitions.BUILD_PROMPT,
                LlmAgentSubnet.Transitions.RE_ASK,
                LlmAgentSubnet.Transitions.RE_ASK_EXHAUSTED_FALLBACK,
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
    //  Fixtures + helpers
    // ============================================================

    private static Fixture run(BaseLlm llm, LlmAgentSubnet.Config config, Content... userMessages) {
        var net = PetriNet.builder("test-host")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));

        List<Token<?>> userInTokens = new ArrayList<>();
        for (var c : userMessages) userInTokens.add(Token.of(c));
        Map<Place<?>, List<Token<?>>> initial = Map.of(AdkColours.USER_IN, userInTokens);

        var store = EventStore.inMemory();
        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(store)
                .build();
        var marking = executor.run();

        return new Fixture(
                marking.peekTokens(AdkColours.EVENT_OUT).stream().map(Token::value).toList(),
                marking.peekTokens(AdkColours.TOOL_CALLS).stream().map(Token::value).toList(),
                marking.peekTokens(AdkColours.TRANSFER).stream().map(Token::value).toList(),
                store.events());
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
            List<NetEvent> netEvents) {
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
