package org.libpetri.adk.runner;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.models.LlmResponse;
import com.google.adk.runner.InMemoryRunner;
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
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;
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
 * End-to-end coverage for {@link PetriAgent#ofLive}: ADK's stock
 * {@link InMemoryRunner#runLive} must reach the shipped {@link BidiPetriAgent}
 * bridge, not the legacy egress-only live path.
 *
 * <p>Both outbound events here are authored by the net, because that is all the
 * bridge returns. The {@code LiveConfig} callback is what turns a server frame
 * into net input: a content-less frame becomes a {@code CALLBACK_SIGNAL}, a model
 * turn becomes a {@code MODEL_CHUNK} token. Two distinct authors keep the two
 * paths apart in the assertions.
 */
class PetriAgentLiveTest {

    private static final String AGENT_NAME = "live_agent";
    private static final Place<Void> CALLBACK_SIGNAL = Place.of("petriAgentLive_callbackSignal", Void.class);
    private static final Place<Content> MODEL_CHUNK = Place.of("petriAgentLive_modelChunk", Content.class);

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    @Test
    void ofLive_bridges_live_queue_model_frames_and_callback_signals() throws InterruptedException {
        var connection = new FakeLiveConnection();
        var callbackFrames = new AtomicInteger();
        var registry = SessionExecutorRegistry.strongOwned();
        var sessionOwners = sessionOwnerMap();
        TestSubscriber<Event> events = null;

        try {
            var agent = PetriAgent.ofLive(
                    AGENT_NAME,
                    "BIDI live bridge test",
                    registry,
                    key -> callbackSignalRunner(),
                    ctx -> sessionOwners.computeIfAbsent(SessionKey.from(ctx.session()), k -> new Object()),
                    new PetriAgent.LiveConfig(
                            ctx -> connection,
                            (msg, runner) -> {
                                callbackFrames.incrementAndGet();
                                if (msg.serverContent().isEmpty()) {
                                    runner.signal(CALLBACK_SIGNAL);
                                }
                                // Model content reaches egress only by entering the net.
                                msg.serverContent()
                                        .flatMap(LiveServerContent::modelTurn)
                                        .ifPresent(c -> runner.inject(MODEL_CHUNK, c));
                            }));

            var adkRunner = new InMemoryRunner(agent);
            var session = adkRunner.sessionService()
                    .createSession(adkRunner.appName(), "live-user", (Map<String, Object>) null, "live-session")
                    .blockingGet();

            var queue = new com.google.adk.agents.LiveRequestQueue();
            events = adkRunner.runLive(
                            session,
                            queue,
                            RunConfig.builder().streamingMode(RunConfig.StreamingMode.BIDI).build())
                    .test();

            Content userContent = content("user", "hello over live");
            queue.content(userContent);
            // LiveRequestQueue delivery is not guaranteed synchronous; poll like
            // BidiPetriAgentTest rather than assert on the calling thread.
            await(() -> !connection.contentSends.isEmpty(), 2000);
            assertThat(connection.contentSends).containsExactly(userContent);

            connection.raw.onNext(signalFrame());
            events.awaitCount(1);
            assertThat(callbackFrames.get()).isEqualTo(1);
            assertThat(events.values()).hasSize(1);
            Event callbackEvent = events.values().get(0);
            assertThat(callbackEvent.author()).isEqualTo("callback_net");
            assertThat(callbackEvent.content().map(Content::text)).hasValue("callback fired");

            connection.raw.onNext(modelContent("model says hi"));
            events.awaitCount(2);

            Event modelEvent = events.values().stream()
                    .filter(e -> "model_net".equals(e.author()))
                    .findFirst()
                    .orElseThrow(() -> new AssertionError("no net-authored model event"));
            assertThat(modelEvent.content().map(Content::text)).hasValue("model says hi");
            // Authored by the net's emit transition, so the turn flag is the net's too.
            assertThat(modelEvent.partial()).hasValue(true);
            assertThat(callbackFrames.get()).isEqualTo(2);

            queue.close();
        } finally {
            if (events != null) {
                events.cancel();
            }
            registry.closeAll();
        }
    }

    private static ConcurrentMap<SessionKey, Object> sessionOwnerMap() {
        return new ConcurrentHashMap<>();
    }

    private static void await(BooleanSupplier cond, long timeoutMillis) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (System.currentTimeMillis() < deadline) {
            if (cond.getAsBoolean()) return;
            Thread.sleep(20);
        }
        throw new AssertionError("Condition was not met within " + timeoutMillis + "ms");
    }

    private static PetriRunner callbackSignalRunner() {
        TransitionAction emitCallback = ctx -> {
            ctx.input(CALLBACK_SIGNAL);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .id(Event.generateEventId())
                    .invocationId("callback-invocation")
                    .author("callback_net")
                    .content(content("model", "callback fired"))
                    .build());
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        };
        TransitionAction emitModelChunk = ctx -> {
            Content chunk = ctx.input(MODEL_CHUNK);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .id(Event.generateEventId())
                    .invocationId("callback-invocation")
                    .author("model_net")
                    .content(chunk)
                    .partial(true)
                    .build());
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        };
        Transition emit = Transition.builder("T_CallbackSignal")
                .inputs(Arc.In.one(CALLBACK_SIGNAL))
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();
        Transition emitChunk = Transition.builder("T_ModelChunk")
                .inputs(Arc.In.one(MODEL_CHUNK))
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();
        PetriNet net = PetriNet.builder("petri-agent-live-test")
                .place(CALLBACK_SIGNAL)
                .place(MODEL_CHUNK)
                .place(AdkColours.EVENT_OUT)
                .transition(emit)
                .transition(emitChunk)
                .build()
                .bindActions(Map.of(
                        "T_CallbackSignal", emitCallback,
                        "T_ModelChunk", emitModelChunk));
        return PetriRunner.builder(net)
                .environmentPlace(CALLBACK_SIGNAL)
                .environmentPlace(MODEL_CHUNK)
                .actionExecutor(EXECUTOR)
                .orchestratorExecutor(EXECUTOR)
                .start();
    }

    private static LiveServerMessage signalFrame() {
        return LiveServerMessage.builder().build();
    }

    private static LiveServerMessage modelContent(String text) {
        return LiveServerMessage.builder()
                .serverContent(LiveServerContent.builder()
                        .modelTurn(content("model", text))
                        .build())
                .build();
    }

    private static Content content(String role, String text) {
        return Content.builder().role(role).parts(List.of(Part.fromText(text))).build();
    }

    /** Records live sends and exposes a raw server stream driven by the test. */
    private static final class FakeLiveConnection implements LiveConnection {
        final PublishProcessor<LiveServerMessage> raw = PublishProcessor.create();
        final List<Blob> realtimeSends = new CopyOnWriteArrayList<>();
        final List<Content> contentSends = new CopyOnWriteArrayList<>();
        final AtomicInteger closes = new AtomicInteger();

        @Override public Flowable<LiveServerMessage> rawReceive() { return raw.serialize(); }
        @Override public Completable sendRealtime(Blob blob) { realtimeSends.add(blob); return Completable.complete(); }
        @Override public Completable sendContent(Content content) { contentSends.add(content); return Completable.complete(); }
        @Override public Completable sendHistory(List<Content> history) { return Completable.complete(); }
        @Override public Flowable<LlmResponse> receive() { return Flowable.empty(); }
        @Override public void close() { closes.incrementAndGet(); }
        @Override public void close(Throwable t) { closes.incrementAndGet(); }
    }
}
