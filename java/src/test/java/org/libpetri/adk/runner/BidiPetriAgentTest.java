package org.libpetri.adk.runner;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.LiveRequestQueue;
import com.google.adk.events.Event;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Blob;
import com.google.genai.types.Content;
import com.google.genai.types.LiveServerContent;
import com.google.genai.types.LiveServerMessage;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Completable;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.processors.PublishProcessor;
import io.reactivex.rxjava3.subscribers.TestSubscriber;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;

/**
 * Proves the generic BIDI pump end to end with no live WebSocket: a fake
 * {@link LiveConnection} and the real ADK {@link LiveRequestQueue} drive a real
 * running {@link PetriRunner}. Covers the responsibilities the library owns:
 * inbound frames reach the connection, consumer-decoded model content and turn
 * edges reach the net via {@link PetriRunner#inject}/{@link PetriRunner#signal},
 * and both pumps are disposed when the outbound stream is cancelled.
 *
 * <p><b>The net authors every event.</b> The bridge maps no frames; it returns
 * {@link PetriRunner#adkEvents()} alone. So {@code partial} and {@code turnComplete}
 * are set by whichever transition fired, which is the point: turn shape is a
 * marking-level decision, not a bridge-side guess. The fixture net is two
 * transitions, one per edge, plus the arc that orders them:
 *
 * <pre>
 *   [MODEL_CHUNK]   --T_EmitPartial--&gt; [EVENT_OUT]   partial=true,  content
 *   [TURN_COMPLETE] --T_EmitFinal----&gt; [EVENT_OUT]   partial=false, turnComplete=true
 *                       o---[MODEL_CHUNK]            inhibitor: no terminal
 *                                                    while chunks are queued
 * </pre>
 *
 * <p><b>Why the inhibitor is load-bearing.</b> A burst of frames drains into the
 * marking in one external-event pass, and each enabled transition then fires at
 * most once per pass. Without the inhibitor, {@code T_EmitFinal} is enabled
 * alongside the still-queued chunks and emits between them: three chunks plus a
 * turn-complete come out as {@code [a, b, TURN_COMPLETE, c]}. The inhibitor makes
 * the ordering structural, so the callback can inject fire-and-forget off the
 * transport's reader thread with nothing to block on.
 *
 * <p>Provider-specific {@code LiveServerMessage} decode stays out of this runner
 * test and is covered by each connection implementation's own tests (for example,
 * {@code SyncGeminiLiveConnectionTest}).
 */
class BidiPetriAgentTest {

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setup() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void teardown() {
        EXECUTOR.shutdown();
    }

    /** Model content the consumer callback injects, one token per streamed turn chunk. */
    private static final Place<Content> MODEL_CHUNK =
            Place.of("bridgeTest_modelChunk", Content.class);

    /** Turn boundary the consumer callback signals; drives the terminal event. */
    private static final Place<Void> TURN_COMPLETE =
            Place.of("bridgeTest_turnComplete", Void.class);

    @Test
    void model_chunk_injected_by_the_callback_is_authored_by_the_net_as_a_partial() throws Exception {
        try (var f = newFixture()) {
            f.conn.raw.onNext(modelContent("the answer is 42"));

            f.sub.awaitCount(1);
            assertThat(f.sub.values()).hasSize(1);
            Event e = f.sub.values().get(0);
            assertThat(e.author()).isEqualTo("net");                 // net-authored, not bridge-authored
            assertThat(e.content().map(Content::text)).hasValue("the answer is 42");
            assertThat(e.partial()).hasValue(true);
            assertThat(e.turnComplete().orElse(false)).isFalse();
        }
    }

    @Test
    void turn_complete_signal_is_authored_by_the_net_as_a_terminal_event() throws Exception {
        try (var f = newFixture()) {
            // A frame with no model content at all: the only thing that reaches the net
            // is the decoded turn edge. Proves a pure signal still drives egress.
            f.conn.raw.onNext(turnCompleteFrame());

            f.sub.awaitCount(1);
            assertThat(f.sub.values()).hasSize(1);
            Event e = f.sub.values().get(0);
            assertThat(e.author()).isEqualTo("net");
            assertThat(e.partial().orElse(false)).isFalse();
            assertThat(e.turnComplete()).hasValue(true);
        }
    }

    /**
     * Regression for live-egress reordering. A burst is the case that breaks: the whole
     * burst is admitted to the marking in one pass, after which each enabled transition
     * fires once. Without {@code T_EmitFinal}'s inhibitor this net emits
     * {@code [a, b, TURN_COMPLETE, c]} and the terminal overtakes its own partials.
     */
    @Test
    void a_burst_streamed_turn_yields_every_partial_before_the_terminal_event() throws Exception {
        try (var f = newFixture()) {
            // Pushed back to back with nothing awaited in between: the frames land in the
            // net faster than the orchestrator drains them, which is the real transport's
            // behaviour and the shape that exposed the bug.
            f.conn.raw.onNext(modelContent("Sure, "));
            f.conn.raw.onNext(modelContent("the answer "));
            f.conn.raw.onNext(modelContent("is 42."));
            f.conn.raw.onNext(turnCompleteFrame());

            f.sub.awaitCount(4);
            var events = f.sub.values();
            assertThat(events).hasSize(4);

            assertThat(events.get(0).partial()).hasValue(true);
            assertThat(events.get(0).content().map(Content::text)).hasValue("Sure, ");
            assertThat(events.get(1).partial()).hasValue(true);
            assertThat(events.get(1).content().map(Content::text)).hasValue("the answer ");
            assertThat(events.get(2).partial()).hasValue(true);
            assertThat(events.get(2).content().map(Content::text)).hasValue("is 42.");
            assertThat(events.get(3).turnComplete()).hasValue(true);
            assertThat(events.get(3).partial().orElse(false)).isFalse();
        }
    }

    @Test
    void inbound_queue_frames_are_forwarded_to_the_connection() throws Exception {
        try (var f = newFixture()) {
            Blob audio = Blob.builder().data(new byte[] {1, 2, 3}).mimeType("audio/pcm").build();
            Content text = Content.builder().role("user").parts(List.of(Part.fromText("hi"))).build();

            f.queue.realtime(audio);
            f.queue.content(text);

            await(() -> !f.conn.realtimeSends.isEmpty() && !f.conn.contentSends.isEmpty(), 2000);
            assertThat(f.conn.realtimeSends).containsExactly(audio);
            assertThat(f.conn.contentSends).containsExactly(text);
        }
    }

    @Test
    void a_transport_error_terminates_the_returned_stream() throws Exception {
        try (var f = newFixture()) {
            // Without this, a dead connection would leave the consumer subscribed to a
            // net egress that no longer has anything feeding it: a silent hang.
            var boom = new IllegalStateException("websocket closed");
            f.conn.raw.onError(boom);

            f.sub.await(2, java.util.concurrent.TimeUnit.SECONDS);
            f.sub.assertError(boom);
        }
    }

    @Test
    void cancelling_the_outbound_stream_disposes_both_pumps() throws Exception {
        try (var f = newFixture()) {
            Blob beforeCancel = Blob.builder().data(new byte[] {1}).mimeType("audio/pcm").build();
            f.queue.realtime(beforeCancel);
            await(() -> f.conn.realtimeSends.size() == 1, 2000);
            f.conn.realtimeSends.clear();

            f.sub.cancel();

            // Output pump gone: the raw server stream has no subscriber left.
            await(() -> !f.conn.raw.hasSubscribers(), 2000);
            // Input pump gone: further queue frames are not forwarded.
            f.queue.realtime(Blob.builder().data(new byte[] {9}).mimeType("audio/pcm").build());
            assertThat(f.conn.realtimeSends).isEmpty();
        }
    }

    // ============================================================
    //  Fixture
    // ============================================================

    private Fixture newFixture() {
        TransitionAction emitPartial = ctx -> {
            Content chunk = ctx.input(MODEL_CHUNK);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .id(Event.generateEventId())
                    .invocationId("inv")
                    .author("net")
                    .content(chunk)
                    .partial(true)
                    .build());
            return CompletableFuture.completedFuture(null);
        };
        TransitionAction emitFinal = ctx -> {
            ctx.input(TURN_COMPLETE);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .id(Event.generateEventId())
                    .invocationId("inv")
                    .author("net")
                    .partial(false)
                    .turnComplete(true)
                    .build());
            return CompletableFuture.completedFuture(null);
        };

        Transition partialEmit = Transition.builder("T_EmitPartial")
                .inputs(Arc.In.one(MODEL_CHUNK))
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();
        Transition finalEmit = Transition.builder("T_EmitFinal")
                .inputs(Arc.In.one(TURN_COMPLETE))
                // The terminal cannot fire while a chunk is still queued. This is what
                // orders egress; see the class javadoc for the burst that breaks without it.
                .inhibitor(MODEL_CHUNK)
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();

        PetriNet net = PetriNet.builder("bridge-test")
                .place(MODEL_CHUNK)
                .place(TURN_COMPLETE)
                .place(AdkColours.EVENT_OUT)
                .transition(partialEmit)
                .transition(finalEmit)
                .build()
                .bindActions(Map.of(
                        "T_EmitPartial", emitPartial,
                        "T_EmitFinal", emitFinal));

        PetriRunner runner = PetriRunner.builder(net)
                .environmentPlace(MODEL_CHUNK)
                .environmentPlace(TURN_COMPLETE)
                .actionExecutor(EXECUTOR)
                .orchestratorExecutor(EXECUTOR)
                .start();

        FakeLiveConnection conn = new FakeLiveConnection();
        LiveRequestQueue queue = new LiveRequestQueue();

        // The consumer half: decode the frame, inject into the net. No event authoring
        // here, and no acceptance future awaited either. Injection is asynchronous, but
        // the net's own structure (T_EmitFinal's inhibitor) is what orders egress, so
        // the callback stays a plain fire-and-forget decode on the transport thread.
        Flowable<Event> out = BidiPetriAgent.bridge(queue, conn, runner,
                (msg, r) -> {
                    msg.serverContent()
                            .flatMap(LiveServerContent::modelTurn)
                            .ifPresent(c -> r.inject(MODEL_CHUNK, c));
                    if (msg.serverContent().flatMap(LiveServerContent::turnComplete).orElse(false)) {
                        r.signal(TURN_COMPLETE);
                    }
                });
        TestSubscriber<Event> sub = out.test();
        return new Fixture(runner, conn, queue, sub);
    }

    private record Fixture(PetriRunner runner, FakeLiveConnection conn,
                           LiveRequestQueue queue, TestSubscriber<Event> sub)
            implements AutoCloseable {
        @Override public void close() {
            sub.cancel();
            runner.shutdown();
        }
    }

    /** Records sends and lets the test push server frames through {@code raw}. */
    private static final class FakeLiveConnection implements LiveConnection {
        final PublishProcessor<LiveServerMessage> raw = PublishProcessor.create();
        final List<Blob> realtimeSends = new CopyOnWriteArrayList<>();
        final List<Content> contentSends = new CopyOnWriteArrayList<>();
        final AtomicBoolean closed = new AtomicBoolean();

        @Override public Flowable<LiveServerMessage> rawReceive() { return raw; }
        @Override public Completable sendRealtime(Blob blob) { realtimeSends.add(blob); return Completable.complete(); }
        @Override public Completable sendContent(Content content) { contentSends.add(content); return Completable.complete(); }
        @Override public Completable sendHistory(List<Content> history) { return Completable.complete(); }
        @Override public Flowable<LlmResponse> receive() { return Flowable.empty(); }
        @Override public void close() { closed.set(true); }
        @Override public void close(Throwable t) { closed.set(true); }
    }

    private static LiveServerMessage turnCompleteFrame() {
        return LiveServerMessage.builder()
                .serverContent(LiveServerContent.builder().turnComplete(true).build())
                .build();
    }

    private static LiveServerMessage modelContent(String text) {
        return LiveServerMessage.builder()
                .serverContent(LiveServerContent.builder()
                        .modelTurn(Content.builder().role("model")
                                .parts(List.of(Part.fromText(text))).build())
                        .build())
                .build();
    }

    private static void await(BooleanSupplier cond, long timeoutMillis) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (System.currentTimeMillis() < deadline) {
            if (cond.getAsBoolean()) return;
            Thread.sleep(20);
        }
        throw new AssertionError("Condition was not met within " + timeoutMillis + "ms");
    }
}
