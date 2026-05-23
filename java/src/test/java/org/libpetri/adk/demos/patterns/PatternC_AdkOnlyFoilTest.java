package org.libpetri.adk.demos.patterns;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.BaseAgent;
import com.google.adk.agents.InvocationContext;
import com.google.adk.agents.LlmAgent;
import com.google.adk.agents.LoopAgent;
import com.google.adk.agents.RunConfig;
import com.google.adk.agents.SequentialAgent;
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
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

/**
 * Pattern C's ADK-only foil — what you have to write to express
 * "optimistic cheap with fallback to slow" using stock ADK
 * orchestration agents, and what's missing.
 *
 * <ol>
 *   <li>{@code loop_agent_only_retries_same_agent} —
 *       {@link LoopAgent} repeats the <i>same</i> sub-agent N times
 *       using RxJava's {@code .repeat(N)}. There is no path-switching
 *       semantics: a failing cheap branch can't fall back to a
 *       different (slow) agent.</li>
 *   <li>{@code sequential_agent_runs_slow_even_when_cheap_validates} —
 *       {@link SequentialAgent} runs every sub-agent in order. There is
 *       no conditional skip — when the cheap agent's output is good
 *       enough, the slow agent still runs, wasting time and tokens.</li>
 *   <li>{@code conditional_fallback_requires_custom_base_agent_escape}
 *       — to get cheap-with-conditional-fallback today you must escape
 *       into a custom {@link BaseAgent} that reads the cheap agent's
 *       emitted event content (or {@code Session.state}) and
 *       conditionally invokes the slow agent. The Petri version
 *       expresses the same as a topological consequence of
 *       {@code VALIDATION_PASSED}/{@code VALIDATION_FAILED} flags, with
 *       a Z3 proof that at-most-one commit fires per turn.</li>
 * </ol>
 */
class PatternC_AdkOnlyFoilTest {

    @Test
    void loop_agent_only_retries_same_agent() throws Exception {
        // LoopAgent.builder().subAgents(cheap).maxIterations(3) repeats the
        // SAME cheap sub-agent three times. There is no path-switching
        // primitive — you can't say "if cheap fails, fall back to slow"
        // inside a LoopAgent.
        AtomicInteger cheapInvocations = new AtomicInteger();
        var cheap = LlmAgent.builder()
                .name("cheap_only")
                .description("Counts invocations to prove LoopAgent reuses the same sub-agent")
                .model(countingScriptedLlm("cheap-answer", cheapInvocations))
                .build();

        var loop = LoopAgent.builder()
                .name("loop_fallback_attempt")
                .description("Tries to express cheap-with-fallback as a LoopAgent — can't")
                .subAgents(cheap)
                .maxIterations(3)
                .build();

        var runner = new InMemoryRunner(loop);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                .blockingGet();

        var events = runner.runAsync(
                        session.userId(), session.id(),
                        userMessage("retry me"),
                        RunConfig.builder().build())
                .toList().blockingGet();

        // The cheap sub-agent ran exactly maxIterations times — never a
        // different (slow) sub-agent. LoopAgent has no "switch agents on
        // condition" semantics.
        assertThat(cheapInvocations.get()).isEqualTo(3);
        var cheapEvents = events.stream()
                .filter(e -> "cheap_only".equals(e.author())).toList();
        assertThat(cheapEvents).hasSize(3);
    }

    @Test
    void sequential_agent_runs_slow_even_when_cheap_validates() throws Exception {
        // SequentialAgent runs every sub-agent in order. When the cheap
        // path's output is already good, the slow path still runs,
        // burning latency and tokens. There is no conditional-skip
        // primitive in the agent vocabulary.
        AtomicInteger cheapInvocations = new AtomicInteger();
        AtomicInteger slowInvocations  = new AtomicInteger();

        var cheap = LlmAgent.builder()
                .name("cheap")
                .description("Fast, usually fine")
                .model(countingScriptedLlm("cheap-answer", cheapInvocations))
                .build();
        var slow = LlmAgent.builder()
                .name("slow")
                .description("Slow, always thorough")
                .model(countingScriptedLlm("slow-answer", slowInvocations))
                .build();

        var sequence = SequentialAgent.builder()
                .name("seq_cheap_then_slow")
                .description("Stock ADK attempt at optimistic-commit")
                .subAgents(cheap, slow)
                .build();

        var runner = new InMemoryRunner(sequence);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                .blockingGet();

        var events = runner.runAsync(
                        session.userId(), session.id(),
                        userMessage("answer me"),
                        RunConfig.builder().build())
                .toList().blockingGet();

        // The damning assertion: even though cheap's answer is fine,
        // slow ran anyway. SequentialAgent has no concept of "stop early
        // if previous output validates."
        assertThat(cheapInvocations.get()).isEqualTo(1);
        assertThat(slowInvocations.get()).isEqualTo(1);
        var agentAuthors = events.stream()
                .map(Event::author)
                .filter(a -> "cheap".equals(a) || "slow".equals(a))
                .toList();
        assertThat(agentAuthors).containsExactly("cheap", "slow");
    }

    @Test
    void conditional_fallback_requires_custom_base_agent_escape() throws Exception {
        // The escape: a custom BaseAgent that inspects the cheap agent's
        // event content and conditionally invokes slow. This works but:
        //   - the conditional lives in caller code, not in the agent
        //     graph (invisible to ADK config-driven topologies);
        //   - the cheap-output validation logic is hand-rolled in Java
        //     (the Petri version puts it in a single Validate transition
        //     with an XOR output);
        //   - there is no path to "pre-warm" the slow agent in parallel
        //     so its result is ready if cheap fails — the custom
        //     subscribes to cheap first, then sequentially to slow;
        //   - if you DO try to pre-warm (subscribe to both upfront), you
        //     have to manually wire cancellation of the loser and the
        //     race-against-validation by hand — at which point you've
        //     re-implemented Pattern C as untyped Rx code.
        AtomicInteger cheapInvocations = new AtomicInteger();
        AtomicInteger slowInvocations  = new AtomicInteger();

        var cheap = LlmAgent.builder()
                .name("cheap")
                .description("Fast")
                .model(scoredLlm("cheap-answer", /*score*/ 10, cheapInvocations))
                .build();
        var slow = LlmAgent.builder()
                .name("slow")
                .description("Slow but thorough")
                .model(scoredLlm("slow-answer", /*score*/ 100, slowInvocations))
                .build();

        BaseAgent optimistic = new BaseAgent(
                "opt_custom", "Custom escape — cheap with conditional fallback",
                List.of(cheap, slow), null, null) {
            @Override
            protected Flowable<Event> runAsyncImpl(InvocationContext ctx) {
                return cheap.runAsync(ctx)
                        .toList()
                        .flatMapPublisher(cheapEvents -> {
                            // Hand-rolled validation: parse the cheap
                            // event content and decide. In a real app
                            // this would be a structured tool call or
                            // a side-effecting LLM-as-judge step.
                            String text = cheapEvents.stream()
                                    .filter(e -> "cheap".equals(e.author()))
                                    .findFirst()
                                    .flatMap(Event::content)
                                    .map(Content::text)
                                    .map(String::strip)
                                    .map(this::extractScoreText)
                                    .orElse("");
                            int score = parseScore(text);
                            if (score >= 50) {
                                return Flowable.fromIterable(cheapEvents);
                            }
                            return Flowable.fromIterable(cheapEvents)
                                    .concatWith(slow.runAsync(ctx));
                        });
            }
            @Override
            protected Flowable<Event> runLiveImpl(InvocationContext ctx) {
                return Flowable.error(new UnsupportedOperationException());
            }

            private String extractScoreText(String t) {
                int idx = t.indexOf("score=");
                return idx < 0 ? "" : t.substring(idx + 6);
            }
            private int parseScore(String t) {
                try { return Integer.parseInt(t.trim()); }
                catch (NumberFormatException _) { return 0; }
            }
        };

        var runner = new InMemoryRunner(optimistic);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                .blockingGet();

        var events = runner.runAsync(
                        session.userId(), session.id(),
                        userMessage("answer me"),
                        RunConfig.builder().build())
                .toList().blockingGet();

        // The custom escape works — cheap ran, was judged inadequate
        // (score=10 < threshold=50), slow ran.
        assertThat(cheapInvocations.get()).isEqualTo(1);
        assertThat(slowInvocations.get()).isEqualTo(1);
        var agentAuthors = events.stream()
                .map(Event::author)
                .filter(a -> "cheap".equals(a) || "slow".equals(a))
                .toList();
        assertThat(agentAuthors).containsExactly("cheap", "slow");

        // Compare to PatternC_OptimisticCommitDemoTest: the same logic
        // is ~30 LOC of net builder calls. The validation transition's
        // XOR output makes the cheap-vs-slow choice a topology property,
        // and Z3 proves at-most-one commit fires per turn. The custom
        // BaseAgent above gives no such structural guarantee — a refactor
        // that double-subscribes to slow, or fails to handle the empty
        // cheap-events case, would silently break invariants.
    }

    // ============================================================
    //  Fixtures
    // ============================================================

    private static BaseLlm countingScriptedLlm(String text, AtomicInteger counter) {
        var response = LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText(text)))
                        .build())
                .build();
        return new BaseLlm("counting") {
            @Override
            public Flowable<LlmResponse> generateContent(LlmRequest r, boolean stream) {
                counter.incrementAndGet();
                return Flowable.just(response);
            }
            @Override
            public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    private static BaseLlm scoredLlm(String label, int score, AtomicInteger counter) {
        var response = LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText(label + " score=" + score)))
                        .build())
                .build();
        return new BaseLlm("scored") {
            @Override
            public Flowable<LlmResponse> generateContent(LlmRequest r, boolean stream) {
                counter.incrementAndGet();
                return Flowable.just(response);
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
