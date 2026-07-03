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
 * running {@link PetriRunner}. Covers the four responsibilities the library now owns:
 * inbound frames reach the connection, a consumer-decoded signal reaches the net via
 * {@link PetriRunner#signal}, model content surfaces as a merged {@link Event}, and
 * the input pump is disposed when the outbound stream is cancelled.
 *
 * <p>The net here is intentionally minimal (one {@code Place<Void>} signal place to one
 * EVENT_OUT transition) so egress is deterministic. Provider-specific
 * {@code LiveServerMessage} decode stays out of this runner test and is covered by
 * each connection implementation's own tests (for example,
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

    private static final Place<Void> SIG = Place.of("bridgeTest_signal", Void.class);

    @Test
    void decoded_signal_reaches_the_net_and_its_event_is_merged_onto_egress() throws Exception {
        try (var f = newFixture()) {
            // The bridge is provider-neutral: the consumer callback owns raw-frame
            // decode and injects the typed signal place. This marker frame has no
            // model content, so the only egress event should be net-produced.
            f.conn.raw.onNext(signalFrame());

            f.sub.awaitCount(1);
            assertThat(f.sub.values()).hasSize(1);
            assertThat(f.sub.values().get(0).author()).isEqualTo("net"); // net-produced, no content
        }
    }

    @Test
    void model_content_frame_surfaces_as_a_merged_event() throws Exception {
        try (var f = newFixture()) {
            f.conn.raw.onNext(modelContent("the answer is 42"));

            f.sub.awaitCount(1);
            assertThat(f.sub.values()).hasSize(1);
            Event e = f.sub.values().get(0);
            assertThat(e.author()).isEqualTo("agent");             // connection-derived author
            assertThat(e.content().map(Content::text)).hasValue("the answer is 42");
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
    void cancelling_the_outbound_stream_disposes_the_input_pump() throws Exception {
        try (var f = newFixture()) {
            Blob beforeCancel = Blob.builder().data(new byte[] {1}).mimeType("audio/pcm").build();
            f.queue.realtime(beforeCancel);
            await(() -> f.conn.realtimeSends.size() == 1, 2000);
            f.conn.realtimeSends.clear();

            f.sub.cancel();
            await(() -> !f.conn.raw.hasSubscribers(), 2000);

            f.queue.realtime(Blob.builder().data(new byte[] {9}).mimeType("audio/pcm").build());
            assertThat(f.conn.realtimeSends).isEmpty();
        }
    }

    // ============================================================
    //  Fixture
    // ============================================================

    private Fixture newFixture() {
        TransitionAction ack = ctx -> {
            ctx.input(SIG);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .id(Event.generateEventId()).invocationId("inv").author("net").build());
            return CompletableFuture.completedFuture(null);
        };
        Transition emit = Transition.builder("T_Ack")
                .inputs(Arc.In.one(SIG))
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();
        PetriNet net = PetriNet.builder("bridge-test")
                .place(SIG)
                .place(AdkColours.EVENT_OUT)
                .transition(emit)
                .build()
                .bindActions(Map.of("T_Ack", ack));

        PetriRunner runner = PetriRunner.builder(net)
                .environmentPlace(SIG)
                .actionExecutor(EXECUTOR)
                .orchestratorExecutor(EXECUTOR)
                .start();

        FakeLiveConnection conn = new FakeLiveConnection();
        LiveRequestQueue queue = new LiveRequestQueue();

        Flowable<Event> out = BidiPetriAgent.bridge(queue, conn, runner, "agent",
                (msg, r) -> {
                    if (msg.serverContent().isEmpty()) {
                        r.signal(SIG);
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

    private static LiveServerMessage signalFrame() {
        return LiveServerMessage.builder().build();
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
