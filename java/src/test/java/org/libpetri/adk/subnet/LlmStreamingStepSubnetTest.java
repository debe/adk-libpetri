package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import com.microsoft.z3.Context;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.subscribers.TestSubscriber;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.analysis.EnvironmentAnalysisMode;
import org.libpetri.core.EnvironmentPlace;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;
import org.libpetri.adk.bridge.EventStoreToFlowableBridge;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.verify.AdkNetInvariants;
import org.libpetri.runtime.BitmapNetExecutor;
import org.libpetri.runtime.Marking;
import org.libpetri.runtime.PetriNetExecutor;
import org.libpetri.smt.SmtVerifier;

class LlmStreamingStepSubnetTest {

    static boolean z3Available() {
        try {
            new Context().close();
            return true;
        } catch (UnsatisfiedLinkError | NoClassDefFoundError _) {
            return false;
        }
    }

    // ============================================================
    //  True incremental streaming — partials arrive while the
    //  LLM call is still in flight, via env-place injection.
    // ============================================================

    @Test
    void three_chunks_each_injected_via_env_place_emit_three_partial_events() throws Exception {
        var chunks = List.of(
                chunkResponse("Hello"),
                chunkResponse(", "),
                chunkResponse("world!"));
        var fixture = runStreaming(streamingLlm(chunks),
                LlmStreamingStepSubnet.Config.builder("streamer").chunkBudget(4),
                simpleRequest("hi"));

        // Three partial Events were emitted to EVENT_OUT via T_EmitChunk.
        // Each was produced from a separately-injected chunk env-place token.
        assertThat(fixture.events).hasSize(3);
        assertThat(fixture.events.get(0).content().get().text()).isEqualTo("Hello");
        assertThat(fixture.events.get(1).content().get().text()).isEqualTo(", ");
        assertThat(fixture.events.get(2).content().get().text()).isEqualTo("world!");
        assertThat(fixture.events.stream().allMatch(e -> e.partial().orElse(false))).isTrue();
        assertThat(fixture.events.stream().map(Event::author).distinct().toList())
                .containsExactly("streamer");

        // One merged LlmResponse for downstream Router consumption.
        assertThat(fixture.mergedResponses).hasSize(1);
        var merged = fixture.mergedResponses.get(0);
        assertThat(merged.turnComplete()).hasValue(Boolean.TRUE);
        assertThat(merged.content().get().parts().get()).hasSize(3);
    }

    @Test
    void large_batch_of_chunks_each_emitted_through_budget() throws Exception {
        // 20 chunks, budget = 4. Even at the worst case, CHUNK_BUDGET stays
        // pinned at 4 across the run because emit consumes-and-returns one
        // permit per fire. The test asserts that all 20 chunks emitted, then
        // CHUNK_BUDGET is back at exactly 4 at quiescence.
        var chunks = new ArrayList<LlmResponse>();
        for (int i = 0; i < 20; i++) chunks.add(chunkResponse("chunk-" + i));

        var fixture = runStreaming(streamingLlm(chunks),
                LlmStreamingStepSubnet.Config.builder("burst").chunkBudget(4),
                simpleRequest("burst me"));

        assertThat(fixture.events).hasSize(20);
        // CHUNK_BUDGET invariant: at quiescence equals the configured K.
        long budgetTokens = fixture.finalMarking
                .peekTokens(LlmStreamingStepSubnet.Places.CHUNK_BUDGET).size();
        assertThat(budgetTokens).isEqualTo(4L);
    }

    @Test
    void budget_resets_on_each_new_request() throws Exception {
        // Two requests through the SAME long-lived executor — each should
        // reset CHUNK_BUDGET (via the Reset arc on T_SeedAndStart) and seed
        // K fresh permits. The end-of-run CHUNK_BUDGET must still equal K
        // (not 2*K, not K-leftover).
        var llm = scriptedStreamingLlm(
                List.of(chunkResponse("a"), chunkResponse("b")),
                List.of(chunkResponse("c"), chunkResponse("d")));

        var fixture = runStreamingMultiRequest(llm,
                LlmStreamingStepSubnet.Config.builder("multi").chunkBudget(3),
                List.of(simpleRequest("one"), simpleRequest("two")));

        assertThat(fixture.events).hasSize(4);
        long budgetTokens = fixture.finalMarking
                .peekTokens(LlmStreamingStepSubnet.Places.CHUNK_BUDGET).size();
        assertThat(budgetTokens).isEqualTo(3L);
    }

    // ============================================================
    //  Structural verification — the budget bound is SMT-provable
    // ============================================================

    @Test
    @EnabledIf("z3Available")
    void chunk_budget_is_smt_provably_bounded() {
        // Build a synthetic-but-shaped net: SeedAndStart + EmitChunk pattern.
        // Z3 Spacer should prove CHUNK_BUDGET <= K under the consume-and-return
        // pattern of T_EmitChunk.
        var k = 4;
        // CORE-043 (libpetri 2.14+): a transition declaring an output spec
        // must carry a producing action at verification as well as at
        // execution. Bind the subnet's real actions so the bound is proven
        // about the net that runs, not an unbound skeleton. The actions are
        // never invoked here; only the structure is encoded.
        var verifyConfig = LlmStreamingStepSubnet.Config.builder("verify")
                .chunkBudget(k)
                .executorRef(new AtomicReference<PetriNetExecutor>())
                .build();
        var net = PetriNet.builder("streaming")
                .compose(LlmStreamingStepSubnet.DEF)
                .build()
                .bindActions(LlmStreamingStepSubnet.actionBindings(
                        streamingLlm(List.of()), verifyConfig));

        var result = SmtVerifier.forNet(net)
                .environmentPlaces(EnvironmentPlace.of(LlmStreamingStepSubnet.Places.CHUNK))
                .environmentMode(EnvironmentAnalysisMode.bounded(1))
                .property(AdkNetInvariants.reaskBudgetIsBounded(
                        LlmStreamingStepSubnet.Places.CHUNK_BUDGET, k))
                .verify();

        // With CHUNK modelled as a bounded environment place the bound is
        // genuinely proven rather than vacuously unrefuted. Left on the
        // default ignore() mode the verifier returns Unknown ("a proof would
        // be vacuous"), which isViolated()==false would have accepted.
        assertThat(result.isProven()).isTrue();
        assertThat(result.isViolated()).isFalse();
    }

    // ============================================================
    //  Edge cases
    // ============================================================

    @Test
    void empty_stream_fails_the_llm_call_transition() throws Exception {
        var fixture = runStreaming(streamingLlm(List.of()),
                LlmStreamingStepSubnet.Config.builder("e").chunkBudget(2),
                simpleRequest("hi"));
        assertThat(fixture.events).isEmpty();
        var failed = fixture.netEvents.stream()
                .filter(NetEvent.TransitionFailed.class::isInstance)
                .toList();
        assertThat(failed).hasSize(1);
    }

    @Test
    void interface_exposes_one_input_and_two_outputs() {
        var ports = LlmStreamingStepSubnet.DEF.iface().ports().stream()
                .map(p -> p.name()).sorted().toList();
        assertThat(ports).containsExactly("eventOut", "llmRequest", "llmResponse").inOrder();
    }

    @Test
    void def_declares_seed_call_and_emit_transitions() {
        var names = LlmStreamingStepSubnet.DEF.body().transitions().stream()
                .map(t -> t.name()).sorted().toList();
        assertThat(names).containsExactly(
                LlmStreamingStepSubnet.Transitions.EMIT_CHUNK,
                LlmStreamingStepSubnet.Transitions.LLM_CALL_STREAM,
                LlmStreamingStepSubnet.Transitions.SEED_AND_START).inOrder();
    }

    @Test
    void config_rejects_zero_chunk_budget() {
        var ex = Assertions.assertThrows(
                IllegalArgumentException.class,
                () -> LlmStreamingStepSubnet.Config.builder("a")
                        .chunkBudget(0)
                        .executorRef(new AtomicReference<>())
                        .build());
        assertThat(ex.getMessage()).contains("chunkBudget must be >= 1");
    }

    // ============================================================
    //  Fixtures — drive a long-running executor with env-place injection
    // ============================================================

    private record Fixture(
            List<Event> events,
            List<LlmResponse> mergedResponses,
            Marking finalMarking,
            List<NetEvent> netEvents) {}

    private static Fixture runStreaming(BaseLlm llm,
                                         LlmStreamingStepSubnet.Config.Builder configBuilder,
                                         LlmRequest request) throws Exception {
        return runStreamingMultiRequest(llm, configBuilder, List.of(request));
    }

    private static Fixture runStreamingMultiRequest(
            BaseLlm llm,
            LlmStreamingStepSubnet.Config.Builder configBuilder,
            List<LlmRequest> requests) throws Exception {

        var execRef = new AtomicReference<PetriNetExecutor>();
        var config = configBuilder.executorRef(execRef).build();
        var chunkEnv = EnvironmentPlace.of(LlmStreamingStepSubnet.Places.CHUNK);

        var net = PetriNet.builder("streaming-test")
                .compose(LlmStreamingStepSubnet.DEF)
                .build()
                .bindActions(LlmStreamingStepSubnet.actionBindings(llm, config));

        // Initial marking holds all the requests we want to drive through.
        List<Token<?>> requestTokens = new ArrayList<>();
        for (var r : requests) requestTokens.add(Token.of(r));
        Map<Place<?>, List<Token<?>>> initial = Map.of(AdkColours.LLM_REQUEST, requestTokens);

        var captured = EventStore.inMemory();
        var bridge = new EventStoreToFlowableBridge(AdkColours.EVENT_OUT, captured);

        var executor = BitmapNetExecutor.builder(net, initial)
                .environmentPlaces(chunkEnv)
                .eventStore(bridge)
                .build();
        execRef.set(executor);

        // Subscribe BEFORE start — the partials arrive on the hot stream.
        // We expect total events = sum over requests of chunk count (collected later).
        TestSubscriber<Event> sub = bridge.asFlowable().test();

        var orchestratorExec = Executors.newVirtualThreadPerTaskExecutor();
        CompletableFuture<Marking> task = CompletableFuture.supplyAsync(
                executor::run, orchestratorExec);

        // Heuristic wait: give the executor up to 2s to process all requests,
        // then drain. With local in-memory mock LLM + virtual threads, this is
        // far more than enough.
        Thread.sleep(200);

        executor.drain();
        Marking finalMarking = task.get(5, TimeUnit.SECONDS);
        orchestratorExec.shutdown();

        return new Fixture(
                List.copyOf(sub.values()),
                finalMarking.peekTokens(AdkColours.LLM_RESPONSE).stream().map(Token::value).toList(),
                finalMarking,
                captured.events());
    }

    private static LlmRequest simpleRequest(String text) {
        return LlmRequest.builder()
                .model("fake")
                .contents(List.of(Content.builder().role("user")
                        .parts(List.of(Part.fromText(text))).build()))
                .build();
    }

    private static LlmResponse chunkResponse(String text) {
        return LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText(text))).build())
                .build();
    }

    private static BaseLlm streamingLlm(List<LlmResponse> chunks) {
        return new BaseLlm("streaming") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.fromIterable(chunks);
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    /** Each call returns a fresh chunk list from the queue. */
    @SafeVarargs
    private static BaseLlm scriptedStreamingLlm(List<LlmResponse>... callsInOrder) {
        var queue = new ArrayDeque<List<LlmResponse>>(List.of(callsInOrder));
        return new BaseLlm("scripted-streaming") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                var next = queue.poll();
                if (next == null) return Flowable.error(new IllegalStateException("scripted exhausted"));
                return Flowable.fromIterable(next);
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }
}
