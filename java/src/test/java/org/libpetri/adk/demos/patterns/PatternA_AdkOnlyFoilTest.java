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
 * Pattern A's ADK-only foil — what you have to write today to express a
 * speculative race using stock {@link ParallelAgent}, and what breaks.
 *
 * <p>Two tests:
 * <ol>
 *   <li>{@code parallel_agent_emits_events_from_all_branches_no_first_wins} —
 *       {@link ParallelAgent} uses {@code Flowable.merge(...)} internally
 *       and only completes when <i>every</i> sub-agent finishes. Even if
 *       the caller cares only about the first useful answer, all three
 *       LLM calls run to completion and surface as events. The overall
 *       runtime is bounded below by the slowest branch. This is the
 *       barrier-like behavior speculative-race aims to avoid.</li>
 *   <li>{@code first_wins_requires_custom_base_agent_escape} — the only
 *       way to get first-wins with cancellation today is to drop out of
 *       the agent framework into a custom {@link BaseAgent} that uses
 *       Rx's {@code .firstElement()} on a merged {@link Flowable}. The
 *       test shows the ~40 lines of escape code, and notes the
 *       trade-offs: no per-branch callbacks fire cleanly, no
 *       per-sub-agent state isolation, cancellation of losers is
 *       best-effort (depends on whether the LLM honours dispose).</li>
 * </ol>
 *
 * <p>Compare with {@code PatternA_SpeculativeRaceDemoTest}: ~40 LOC of
 * builder calls express first-wins-with-structural-cancellation as a
 * topology property, with a Z3 proof that at-most-one commit ever fires.
 */
class PatternA_AdkOnlyFoilTest {

    @Test
    void parallel_agent_emits_events_from_all_branches_no_first_wins() throws Exception {
        var fast   = llmSubAgent("fast",   20);
        var medium = llmSubAgent("medium", 120);
        var slow   = llmSubAgent("slow",   300);

        var parallel = ParallelAgent.builder()
                .name("race_parallel")
                .description("Stock ADK attempt at a speculative race")
                .subAgents(fast, medium, slow)
                .build();

        var runner = new InMemoryRunner(parallel);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                .blockingGet();

        long start = System.nanoTime();
        var events = runner.runAsync(
                        session.userId(), session.id(),
                        userMessage("which branch wins?"),
                        RunConfig.builder().build())
                .toList().blockingGet();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        // The first failure mode: ALL branches emit, not just the winner.
        var perBranchEvents = events.stream()
                .filter(e -> List.of("fast", "medium", "slow").contains(e.author()))
                .toList();
        assertThat(perBranchEvents).hasSize(3);

        // The second failure mode: runtime is bounded below by the slowest
        // branch — the merged Flowable only completes when every sub-agent
        // does. There is no path to "first useful answer wins; cancel rest"
        // inside the agent tree.
        assertThat(elapsedMs).isAtLeast(300L);
    }

    @Test
    void first_wins_requires_custom_base_agent_escape() throws Exception {
        // The framework escape: a hand-written BaseAgent that runs the
        // sub-agents via merge(...).firstElement() outside the agent tree.
        // This DOES achieve first-wins behavior, but at the cost of:
        //   - per-sub-agent BeforeAgent / AfterAgent callbacks don't fire
        //     in their normal pre/post-event-stream positions;
        //   - the losers' resulting events never reach the caller, but
        //     whether the LLM calls themselves actually cancel depends on
        //     the BaseLlm implementation honouring Rx Disposable. With
        //     scripted/test LLMs this is fine; with real network LLMs
        //     using non-cancellable HTTP clients, the loser requests run
        //     to completion server-side anyway.
        //   - the merged stream surfaces only ONE event (the winner's),
        //     so any session-state writes the losing branches would have
        //     made simply don't happen — callers depending on cross-
        //     sub-agent state isolation get inconsistent results.
        BaseAgent firstWins = new BaseAgent(
                "first_wins_custom", "Custom escape for first-wins",
                List.of(llmSubAgent("fast", 20),
                        llmSubAgent("medium", 120),
                        llmSubAgent("slow", 300)),
                null, null) {
            @Override
            protected Flowable<Event> runAsyncImpl(InvocationContext ctx) {
                List<Flowable<Event>> streams = new ArrayList<>();
                for (BaseAgent sub : subAgents()) {
                    streams.add(sub.runAsync(ctx));
                }
                return Flowable.merge(streams)
                        .firstElement()
                        .toFlowable();
            }
            @Override
            protected Flowable<Event> runLiveImpl(InvocationContext ctx) {
                return Flowable.error(new UnsupportedOperationException());
            }
        };

        var runner = new InMemoryRunner(firstWins);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                .blockingGet();

        var events = runner.runAsync(
                        session.userId(), session.id(),
                        userMessage("which branch wins?"),
                        RunConfig.builder().build())
                .toList().blockingGet();

        // It works — the fast branch wins.
        var winnerEvents = events.stream()
                .filter(e -> List.of("fast", "medium", "slow").contains(e.author()))
                .toList();
        assertThat(winnerEvents).hasSize(1);
        assertThat(winnerEvents.get(0).author()).isEqualTo("fast");

        // But the compare-and-contrast is the point: the Petri version
        // (PatternA_SpeculativeRaceDemoTest, ~40 LOC of builder calls)
        // expresses the same thing as a structural property of the net,
        // with a Z3 proof that at-most-one commit fires per turn. This
        // foil needs a custom BaseAgent subclass plus careful Rx and
        // sacrifices ADK's per-sub-agent isolation.
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

    /** Scripted LLM that emits one fixed response after a delay. */
    private static BaseLlm delayedScriptedLlm(String branchId, long delayMs) {
        var response = LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText("answer from " + branchId)))
                        .build())
                .build();
        return new BaseLlm("delayed-" + branchId) {
            @Override
            public Flowable<LlmResponse> generateContent(LlmRequest r, boolean stream) {
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
