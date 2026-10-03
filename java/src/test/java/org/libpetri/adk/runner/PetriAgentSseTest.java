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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.subnet.StreamingLlmAgentSubnet;

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

    /**
     * Two live sessions must each get their own chunks. The streaming step
     * injects chunks through an executor reference; when one bound net and one
     * reference were shared by every session's runner, each new session
     * overwrote the reference, and an earlier session's chunks were injected
     * into the newest session's executor, so that turn never completed.
     */
    @Test
    void concurrent_sse_sessions_each_receive_only_their_own_chunks() {
        var sse = RunConfig.builder().streamingMode(RunConfig.StreamingMode.SSE).build();
        var registry = SessionExecutorRegistry.strongOwned();
        try {
            var runner = new InMemoryRunner(agentFor(echoingLlm(), registry));
            var alice = newSession(runner, "alice");
            var bob = newSession(runner, "bob");

            // Start both sessions' runners before either sends again, so the
            // second runner exists when the first session's turn streams.
            assertThat(finalText(runTurn(runner, alice, "hello from alice", sse)))
                    .isEqualTo("echo: hello from alice");
            assertThat(finalText(runTurn(runner, bob, "hello from bob", sse)))
                    .isEqualTo("echo: hello from bob");
            assertThat(finalText(runTurn(runner, alice, "alice again", sse)))
                    .isEqualTo("echo: alice again");
        } finally {
            registry.closeAll();
        }
    }

    private static String finalText(List<Event> events) {
        return events.getLast().content().get().text();
    }

    private static com.google.adk.sessions.Session newSession(InMemoryRunner runner, String user) {
        return runner.sessionService()
                .createSession(runner.appName(), user, (Map<String, Object>) null,
                        "session-" + UUID.randomUUID())
                .blockingGet();
    }

    private static List<Event> runTurn(InMemoryRunner runner,
                                       com.google.adk.sessions.Session session,
                                       String text, RunConfig runConfig) {
        var sub = runner.runAsync(session.userId(), session.id(), userMessage(text), runConfig)
                .test();
        sub.awaitDone(3, TimeUnit.SECONDS);
        sub.assertComplete();
        sub.assertNoErrors();
        return List.copyOf(sub.values());
    }

    /** Streams "echo: " plus the request's last user text, in two chunks. */
    private static BaseLlm echoingLlm() {
        return new BaseLlm("echoing") {
            @Override
            public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
                String text = request.contents().getLast().text();
                return Flowable.just(chunkResponse("echo: "), chunkResponse(text));
            }

            @Override
            public BaseLlmConnection connect(LlmRequest request) {
                throw new UnsupportedOperationException("echoingLlm.connect()");
            }
        };
    }

    private static List<Event> runStreamingTurn(BaseLlm llm, RunConfig runConfig) {
        var registry = SessionExecutorRegistry.strongOwned();
        try {
            var suppliedInvocationIds = new AtomicInteger();
            var config = StreamingLlmAgentSubnet.Config.builder(AGENT_NAME, "fake-model")
                    .dispatchExecutor(EXECUTOR)
                    .invocationIdSupplier(() -> "net-generated-" + suppliedInvocationIds.incrementAndGet())
                    .build();
            var runner = new InMemoryRunner(agentFor(llm, registry, config));
            return runTurn(runner, newSession(runner, "user-1"), "what's 6*7", runConfig);
        } finally {
            registry.closeAll();
        }
    }

    private static PetriAgent agentFor(BaseLlm llm, SessionExecutorRegistry registry) {
        return agentFor(llm, registry, StreamingLlmAgentSubnet.Config.builder(AGENT_NAME, "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build());
    }

    private static PetriAgent agentFor(BaseLlm llm, SessionExecutorRegistry registry,
                                       StreamingLlmAgentSubnet.Config config) {
        return PetriAgent.builder(AGENT_NAME, registry,
                StreamingLlmAgentSubnet.runnerFactory(llm, config,
                        b -> b.orchestratorExecutor(EXECUTOR)))
                .description("Streaming SSE test agent")
                .build();
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
