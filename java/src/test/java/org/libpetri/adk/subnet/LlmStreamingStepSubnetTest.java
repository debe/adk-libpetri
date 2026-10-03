package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;
import static com.google.common.truth.Truth.assertWithMessage;

import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.subscribers.TestSubscriber;
import java.util.ArrayDeque;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.UnaryOperator;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.analysis.EnvironmentAnalysisMode;
import org.libpetri.core.Arc;
import org.libpetri.core.EnvironmentPlace;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.Transition;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;
import org.libpetri.adk.bridge.EventStoreToFlowableBridge;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.verify.SmtProofs;
import org.libpetri.runtime.BitmapNetExecutor;
import org.libpetri.runtime.Marking;
import org.libpetri.runtime.PetriNetExecutor;
import org.libpetri.smt.SmtProperty;
import org.libpetri.smt.SmtVerifier;

class LlmStreamingStepSubnetTest {

    static boolean z3Available() {
        return SmtVerifier.z3Available();
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
                LlmStreamingStepSubnet.Config.builder("streamer"),
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
    void large_batch_of_chunks_each_emitted_in_order() throws Exception {
        var chunks = new ArrayList<LlmResponse>();
        for (int i = 0; i < 20; i++) chunks.add(chunkResponse("chunk-" + i));

        var fixture = runStreaming(streamingLlm(chunks),
                LlmStreamingStepSubnet.Config.builder("burst"),
                simpleRequest("burst me"));

        assertThat(fixture.events).hasSize(20);
        assertThat(fixture.events.stream().map(e -> e.content().get().text()).toList())
                .containsExactlyElementsIn(chunks.stream()
                        .map(c -> c.content().get().text()).toList())
                .inOrder();
        assertThat(fixture.mergedResponses).hasSize(1);
    }

    @Test
    void each_request_on_one_executor_streams_its_own_chunks() throws Exception {
        // Two requests through the SAME long-lived executor. The executor
        // never starts LlmCallStream again while a stream is in flight, so
        // the second call begins only after the first has injected its
        // terminal chunk: four partials, in request order, and one merged
        // response per request.
        var llm = scriptedStreamingLlm(
                List.of(chunkResponse("a"), chunkResponse("b")),
                List.of(chunkResponse("c"), chunkResponse("d")));

        var fixture = runStreamingMultiRequest(llm,
                LlmStreamingStepSubnet.Config.builder("multi"),
                List.of(simpleRequest("one"), simpleRequest("two")));

        assertThat(fixture.events.stream().map(e -> e.content().get().text()).toList())
                .containsExactly("a", "b", "c", "d").inOrder();
        assertThat(fixture.mergedResponses).hasSize(2);
    }

    // ============================================================
    //  Structural verification
    // ============================================================

    /**
     * Every request is taken and every chunk the stream injects drains to
     * {@code EVENT_OUT} or {@code LLM_RESPONSE}; nothing strands on
     * {@code LLM_REQUEST} or {@code CHUNK}.
     *
     * <p>{@code CHUNK} is an internal environment place, not a port, and a
     * stream may carry any number of chunks, so {@code bounded(1)} (one
     * resident chunk, refilled forever) is the model rather than a finite
     * {@code arrivals(k)}. Left on {@code ignore()}, nothing would ever reach
     * {@code CHUNK} and the proof would say nothing about emission.
     *
     * <p>The second half keeps the proof honest: the same check on a variant
     * whose emit also needs a token nobody produces, an exhausted permit
     * pool, must come back Violated. The chunk-budget proof this replaces
     * seeded no request and held even at bound 0.
     */
    @Test
    @EnabledIf("z3Available")
    void streaming_step_never_strands_a_request_or_a_chunk() {
        // CORE-043 (libpetri 2.14+): a transition declaring an output spec
        // must carry a producing action at verification as well as at
        // execution. Bind the subnet's real actions so the property is proven
        // about the net that runs, not an unbound skeleton. The actions are
        // never invoked here; only the structure is encoded.
        var verifyConfig = LlmStreamingStepSubnet.Config.builder("verify")
                .executorRef(new AtomicReference<PetriNetExecutor>())
                .build();
        var net = PetriNet.builder("streaming")
                .compose(LlmStreamingStepSubnet.DEF)
                .build()
                .bindActions(LlmStreamingStepSubnet.actionBindings(
                        streamingLlm(List.of()), verifyConfig));
        UnaryOperator<SmtVerifier> twoRequestsOpenStream = v -> v
                .initialMarking(b -> b.tokens(AdkColours.LLM_REQUEST, 2))
                .environmentPlaces(EnvironmentPlace.of(LlmStreamingStepSubnet.Places.CHUNK))
                .environmentMode(EnvironmentAnalysisMode.bounded(1))
                .sinkPlaces(AdkColours.EVENT_OUT, AdkColours.LLM_RESPONSE);

        SmtProofs.assertEachProven(net, twoRequestsOpenStream,
                Map.of("deadlockFree", SmtProperty.deadlockFree()));

        var starved = twoRequestsOpenStream
                .apply(SmtVerifier.forNet(withEmitGatedOnUnseededPlace(net)))
                .property(SmtProperty.deadlockFree())
                .verify();
        assertWithMessage("a starved emit must strand a chunk:\n%s", starved.report())
                .that(starved.isViolated()).isTrue();
    }

    // ============================================================
    //  Edge cases
    // ============================================================

    @Test
    void empty_stream_fails_the_llm_call_transition() throws Exception {
        var fixture = runStreaming(streamingLlm(List.of()),
                LlmStreamingStepSubnet.Config.builder("e"),
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
    void def_declares_call_and_emit_transitions() {
        var names = LlmStreamingStepSubnet.DEF.body().transitions().stream()
                .map(t -> t.name()).sorted().toList();
        assertThat(names).containsExactly(
                LlmStreamingStepSubnet.Transitions.EMIT_CHUNK,
                LlmStreamingStepSubnet.Transitions.LLM_CALL_STREAM).inOrder();
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

        // Drain only once the net has really finished. drain() refuses new
        // injects, so draining while a stream is still injecting chunks would
        // lose them; this used to be a fixed 200ms sleep that bet otherwise.
        awaitSettled(executor, Duration.ofSeconds(5));

        executor.drain();
        Marking finalMarking = task.get(5, TimeUnit.SECONDS);
        orchestratorExec.shutdown();

        return new Fixture(
                List.copyOf(sub.values()),
                finalMarking.peekTokens(AdkColours.LLM_RESPONSE).stream().map(Token::value).toList(),
                finalMarking,
                captured.events());
    }

    /**
     * Waits until no action is in flight, no inject is pending and no request
     * or chunk is still waiting: the stream has fully played out. libpetri
     * reports the first two through {@code snapshot().isRestorePoint()}.
     */
    @SuppressWarnings("BusyWait")
    private static void awaitSettled(BitmapNetExecutor executor, Duration timeout)
            throws InterruptedException {
        long deadline = System.nanoTime() + timeout.toNanos();
        while (System.nanoTime() < deadline) {
            var snap = executor.snapshot();
            if (snap.isRestorePoint()
                    && !snap.marking().containsKey(AdkColours.LLM_REQUEST.name())
                    && !snap.marking().containsKey(LlmStreamingStepSubnet.Places.CHUNK.name())) {
                return;
            }
            Thread.sleep(2);
        }
        throw new AssertionError("streaming net did not settle within " + timeout);
    }

    /**
     * {@code net} with {@code T_EmitChunk} also consuming from a place that
     * nothing produces into: the shape of an emit gated on a permit pool
     * that has run dry.
     */
    private static PetriNet withEmitGatedOnUnseededPlace(PetriNet net) {
        var gate = Place.of("unseededGate", Void.class);
        var mutant = PetriNet.builder(net.name() + "-starved").place(gate);
        net.places().forEach(mutant::place);
        for (var t : net.transitions()) {
            if (!t.name().equals(LlmStreamingStepSubnet.Transitions.EMIT_CHUNK)) {
                mutant.transition(t);
                continue;
            }
            var inputs = new ArrayList<>(t.inputSpecs());
            inputs.add(Arc.In.one(gate));
            mutant.transition(Transition.builder(t.name())
                    .inputs(inputs.toArray(Arc.In[]::new))
                    .outputs(t.outputSpec())
                    .priority(t.priority())
                    .action(t.action())
                    .build());
        }
        return mutant.build();
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
