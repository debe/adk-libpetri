package org.libpetri.adk.demos.patterns;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.runner.InMemoryRunner;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import com.microsoft.z3.Context;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Predicate;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.runner.PetriAgent;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.adk.runner.SessionExecutorRegistry;
import org.libpetri.adk.runner.SessionKey;
import org.libpetri.adk.verify.AdkNetInvariants;
import org.libpetri.smt.SmtProperty;
import org.libpetri.smt.SmtVerifier;

/**
 * Pattern C — optimistic commit with structural fallback (pre-warmed).
 *
 * <p>The cheap and slow paths fire concurrently from the same
 * {@code USER_IN}. The cheap result goes through a validation
 * transition; on pass, the cheap path commits and the slow result is
 * structurally discarded. On fail, the slow path commits (waiting for
 * its result if not yet ready). Crucially, the cheap-vs-slow choice is
 * a topological consequence of {@code VALIDATION_PASSED} /
 * {@code VALIDATION_FAILED} and {@code COMMITTED} markings — there is
 * no caller-side retry orchestration, no {@code LoopAgent}, no
 * {@code Session.state} field carrying the validation result across
 * agent boundaries.
 *
 * <h2>Why this exists</h2>
 * <p>ADK's {@link com.google.adk.agents.LoopAgent} retries the
 * <i>same</i> agent on a condition; it does not switch agents
 * mid-retry. Expressing "try cheap, validate, fall back to slow"
 * requires nesting {@code SequentialAgent(cheap, validator,
 * LoopAgent(slow))} and plumbing the validation outcome via
 * {@code Session.state}. The {@code Session.state} write/read pair
 * races across concurrent invocations on the same session.
 * {@code PatternC_AdkOnlyFoilTest} demonstrates the race.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [USER_IN]       --T_StartBoth--> AND(CHEAP_TRIGGER, SLOW_TRIGGER)
 *                                    reset(COMMITTED, VALIDATION_*,
 *                                          CHEAP_DONE, SLOW_DONE)
 *   [CHEAP_TRIGGER] --T_RunCheap-->  [CHEAP_DONE]
 *   [SLOW_TRIGGER]  --T_RunSlow-->   [SLOW_DONE]   (inhibitor COMMITTED)
 *
 *   [CHEAP_DONE]    --T_Validate-->  XOR(VALIDATION_PASSED,
 *                                        VALIDATION_FAILED)
 *
 *   [VALIDATION_PASSED] --T_CommitCheap--> AND(EVENT_OUT, COMMITTED)
 *                                          inhibitor(COMMITTED)
 *   [SLOW_DONE]    --T_CommitSlow-->  AND(EVENT_OUT, COMMITTED)
 *                                     read(VALIDATION_FAILED),
 *                                     inhibitor(COMMITTED)
 *   [SLOW_DONE]    --T_DiscardSlow--> [SLOW_DISCARDED]
 *                                     read(COMMITTED)
 * </pre>
 *
 * <h2>Structural properties (Z3)</h2>
 * <ul>
 *   <li>{@code PlaceBound(COMMITTED, 1)} — at most one commit fires per turn.</li>
 *   <li>{@code atMostOneCommits(VALIDATION_PASSED, VALIDATION_FAILED)}
 *       — the validation outputs are XOR by construction, so the two
 *       commit transitions are never simultaneously enabled by their
 *       respective downstream guards.</li>
 *   <li>{@code deadlockFree} with all sink places declared.</li>
 * </ul>
 */
class PatternC_OptimisticCommitDemoTest {

    record BranchResult(String branchId, String text, int score) {}

    private static final Place<Void> CHEAP_TRIGGER = Place.of("cheapTrigger", Void.class);
    private static final Place<Void> SLOW_TRIGGER  = Place.of("slowTrigger",  Void.class);

    private static final Place<BranchResult> CHEAP_DONE =
            Place.of("cheapDone", BranchResult.class);
    private static final Place<BranchResult> SLOW_DONE =
            Place.of("slowDone", BranchResult.class);

    private static final Place<Void> VALIDATION_PASSED = Place.of("validationPassed", Void.class);
    private static final Place<Void> VALIDATION_FAILED = Place.of("validationFailed", Void.class);
    private static final Place<Void> COMMITTED        = Place.of("committed",        Void.class);
    private static final Place<BranchResult> SLOW_DISCARDED =
            Place.of("slowDiscarded", BranchResult.class);

    // Per-turn cached cheap result for T_CommitCheap to read.
    // Validation consumes CHEAP_DONE but produces only an XOR flag; we
    // stash the original for the commit step to author the event.
    private static final Place<BranchResult> CHEAP_PENDING =
            Place.of("cheapPending", BranchResult.class);

    private static final String T_START_BOTH    = "Opt_StartBoth";
    private static final String T_RUN_CHEAP     = "Opt_RunCheap";
    private static final String T_RUN_SLOW      = "Opt_RunSlow";
    private static final String T_VALIDATE      = "Opt_Validate";
    private static final String T_COMMIT_CHEAP  = "Opt_CommitCheap";
    private static final String T_COMMIT_SLOW   = "Opt_CommitSlow";
    private static final String T_DISCARD_SLOW  = "Opt_DiscardSlow";

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setUp() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void tearDown() {
        EXECUTOR.shutdown();
    }

    static boolean z3Available() {
        try { new Context().close(); return true; }
        catch (UnsatisfiedLinkError | NoClassDefFoundError _) { return false; }
    }

    @Test
    void cheap_path_commits_when_validation_passes_slow_discarded() throws Exception {
        // Validator passes iff score >= 50. Cheap produces score=100, so it passes.
        var bound = buildNet().bindActions(buildBindings(
                /*cheapScore*/ 100,
                /*slowScore*/  100,
                /*passThreshold*/ 50,
                Duration.ofMillis(30),   // cheap
                Duration.ofMillis(200))); // slow

        var events = runOneInvocation(bound, "answer me cheaply");

        var agentEvents = events.stream()
                .filter(e -> "opt_agent".equals(e.author())).toList();
        assertThat(agentEvents).hasSize(1);
        assertThat(agentEvents.get(0).content().get().text()).contains("cheap");
    }

    @Test
    void slow_path_commits_when_validation_fails() throws Exception {
        // Validator passes iff score >= 50. Cheap produces score=10, fails.
        // Slow produces score=100 and commits via the structural fallback.
        var bound = buildNet().bindActions(buildBindings(
                /*cheapScore*/ 10,
                /*slowScore*/  100,
                /*passThreshold*/ 50,
                Duration.ofMillis(30),
                Duration.ofMillis(150)));

        var events = runOneInvocation(bound, "give me a thorough answer");

        var agentEvents = events.stream()
                .filter(e -> "opt_agent".equals(e.author())).toList();
        assertThat(agentEvents).hasSize(1);
        assertThat(agentEvents.get(0).content().get().text()).contains("slow");
    }

    @Test
    @EnabledIf("z3Available")
    void optimistic_commit_net_proves_at_most_one_commit_per_turn() {
        var net = buildNet();
        var result = SmtVerifier.forNet(net)
                .initialMarking(b -> b.tokens(AdkColours.USER_IN, 1))
                .sinkPlaces(
                        AdkColours.EVENT_OUT,
                        COMMITTED,
                        SLOW_DISCARDED,
                        VALIDATION_PASSED,
                        VALIDATION_FAILED,
                        CHEAP_PENDING)
                .property(SmtProperty.placeBound(COMMITTED, 1))
                .property(AdkNetInvariants.atMostOneCommits(
                        VALIDATION_PASSED, VALIDATION_FAILED))
                .property(SmtProperty.deadlockFree())
                .verify();
        assertThat(result.isViolated()).isFalse();
    }

    // ============================================================
    //  Net construction
    // ============================================================

    private static PetriNet buildNet() {
        return PetriNet.builder("optimistic-commit")
                .place(AdkColours.USER_IN)
                .place(AdkColours.EVENT_OUT)
                .place(CHEAP_TRIGGER).place(SLOW_TRIGGER)
                .place(CHEAP_DONE).place(SLOW_DONE)
                .place(CHEAP_PENDING)
                .place(VALIDATION_PASSED).place(VALIDATION_FAILED)
                .place(COMMITTED)
                .place(SLOW_DISCARDED)

                .transition(Transition.builder(T_START_BOTH)
                        .inputs(Arc.In.one(AdkColours.USER_IN))
                        .resets(COMMITTED, VALIDATION_PASSED, VALIDATION_FAILED,
                                CHEAP_DONE, SLOW_DONE, CHEAP_PENDING, SLOW_DISCARDED)
                        .outputs(Arc.Out.and(CHEAP_TRIGGER, SLOW_TRIGGER))
                        .build())

                .transition(Transition.builder(T_RUN_CHEAP)
                        .inputs(Arc.In.one(CHEAP_TRIGGER))
                        .outputs(Arc.Out.place(CHEAP_DONE))
                        .build())

                .transition(Transition.builder(T_RUN_SLOW)
                        .inputs(Arc.In.one(SLOW_TRIGGER))
                        .inhibitor(COMMITTED)        // don't bother if cheap already won
                        .outputs(Arc.Out.place(SLOW_DONE))
                        .build())

                .transition(Transition.builder(T_VALIDATE)
                        .inputs(Arc.In.one(CHEAP_DONE))
                        .outputs(Arc.Out.xor(
                                Arc.Out.and(VALIDATION_PASSED, CHEAP_PENDING),
                                Arc.Out.and(VALIDATION_FAILED, CHEAP_PENDING)))
                        .build())

                .transition(Transition.builder(T_COMMIT_CHEAP)
                        .inputs(Arc.In.one(VALIDATION_PASSED), Arc.In.one(CHEAP_PENDING))
                        .inhibitor(COMMITTED)
                        .outputs(Arc.Out.and(AdkColours.EVENT_OUT, COMMITTED))
                        .priority(10)
                        .build())

                .transition(Transition.builder(T_COMMIT_SLOW)
                        .inputs(Arc.In.one(SLOW_DONE))
                        .read(VALIDATION_FAILED)
                        .inhibitor(COMMITTED)
                        .outputs(Arc.Out.and(AdkColours.EVENT_OUT, COMMITTED))
                        .priority(10)
                        .build())

                .transition(Transition.builder(T_DISCARD_SLOW)
                        .inputs(Arc.In.one(SLOW_DONE))
                        .read(COMMITTED)
                        .outputs(Arc.Out.place(SLOW_DISCARDED))
                        .priority(-10)
                        .build())

                .build();
    }

    // ============================================================
    //  Action bindings
    // ============================================================

    private static Map<String, TransitionAction> buildBindings(
            int cheapScore, int slowScore, int passThreshold,
            Duration cheapDelay, Duration slowDelay) {

        Predicate<BranchResult> validator = r -> r.score() >= passThreshold;

        Map<String, TransitionAction> m = new LinkedHashMap<>();
        m.put(T_START_BOTH, startBothAction());
        m.put(T_RUN_CHEAP, branchAction("cheap", cheapScore, cheapDelay,
                CHEAP_TRIGGER, CHEAP_DONE));
        m.put(T_RUN_SLOW,  branchAction("slow",  slowScore, slowDelay,
                SLOW_TRIGGER,  SLOW_DONE));
        m.put(T_VALIDATE,  validateAction(validator));
        m.put(T_COMMIT_CHEAP, commitCheapAction());
        m.put(T_COMMIT_SLOW,  commitSlowAction());
        m.put(T_DISCARD_SLOW, discardSlowAction());
        return m;
    }

    private static TransitionAction startBothAction() {
        return ctx -> {
            ctx.input(AdkColours.USER_IN);
            ctx.output(CHEAP_TRIGGER, (Void) null);
            ctx.output(SLOW_TRIGGER,  (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction branchAction(
            String id, int score, Duration delay,
            Place<Void> trigger, Place<BranchResult> done) {
        return ctx -> {
            ctx.input(trigger);
            return CompletableFuture.runAsync(() -> {
                try { Thread.sleep(delay.toMillis()); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); return; }
                ctx.output(done, new BranchResult(id, "answer from " + id, score));
            }, EXECUTOR);
        };
    }

    private static TransitionAction validateAction(Predicate<BranchResult> validator) {
        return ctx -> {
            BranchResult result = ctx.input(CHEAP_DONE);
            // The XOR branch chosen here is the only place that decides
            // pass-vs-fail. The two commit transitions are gated purely
            // on which marker place this produces to.
            if (validator.test(result)) {
                ctx.output(VALIDATION_PASSED, (Void) null);
            } else {
                ctx.output(VALIDATION_FAILED, (Void) null);
            }
            ctx.output(CHEAP_PENDING, result);   // stash for CommitCheap
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction commitCheapAction() {
        return ctx -> {
            ctx.input(VALIDATION_PASSED);
            BranchResult cheap = ctx.input(CHEAP_PENDING);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .invocationId("opt-cheap")
                    .author("opt_agent")
                    .content(Content.builder().role("model")
                            .parts(List.of(Part.fromText(cheap.text())))
                            .build())
                    .build());
            ctx.output(COMMITTED, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction commitSlowAction() {
        return ctx -> {
            BranchResult slow = ctx.input(SLOW_DONE);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .invocationId("opt-slow")
                    .author("opt_agent")
                    .content(Content.builder().role("model")
                            .parts(List.of(Part.fromText(slow.text())))
                            .build())
                    .build());
            ctx.output(COMMITTED, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction discardSlowAction() {
        return ctx -> {
            BranchResult late = ctx.input(SLOW_DONE);
            ctx.output(SLOW_DISCARDED, late);
            return CompletableFuture.completedFuture(null);
        };
    }

    // ============================================================
    //  ADK runner harness
    // ============================================================

    private static List<Event> runOneInvocation(PetriNet bound, String userText) {
        var registry = SessionExecutorRegistry.cleanerOwned();
        ConcurrentMap<SessionKey, Object> sessionOwners = new ConcurrentHashMap<>();
        var agent = PetriAgent.of(
                "opt_agent",
                "Optimistic commit with structural fallback",
                registry,
                key -> PetriRunner.builder(bound)
                        .environmentPlace(AdkColours.USER_IN)
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start(),
                ctx -> sessionOwners.computeIfAbsent(
                        SessionKey.from(ctx.session()), k -> new Object()));

        var runner = new InMemoryRunner(agent);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                .blockingGet();
        try {
            return runner.runAsync(
                            session.userId(), session.id(),
                            userMessage(userText),
                            RunConfig.builder().build())
                    .toList().blockingGet();
        } finally {
            registry.closeAll();
        }
    }

    private static Content userMessage(String text) {
        return Content.builder().role("user")
                .parts(List.of(Part.fromText(text))).build();
    }
}
