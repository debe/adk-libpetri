package org.libpetri.adk.demos.patterns;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.BaseAgent;
import com.google.adk.agents.InvocationContext;
import com.google.adk.agents.LlmAgent;
import com.google.adk.agents.ParallelAgent;
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
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;

/**
 * Pattern B's ADK-only foil — the two ways to attempt K-of-N quorum with
 * stock ADK, and what they cost.
 *
 * <ol>
 *   <li>{@code parallel_agent_waits_for_all_n_not_k} —
 *       {@link ParallelAgent} streams events from all sub-agents but the
 *       overall {@code Flowable} only completes when <i>every</i>
 *       sub-agent finishes. Even if the caller only needs K answers,
 *       all N branches run to completion, and runtime is bounded below
 *       by the slowest. There is no built-in K-of-N synchronisation
 *       primitive in ADK's agent vocabulary.</li>
 *   <li>{@code rx_take_k_drops_late_results_silently} — escaping into a
 *       custom {@link BaseAgent} that uses {@code Flowable.merge(...)
 *       .take(K)} achieves the early-completion behaviour but the
 *       (N-K) late results never reach the caller. Any post-quorum
 *       supplemental information they would have contributed (e.g. the
 *       slow-but-thorough branch's correction of the consensus) is
 *       silently lost. The Petri version preserves these via a
 *       dedicated {@code DISCARDED} sink that a parent net can wire to
 *       a supplemental-update path if desired.</li>
 * </ol>
 */
class PatternB_AdkOnlyFoilTest {

    @Test
    void parallel_agent_waits_for_all_n_not_k() throws Exception {
        var parallel = ParallelAgent.builder()
                .name("quorum_parallel")
                .description("Stock ADK attempt at K-of-N quorum (it's not)")
                .subAgents(
                        llmSubAgent("b1", 30),
                        llmSubAgent("b2", 60),
                        llmSubAgent("b3", 90),
                        llmSubAgent("b4", 400),
                        llmSubAgent("b5", 800))
                .build();

        var runner = new InMemoryRunner(parallel);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                .blockingGet();

        long start = System.nanoTime();
        var events = runner.runAsync(
                        session.userId(), session.id(),
                        userMessage("synthesize"),
                        RunConfig.builder().build())
                .toList().blockingGet();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        // All N branches emit; no early synthesis on K.
        var perBranchEvents = events.stream()
                .filter(e -> e.author() != null && e.author().startsWith("b"))
                .toList();
        assertThat(perBranchEvents).hasSize(5);

        // Runtime bounded below by the slowest sub-agent.
        assertThat(elapsedMs).isAtLeast(800L);
    }

    @Test
    void rx_take_k_drops_late_results_silently() throws Exception {
        final int K = 3;

        BaseAgent quorumEscape = new BaseAgent(
                "quorum_custom", "Custom escape for K-of-N early completion",
                List.of(llmSubAgent("b1", 30),
                        llmSubAgent("b2", 60),
                        llmSubAgent("b3", 90),
                        llmSubAgent("b4", 400),
                        llmSubAgent("b5", 800)),
                null, null) {
            @Override
            protected Flowable<Event> runAsyncImpl(InvocationContext ctx) {
                List<Flowable<Event>> streams = new ArrayList<>();
                for (BaseAgent sub : subAgents()) {
                    streams.add(sub.runAsync(ctx));
                }
                // .take(K): completes after K elements; the rest are
                // upstream-disposed and never observed downstream. There
                // is no path for late values to reach a supplemental sink.
                return Flowable.merge(streams).take(K);
            }
            @Override
            protected Flowable<Event> runLiveImpl(InvocationContext ctx) {
                return Flowable.error(new UnsupportedOperationException());
            }
        };

        var runner = new InMemoryRunner(quorumEscape);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                .blockingGet();

        var events = runner.runAsync(
                        session.userId(), session.id(),
                        userMessage("synthesize"),
                        RunConfig.builder().build())
                .toList().blockingGet();

        // Only the first K branches' events reach the caller.
        var perBranchEvents = events.stream()
                .filter(e -> e.author() != null && e.author().startsWith("b"))
                .toList();
        assertThat(perBranchEvents).hasSize(K);

        // What's NOT here is the point: b4 and b5's events are lost. If
        // they had supplemental value (a correction, a slower-but-more-
        // accurate source), there's no recovery path. The Petri version
        // routes late arrivals to a DISCARDED sink that a parent net can
        // observe — making the "supplemental update" path a structural
        // choice rather than a silent drop.
        var lateBranchEvents = events.stream()
                .filter(e -> "b4".equals(e.author()) || "b5".equals(e.author()))
                .toList();
        assertThat(lateBranchEvents).isEmpty();
    }

    // ============================================================
    //  Fixtures
    // ============================================================

    private static LlmAgent llmSubAgent(String name, long delayMs) {
        return LlmAgent.builder()
                .name(name)
                .description("LLM branch '" + name + "' with " + delayMs + "ms latency")
                .model(delayedScriptedLlm(name, delayMs))
                .build();
    }

    private static BaseLlm delayedScriptedLlm(String branchId, long delayMs) {
        var response = LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText("answer from " + branchId)))
                        .build())
                .build();
        return new BaseLlm("delayed-" + branchId) {
            @Override
            public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.just(response)
                        .delay(delayMs, TimeUnit.MILLISECONDS);
            }
            @Override
            public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    private static Content userMessage(String text) {
        return Content.builder().role("user")
                .parts(List.of(Part.fromText(text))).build();
    }
}
