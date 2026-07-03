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
import io.reactivex.rxjava3.core.Flowable;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmStreamingStepSubnet;
import org.libpetri.adk.subnet.StreamingLlmAgentSubnet;
import org.libpetri.core.PetriNet;
import org.libpetri.runtime.PetriNetExecutor;

class PetriAgentSseTest {

    private static final String AGENT_NAME = "sse_agent";

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
    void sse_mode_emits_ordered_partials_then_final_event_and_completes_with_one_invocation_id() {
        var events = runStreamingTurn(
                streamingLlm(List.of(
                        chunkResponse("Sure, "),
                        chunkResponse("the answer "),
                        chunkResponse("is 42."))),
                RunConfig.builder().streamingMode(RunConfig.StreamingMode.SSE).build());

        assertThat(events).hasSize(4);
        assertThat(events.stream().map(e -> e.content().get().text()).toList())
                .containsExactly("Sure, ", "the answer ", "is 42.", "Sure, the answer is 42.")
                .inOrder();

        var partials = events.subList(0, events.size() - 1);
        assertThat(partials).hasSize(3);
        for (Event partial : partials) {
            assertThat(partial.partial()).hasValue(Boolean.TRUE);
            assertThat(partial.author()).isEqualTo(AGENT_NAME);
        }

        Event finalEvent = events.getLast();
        assertThat(finalEvent.partial().orElse(false)).isFalse();
        assertThat(finalEvent.author()).isEqualTo(AGENT_NAME);

        String turnInvocationId = events.getFirst().invocationId();
        assertThat(turnInvocationId).isNotEmpty();
        assertThat(turnInvocationId).doesNotContain("net-generated-");
        assertThat(events.stream().map(Event::invocationId).distinct().toList())
                .containsExactly(turnInvocationId);
    }

    @Test
    void none_mode_on_streaming_net_returns_one_terminal_non_partial_event() {
        var events = runStreamingTurn(
                streamingLlm(List.of(
                        chunkResponse("Sure, "),
                        chunkResponse("the answer "),
                        chunkResponse("is 42."))),
                RunConfig.builder().streamingMode(RunConfig.StreamingMode.NONE).build());

        assertThat(events).hasSize(1);
        Event only = events.getFirst();
        assertThat(only.partial().orElse(false)).isFalse();
        assertThat(only.author()).isEqualTo(AGENT_NAME);
        assertThat(only.content().get().text()).isEqualTo("Sure, the answer is 42.");
    }

    private static List<Event> runStreamingTurn(BaseLlm llm, RunConfig runConfig) {
        var registry = SessionExecutorRegistry.strongOwned();
        try {
            var sessionOwners = sessionOwnerMap();
            var execRef = new AtomicReference<PetriNetExecutor>();
            var suppliedInvocationIds = new AtomicInteger();
            var config = StreamingLlmAgentSubnet.Config.builder(AGENT_NAME, "fake-model")
                    .dispatchExecutor(EXECUTOR)
                    .chunkBudget(4)
                    .invocationIdSupplier(() -> "net-generated-" + suppliedInvocationIds.incrementAndGet())
                    .executorRef(execRef)
                    .build();
            var net = PetriNet.builder("streaming-agent-test-net")
                    .compose(StreamingLlmAgentSubnet.DEF)
                    .build()
                    .bindActions(StreamingLlmAgentSubnet.actionBindings(llm, config));
            var agent = PetriAgent.of(
                    AGENT_NAME,
                    "Streaming SSE test agent",
                    registry,
                    key -> PetriRunner.builder(net)
                            .environmentPlace(AdkColours.USER_IN)
                            .environmentPlace(LlmStreamingStepSubnet.Places.CHUNK)
                            .deferredExecutorRef(execRef)
                            .actionExecutor(EXECUTOR)
                            .orchestratorExecutor(EXECUTOR)
                            .start(),
                    ctx -> sessionOwners.computeIfAbsent(
                            SessionKey.from(ctx.session()), ignored -> new Object()));

            var runner = new InMemoryRunner(agent);
            var session = runner.sessionService()
                    .createSession(runner.appName(), "user-1", (Map<String, Object>) null,
                            "session-" + UUID.randomUUID())
                    .blockingGet();

            var sub = runner.runAsync(
                            session.userId(),
                            session.id(),
                            userMessage("what's 6*7"),
                            runConfig)
                    .test();
            sub.awaitDone(3, TimeUnit.SECONDS);
            sub.assertComplete();
            sub.assertNoErrors();
            return List.copyOf(sub.values());
        } finally {
            registry.closeAll();
        }
    }

    private static ConcurrentMap<SessionKey, Object> sessionOwnerMap() {
        return new ConcurrentHashMap<>();
    }

    private static Content userMessage(String text) {
        return Content.builder().role("user")
                .parts(List.of(Part.fromText(text)))
                .build();
    }

    private static LlmResponse chunkResponse(String text) {
        return LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText(text)))
                        .build())
                .build();
    }

    private static BaseLlm streamingLlm(List<LlmResponse> chunks) {
        return new BaseLlm("streaming") {
            @Override
            public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
                return Flowable.fromIterable(chunks);
            }

            @Override
            public BaseLlmConnection connect(LlmRequest request) {
                throw new UnsupportedOperationException("streamingLlm.connect()");
            }
        };
    }
}
