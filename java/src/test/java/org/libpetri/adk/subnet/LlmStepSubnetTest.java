package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.TransitionAction;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.runtime.BitmapNetExecutor;

class LlmStepSubnetTest {

    // ============================================================
    //  Happy path
    // ============================================================

    @Test
    void llm_call_routes_response_to_output_port() {
        var response = textResponse("hello world");
        var baseLlm = new FakeBaseLlm(req -> Flowable.just(response));

        var fixture = run(baseLlm, LlmStepSubnet.Callbacks.none(), simpleRequest("hi"));

        assertThat(fixture.responses()).containsExactly(response);
        assertThat(fixture.firedTransitionNames())
                .containsExactly(
                        LlmStepSubnet.Transitions.BEFORE_MODEL,
                        LlmStepSubnet.Transitions.LLM_CALL,
                        LlmStepSubnet.Transitions.AFTER_MODEL)
                .inOrder();
    }

    @Test
    void multiple_initial_requests_each_produce_one_response() {
        var r1 = textResponse("first");
        var r2 = textResponse("second");
        var seq = new AtomicReference<>(List.of(r1, r2));
        var baseLlm = new FakeBaseLlm(req -> Flowable.just(seq.getAndUpdate(l -> l.subList(1, l.size())).get(0)));

        // Drop two LlmRequest tokens into the initial marking.
        var fixture = runWithInitialRequests(baseLlm, LlmStepSubnet.Callbacks.none(),
                simpleRequest("q1"), simpleRequest("q2"));

        assertThat(fixture.responses()).containsExactly(r1, r2);
    }

    // ============================================================
    //  Before-model short-circuit
    // ============================================================

    @Test
    void before_model_short_circuit_skips_llm_call_and_after_model() {
        var canned = textResponse("canned");
        var llmCalls = new AtomicInteger(0);
        var baseLlm = new FakeBaseLlm(req -> {
            llmCalls.incrementAndGet();
            return Flowable.just(textResponse("from model"));
        });
        var callbacks = LlmStepSubnet.Callbacks.builder()
                .beforeModel(req -> Optional.of(canned))
                .build();

        var fixture = run(baseLlm, callbacks, simpleRequest("anything"));

        assertThat(fixture.responses()).containsExactly(canned);
        assertThat(llmCalls.get()).isEqualTo(0);
        assertThat(fixture.firedTransitionNames())
                .containsExactly(LlmStepSubnet.Transitions.BEFORE_MODEL);
    }

    @Test
    void before_model_returning_empty_optional_continues_to_llm_call() {
        var modelResponse = textResponse("model said");
        var baseLlm = new FakeBaseLlm(req -> Flowable.just(modelResponse));
        var sawBefore = new AtomicReference<LlmRequest>();
        var callbacks = LlmStepSubnet.Callbacks.builder()
                .beforeModel(req -> { sawBefore.set(req); return Optional.empty(); })
                .build();

        var fixture = run(baseLlm, callbacks, simpleRequest("user prompt"));

        assertThat(fixture.responses()).containsExactly(modelResponse);
        assertThat(sawBefore.get()).isNotNull();
        assertThat(fixture.firedTransitionNames())
                .contains(LlmStepSubnet.Transitions.LLM_CALL);
        assertThat(fixture.firedTransitionNames())
                .contains(LlmStepSubnet.Transitions.AFTER_MODEL);
    }

    // ============================================================
    //  After-model mutation
    // ============================================================

    @Test
    void after_model_can_replace_the_response() {
        var raw = textResponse("raw");
        var replacement = textResponse("replaced");
        var baseLlm = new FakeBaseLlm(req -> Flowable.just(raw));
        var callbacks = LlmStepSubnet.Callbacks.builder()
                .afterModel(r -> replacement)
                .build();

        var fixture = run(baseLlm, callbacks, simpleRequest("q"));

        assertThat(fixture.responses()).containsExactly(replacement);
    }

    @Test
    void after_model_default_forwards_unchanged() {
        var raw = textResponse("raw");
        var baseLlm = new FakeBaseLlm(req -> Flowable.just(raw));

        var fixture = run(baseLlm, LlmStepSubnet.Callbacks.none(), simpleRequest("q"));

        assertThat(fixture.responses()).containsExactly(raw);
    }

    // ============================================================
    //  Error path
    // ============================================================

    @Test
    void llm_error_with_recovery_callback_emits_fallback_response() {
        var baseLlm = new FakeBaseLlm(req -> Flowable.error(new RuntimeException("boom")));
        var fallback = textResponse("sorry, try again");
        var sawError = new AtomicReference<LlmStepSubnet.LlmError>();
        var callbacks = LlmStepSubnet.Callbacks.builder()
                .onModelError(err -> { sawError.set(err); return fallback; })
                .build();

        var fixture = run(baseLlm, callbacks, simpleRequest("q"));

        assertThat(fixture.responses()).containsExactly(fallback);
        assertThat(sawError.get().message()).isEqualTo("boom");
        assertThat(sawError.get().exceptionType()).isEqualTo("java.lang.RuntimeException");
        assertThat(fixture.firedTransitionNames())
                .containsExactly(
                        LlmStepSubnet.Transitions.BEFORE_MODEL,
                        LlmStepSubnet.Transitions.LLM_CALL,
                        LlmStepSubnet.Transitions.ON_MODEL_ERROR)
                .inOrder();
        assertThat(fixture.firedTransitionNames())
                .doesNotContain(LlmStepSubnet.Transitions.AFTER_MODEL);
    }

    @Test
    void llm_error_without_recovery_callback_fails_on_model_error_transition() {
        var baseLlm = new FakeBaseLlm(req -> Flowable.error(new RuntimeException("network down")));

        var fixture = run(baseLlm, LlmStepSubnet.Callbacks.none(), simpleRequest("q"));

        assertThat(fixture.responses()).isEmpty();
        var failed = fixture.events().stream()
                .filter(NetEvent.TransitionFailed.class::isInstance)
                .map(NetEvent.TransitionFailed.class::cast)
                .toList();
        assertThat(failed).hasSize(1);
        assertThat(failed.get(0).transitionName())
                .isEqualTo(LlmStepSubnet.Transitions.ON_MODEL_ERROR);
        assertThat(failed.get(0).errorMessage()).contains("network down");
    }

    // ============================================================
    //  Subnet shape + binding validation
    // ============================================================

    @Test
    void subnet_def_declares_exactly_the_expected_ports() {
        var ifaceNames = LlmStepSubnet.DEF.iface().ports().stream()
                .map(p -> p.name())
                .sorted()
                .toList();
        assertThat(ifaceNames).containsExactly("llmRequest", "llmResponse").inOrder();
    }

    @Test
    void subnet_def_declares_exactly_four_transitions() {
        var names = LlmStepSubnet.DEF.body().transitions().stream()
                .map(t -> t.name())
                .sorted()
                .toList();
        assertThat(names).containsExactly(
                LlmStepSubnet.Transitions.AFTER_MODEL,
                LlmStepSubnet.Transitions.BEFORE_MODEL,
                LlmStepSubnet.Transitions.LLM_CALL,
                LlmStepSubnet.Transitions.ON_MODEL_ERROR)
                .inOrder();
    }

    @Test
    void subnet_actions_bind_rejects_missing_keys() {
        // Pass an incomplete map: missing AFTER_MODEL and ON_MODEL_ERROR.
        var partial = Map.<String, TransitionAction>of(
                LlmStepSubnet.Transitions.BEFORE_MODEL, ctx -> null,
                LlmStepSubnet.Transitions.LLM_CALL,     ctx -> null);
        var ex = assertThrows(IllegalStateException.class,
                () -> SubnetActions.bind(LlmStepSubnet.DEF, partial));
        assertThat(ex.getMessage()).contains("missing keys");
        assertThat(ex.getMessage()).contains(LlmStepSubnet.Transitions.AFTER_MODEL);
        assertThat(ex.getMessage()).contains(LlmStepSubnet.Transitions.ON_MODEL_ERROR);
    }

    @Test
    void subnet_actions_bind_rejects_extra_keys() {
        var extra = new LinkedHashMap<String, TransitionAction>();
        extra.put(LlmStepSubnet.Transitions.BEFORE_MODEL,   ctx -> null);
        extra.put(LlmStepSubnet.Transitions.LLM_CALL,       ctx -> null);
        extra.put(LlmStepSubnet.Transitions.AFTER_MODEL,    ctx -> null);
        extra.put(LlmStepSubnet.Transitions.ON_MODEL_ERROR, ctx -> null);
        extra.put("BogusTransition", ctx -> null);

        var ex = assertThrows(IllegalStateException.class,
                () -> SubnetActions.bind(LlmStepSubnet.DEF, extra));
        assertThat(ex.getMessage()).contains("extra keys");
        assertThat(ex.getMessage()).contains("BogusTransition");
    }

    // ============================================================
    //  Composition + per-token isolation
    // ============================================================

    @Test
    void composed_subnet_routes_inputs_to_correct_subnet_internal_places() {
        // The interface uses USER-facing LLM_REQUEST / LLM_RESPONSE.
        // Composing should not produce duplicate places when AdkColours
        // are referenced by both the subnet body and the parent net.
        var baseLlm = new FakeBaseLlm(req -> Flowable.just(textResponse("ok")));
        var net = PetriNet.builder("test-net")
                .compose(LlmStepSubnet.DEF)
                .build()
                .bindActions(LlmStepSubnet.actionBindings(baseLlm));

        // Sanity: the composed net has the same 4 transitions and includes
        // both AdkColours ports + internal places.
        assertThat(net.transitions().stream().map(t -> t.name()).toList())
                .containsAtLeast(
                        LlmStepSubnet.Transitions.BEFORE_MODEL,
                        LlmStepSubnet.Transitions.LLM_CALL,
                        LlmStepSubnet.Transitions.AFTER_MODEL,
                        LlmStepSubnet.Transitions.ON_MODEL_ERROR);
        assertThat(net.places()).containsAtLeast(
                AdkColours.LLM_REQUEST,
                AdkColours.LLM_RESPONSE,
                LlmStepSubnet.Places.READY_TO_CALL,
                LlmStepSubnet.Places.RAW_RESPONSE,
                LlmStepSubnet.Places.LLM_ERROR);
    }

    // ============================================================
    //  Fixtures and helpers
    // ============================================================

    /** Drive the net with a single request token in the initial marking. */
    private static Fixture run(BaseLlm baseLlm, LlmStepSubnet.Callbacks callbacks, LlmRequest request) {
        return runWithInitialRequests(baseLlm, callbacks, request);
    }

    private static Fixture runWithInitialRequests(
            BaseLlm baseLlm, LlmStepSubnet.Callbacks callbacks, LlmRequest... requests) {
        var net = PetriNet.builder("test-net")
                .compose(LlmStepSubnet.DEF)
                .build()
                .bindActions(LlmStepSubnet.actionBindings(baseLlm, callbacks));

        List<Token<?>> initialRequests = new ArrayList<>();
        for (var r : requests) initialRequests.add(Token.of(r));
        Map<Place<?>, List<Token<?>>> initial = Map.of(AdkColours.LLM_REQUEST, initialRequests);

        var store = EventStore.inMemory();
        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(store)
                .build();
        var finalMarking = executor.run();

        var responses = finalMarking.peekTokens(AdkColours.LLM_RESPONSE).stream()
                .map(Token::value)
                .toList();

        return new Fixture(responses, store.events());
    }

    private static LlmRequest simpleRequest(String userText) {
        return LlmRequest.builder()
                .model("fake-model")
                .contents(List.of(Content.builder()
                        .role("user")
                        .parts(List.of(Part.fromText(userText)))
                        .build()))
                .build();
    }

    private static LlmResponse textResponse(String text) {
        return LlmResponse.builder()
                .content(Content.builder()
                        .role("model")
                        .parts(List.of(Part.fromText(text)))
                        .build())
                .build();
    }

    private record Fixture(List<LlmResponse> responses, List<NetEvent> events) {
        List<String> firedTransitionNames() {
            return events.stream()
                    .filter(NetEvent.TransitionStarted.class::isInstance)
                    .map(e -> ((NetEvent.TransitionStarted) e).transitionName())
                    .toList();
        }
    }

    /**
     * Minimal {@link BaseLlm} that delegates {@code generateContent} to a
     * supplier. {@code connect} is unsupported.
     */
    private static final class FakeBaseLlm extends BaseLlm {
        private final Function<LlmRequest, Flowable<LlmResponse>> impl;
        FakeBaseLlm(Function<LlmRequest, Flowable<LlmResponse>> impl) {
            super("fake");
            this.impl = impl;
        }
        @Override public Flowable<LlmResponse> generateContent(LlmRequest req, boolean stream) {
            return impl.apply(req);
        }
        @Override public BaseLlmConnection connect(LlmRequest req) {
            throw new UnsupportedOperationException("FakeBaseLlm.connect()");
        }
    }
}
