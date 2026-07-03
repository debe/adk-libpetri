package org.libpetri.adk.runner;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.subscribers.TestSubscriber;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;

class PetriRunnerTest {

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
    void send_and_observe_event_for_text_only_llm_response() throws Exception {
        var llm = scriptedLlm(textResponse("hello back"));
        var net = buildAgentNet(llm, "hello-agent");

        try (var runner = newRunner(net)) {
            // Subscribe BEFORE inject to avoid the hot-stream race.
            TestSubscriber<Event> sub = runner.adkEvents().take(1).test();

            runner.inject(AdkColours.USER_IN, userMessage("hi")).get(2, TimeUnit.SECONDS);

            sub.awaitDone(2, TimeUnit.SECONDS);
            sub.assertValueCount(1);
            sub.assertValue(e -> "hello back".equals(e.content().get().text()));
        }
    }

    @Test
    void runner_supports_multiple_sequential_sends() throws Exception {
        var llm = scriptedLlm(textResponse("r1"), textResponse("r2"), textResponse("r3"));
        var net = buildAgentNet(llm, "multi-agent");

        try (var runner = newRunner(net)) {
            // Collect every event the runner emits. Take 3.
            TestSubscriber<Event> sub = runner.adkEvents().take(3).test();

            runner.inject(AdkColours.USER_IN, userMessage("a")).get(2, TimeUnit.SECONDS);
            runner.inject(AdkColours.USER_IN, userMessage("b")).get(2, TimeUnit.SECONDS);
            runner.inject(AdkColours.USER_IN, userMessage("c")).get(2, TimeUnit.SECONDS);

            sub.awaitDone(3, TimeUnit.SECONDS);
            assertThat(sub.values().stream().map(e -> e.content().get().text()).toList())
                    .containsExactly("r1", "r2", "r3").inOrder();
        }
    }

    @Test
    void shutdown_completes_event_stream() throws Exception {
        var llm = scriptedLlm();   // never invoked
        var net = buildAgentNet(llm, "shutdown-test");

        var runner = newRunner(net);
        TestSubscriber<Event> sub = runner.adkEvents().test();

        runner.shutdown();

        sub.awaitDone(2, TimeUnit.SECONDS);
        sub.assertComplete();
        sub.assertNoValues();
    }

    @Test
    void drain_async_returns_immediately_and_await_termination_confirms_completion() throws Exception {
        // The split shutdown path from ADK_FINDINGS.md #6: drainAsync()
        // is fire-and-forget (right for @OnClose hooks where blocking
        // the caller is unacceptable); awaitTermination(Duration) is the
        // bounded wait that confirms teardown when the caller needs it.
        var llm = scriptedLlm();
        var net = buildAgentNet(llm, "drain-async-test");
        var runner = newRunner(net);
        TestSubscriber<Event> sub = runner.adkEvents().test();

        long start = System.nanoTime();
        runner.drainAsync();
        Duration drainLatency = Duration.ofNanos(System.nanoTime() - start);
        // Fire-and-forget: the caller's thread should not be blocked on
        // orchestrator teardown. A few ms is plenty of headroom; the
        // synchronous shutdown() path takes orders of magnitude longer.
        assertThat(drainLatency).isLessThan(Duration.ofMillis(100));

        assertThat(runner.awaitTermination(Duration.ofSeconds(2))).isTrue();
        sub.awaitDone(2, TimeUnit.SECONDS);
        sub.assertComplete();
    }

    @Test
    void await_termination_returns_false_when_orchestrator_is_still_running() throws Exception {
        // Without a drain, the orchestrator is alive — awaitTermination
        // must report timeout instead of blocking the caller forever.
        var llm = scriptedLlm();
        var net = buildAgentNet(llm, "await-timeout-test");
        try (var runner = newRunner(net)) {
            long start = System.nanoTime();
            boolean terminated = runner.awaitTermination(Duration.ofMillis(50));
            Duration elapsed = Duration.ofNanos(System.nanoTime() - start);

            assertThat(terminated).isFalse();
            // Sanity: we waited approximately the requested timeout, not
            // forever. Generous upper bound to tolerate CI noise.
            assertThat(elapsed).isLessThan(Duration.ofSeconds(2));
        }
    }

    @Test
    void signal_and_token_overload_inject_unit_tokens_onto_a_void_place() throws Exception {
        // The value overload inject(Place, T) rejects null, so a Place<Void> signal
        // (voice-activity edges, barge-in, END_INVOCATION) was previously un-injectable
        // through the public API. signal(Place<Void>) and inject(Place, Token) fix that;
        // each unit token here fires T_Ack and produces one EVENT_OUT.
        var net = buildSignalNet();
        try (var runner = PetriRunner.builder(net)
                .environmentPlace(SIGNAL)
                .actionExecutor(EXECUTOR)
                .orchestratorExecutor(EXECUTOR)
                .start()) {
            TestSubscriber<Event> sub = runner.adkEvents().take(2).test();

            runner.signal(SIGNAL).get(2, TimeUnit.SECONDS);
            runner.inject(SIGNAL, Token.unit()).get(2, TimeUnit.SECONDS);

            sub.awaitDone(2, TimeUnit.SECONDS);
            sub.assertValueCount(2);
        }
    }

    // ============================================================
    //  Fixtures
    // ============================================================

    private static final Place<Void> SIGNAL = Place.of("signalTest_sig", Void.class);

    private static PetriNet buildSignalNet() {
        TransitionAction ack = ctx -> {
            ctx.input(SIGNAL);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .id(Event.generateEventId()).invocationId("inv").author("net").build());
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        };
        Transition emit = Transition.builder("T_Ack")
                .inputs(Arc.In.one(SIGNAL))
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();
        return PetriNet.builder("signal-test")
                .place(SIGNAL)
                .place(AdkColours.EVENT_OUT)
                .transition(emit)
                .build()
                .bindActions(Map.of("T_Ack", ack));
    }

    private static PetriRunner newRunner(PetriNet net) {
        return PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .actionExecutor(EXECUTOR)
                .orchestratorExecutor(EXECUTOR)
                .start();
    }

    private static PetriNet buildAgentNet(BaseLlm llm, String name) {
        var config = LlmAgentSubnet.Config.builder(name, "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build();
        return PetriNet.builder("test-host")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));
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

    private static BaseLlm scriptedLlm(LlmResponse... responses) {
        var queue = new ArrayDeque<LlmResponse>(List.of(responses));
        return new BaseLlm("scripted") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                var next = queue.poll();
                if (next == null) {
                    return Flowable.error(new IllegalStateException("scriptedLlm exhausted"));
                }
                return Flowable.just(next);
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }
}
