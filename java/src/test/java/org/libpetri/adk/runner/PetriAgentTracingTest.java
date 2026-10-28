package org.libpetri.adk.runner;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.runner.InMemoryRunner;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.reactivex.rxjava3.core.Flowable;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.bridge.OtelEventStore;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.core.PetriNet;
import org.libpetri.event.EventStore;

/** Per-session tracing: concurrent turns of two sessions keep their spans apart. */
class PetriAgentTracingTest {

    private static final String AGENT = "traced";
    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setup() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void teardown() {
        EXECUTOR.shutdownNow();
    }

    /**
     * Session A's model call blocks until session B's whole turn has run, so
     * A's turn finishes after B bound its span. With a store per session,
     * A's later transitions still attach to A's span.
     */
    @Test
    void overlapping_turns_of_two_sessions_parent_their_spans_on_their_own_turn() throws Exception {
        var exporter = InMemorySpanExporter.create();
        var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        var tracer = OpenTelemetrySdk.builder().setTracerProvider(provider).build().getTracer("test");

        var aCalled = new CountDownLatch(1);
        var releaseA = new CountDownLatch(1);
        var stores = new ConcurrentHashMap<SessionKey, OtelEventStore>();
        var registry = SessionExecutorRegistry.strongOwned();
        var agent = PetriAgent.builder(AGENT, registry, key -> {
                    var store = stores.computeIfAbsent(key, k -> new OtelEventStore(tracer, EventStore.noop()));
                    BaseLlm llm = key.sessionId().equals("a") ? blockingLlm(aCalled, releaseA) : answeringLlm();
                    return runner(llm, store);
                })
                .tracingPerSession(tracer, stores::get)
                .build();
        try {
            var adk = new InMemoryRunner(agent);
            for (var id : List.of("a", "b")) {
                adk.sessionService().createSession(adk.appName(), "u", (Map<String, Object>) null, id).blockingGet();
            }
            CompletableFuture<List<Event>> turnA = CompletableFuture.supplyAsync(
                    () -> turn(adk, "a"), EXECUTOR);
            assertThat(aCalled.await(5, TimeUnit.SECONDS)).isTrue();
            assertThat(turn(adk, "b")).isNotEmpty();
            releaseA.countDown();
            assertThat(turnA.get(5, TimeUnit.SECONDS)).isNotEmpty();
        } finally {
            registry.closeAll();
        }
        agent.endAllOpenInvocationSpans();  // a turn's span otherwise ends with the next turn
        provider.forceFlush().join(2, TimeUnit.SECONDS);

        var spans = exporter.getFinishedSpanItems();
        var invocations = spans.stream()
                .filter(s -> s.getName().equals("petri.invocation." + AGENT))
                .collect(Collectors.toMap(SpanData::getSpanId,
                        s -> s.getAttributes().asMap().entrySet().stream()
                                .filter(e -> e.getKey().getKey().equals("adk.session.id"))
                                .map(e -> (String) e.getValue()).findFirst().orElseThrow()));
        var childrenPerSession = spans.stream()
                .filter(s -> invocations.containsKey(s.getParentSpanId()))
                .collect(Collectors.groupingBy(s -> invocations.get(s.getParentSpanId()), Collectors.counting()));
        assertThat(childrenPerSession.keySet()).containsExactly("a", "b");
        assertThat(childrenPerSession.get("a")).isEqualTo(childrenPerSession.get("b"));
        provider.close();
    }

    private static List<Event> turn(InMemoryRunner adk, String sessionId) {
        var message = Content.builder().role("user").parts(List.of(Part.fromText("hi"))).build();
        return adk.runAsync("u", sessionId, message, RunConfig.builder().build()).toList().blockingGet();
    }

    private static PetriRunner runner(BaseLlm llm, OtelEventStore store) {
        var config = LlmAgentSubnet.Config.builder(AGENT, "fake-model").dispatchExecutor(EXECUTOR).build();
        var net = PetriNet.builder("traced-net")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));
        return PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .eventStore(store)
                .orchestratorExecutor(EXECUTOR)
                .start();
    }

    private static BaseLlm answeringLlm() {
        return new BaseLlm("answering") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.just(text("ok"));
            }
            @Override public BaseLlmConnection connect(LlmRequest r) { throw new UnsupportedOperationException(); }
        };
    }

    private static BaseLlm blockingLlm(CountDownLatch called, CountDownLatch release) {
        return new BaseLlm("blocking") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.fromCallable(() -> {
                    called.countDown();
                    release.await(5, TimeUnit.SECONDS);
                    return text("late");
                });
            }
            @Override public BaseLlmConnection connect(LlmRequest r) { throw new UnsupportedOperationException(); }
        };
    }

    private static LlmResponse text(String t) {
        return LlmResponse.builder()
                .content(Content.builder().role("model").parts(List.of(Part.fromText(t))).build())
                .build();
    }
}
