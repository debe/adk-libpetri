package org.libpetri.adk.demos.patterns;

import static com.google.common.truth.Truth.assertThat;
import static com.google.common.truth.Truth.assertWithMessage;

import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.runner.InMemoryRunner;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.analysis.EnvironmentAnalysisMode;
import org.libpetri.core.Arc;
import org.libpetri.core.EnvironmentPlace;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.runner.PetriAgent;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.adk.runner.SessionExecutorRegistry;
import org.libpetri.adk.subnet.SubnetActions;
import org.libpetri.adk.verify.SmtProofs;
import org.libpetri.runtime.BitmapNetExecutor;
import org.libpetri.smt.SmtProperty;
import org.libpetri.smt.SmtVerifier;

/**
 * Pattern A — speculative race with structural cancellation.
 *
 * <p>Three branches dispatch concurrently on one {@code USER_IN}. The first
 * one to deposit a result token commits to {@code EVENT_OUT}. Committing
 * consumes the turn's single {@code RACE_PERMIT} token, so no other commit
 * can fire, and produces a token on {@code RACE_WON}, which opens the
 * losers' discard path. The net does not dispose the losers: their actions
 * run to completion and their results drain to {@code RACE_DISCARDED}. They
 * cannot commit.
 *
 * <h2>Why this exists</h2>
 * <p>Stock ADK {@code ParallelAgent.runAsyncImpl} is
 * {@code Flowable.merge(branches).takeUntil(escalate)} (ADK 1.10.1): the
 * first branch to escalate ends the merge and disposes the rest. That is
 * first-escalation-wins. It does not give a preference order, a K-of-N
 * commit without a check-and-act counter in {@code session.state}, or a
 * provable at-most-once commit that composes with a turn permit and abort.
 * A custom {@code BaseAgent} with {@code Flowable.merge(...).firstElement()}
 * gets first-wins and disposes the losers; see
 * {@code PatternA_AdkOnlyFoilTest} for that paired counter-example.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [USER_IN] --T_StartRace--> AND(TriggerA, TriggerB, TriggerC, RACE_PERMIT)
 *                              reset(RACE_PERMIT, RACE_WON, Trigger*,
 *                                    BRANCH_*_DONE, RACE_DISCARDED)
 *
 *   [TriggerX] --T_RunBranchX--> [BRANCH_X_DONE]       (inhibitor: RACE_WON)
 *   [BRANCH_X_DONE] + [RACE_PERMIT] --T_CommitX--> AND(EVENT_OUT, RACE_WON)  (prio +10)
 *   [BRANCH_X_DONE] --T_DiscardX--> [RACE_DISCARDED]   (read: RACE_WON, prio -10)
 * </pre>
 *
 * <h2>Why a permit, not an inhibitor on RACE_WON</h2>
 * <p>The obvious encoding guards every commit with {@code inhibitor(RACE_WON)}.
 * It does not exclude anything on a real executor. Within one orchestrator
 * pass an inhibitor reads the marking as of the start of the pass, and a
 * commit's {@code RACE_WON} deposit lands only at the end of it (libpetri
 * EXEC-003 AC5). Two branch results that are ready in the same pass therefore
 * both commit: {@code RACE_WON} and {@code EVENT_OUT} each reach 2. libpetri
 * 8.0's verifier sees the same race through its in-flight split (VER-004), so
 * the bounds below were Violated on that encoding.
 * A consumed permit has no such window: the first commit takes the only
 * token, and the second is not enabled at all. The regression test
 * {@link #two_results_ready_in_one_pass_commit_exactly_once()} replays that
 * marking on {@code BitmapNetExecutor}.
 *
 * <p>{@code T_StartRace} resets every per-turn place, triggers included, so a
 * long-lived session does not accumulate the triggers that cancelled losers
 * leave behind, and a stale permit cannot carry over into the next turn.
 *
 * <h2>Structural properties</h2>
 * <ul>
 *   <li><b>At-most-once commit per turn:</b> {@code PlaceBound(RACE_WON, 1)}
 *       — the turn's single permit is consumed by whichever commit fires
 *       first, so no other commit is ever enabled in that turn.</li>
 *   <li><b>Bounded output:</b> {@code PlaceBound(EVENT_OUT, 1)} per turn —
 *       follows from the above plus the topology that only commit
 *       transitions write to {@code EVENT_OUT}.</li>
 *   <li><b>Losers are reachable as discard, not commit:</b> the discard
 *       transitions become enabled iff {@code RACE_WON} is present, so
 *       in-flight results from losing branches drain safely.</li>
 * </ul>
 *
 * <p>All three properties are topology-level — they hold regardless of
 * how the branch actions are implemented. Each is proved on its own, without
 * {@code assumeAtomicFiring}.
 */
public class PatternA_SpeculativeRaceDemoTest {

    /** Test-local typed colour for branch results. */
    record BranchResult(String branchId, String text) {}

    // ============================================================
    //  Places (all test-local — not part of AdkColours)
    // ============================================================
    private static final Place<Void> TRIGGER_A = Place.of("triggerA", Void.class);
    private static final Place<Void> TRIGGER_B = Place.of("triggerB", Void.class);
    private static final Place<Void> TRIGGER_C = Place.of("triggerC", Void.class);
    private static final Place<BranchResult> BRANCH_A_DONE =
            Place.of("branchADone", BranchResult.class);
    private static final Place<BranchResult> BRANCH_B_DONE =
            Place.of("branchBDone", BranchResult.class);
    private static final Place<BranchResult> BRANCH_C_DONE =
            Place.of("branchCDone", BranchResult.class);
    private static final Place<Void> RACE_PERMIT = Place.of("racePermit", Void.class);
    private static final Place<Void> RACE_WON = Place.of("raceWon", Void.class);
    private static final Place<BranchResult> RACE_DISCARDED =
            Place.of("raceDiscarded", BranchResult.class);

    // ============================================================
    //  Transition names
    // ============================================================
    private static final String T_START_RACE = "Race_Start";
    private static final String T_RUN_A      = "Race_RunBranchA";
    private static final String T_RUN_B      = "Race_RunBranchB";
    private static final String T_RUN_C      = "Race_RunBranchC";
    private static final String T_COMMIT_A   = "Race_CommitA";
    private static final String T_COMMIT_B   = "Race_CommitB";
    private static final String T_COMMIT_C   = "Race_CommitC";
    private static final String T_DISCARD_A  = "Race_DiscardA";
    private static final String T_DISCARD_B  = "Race_DiscardB";
    private static final String T_DISCARD_C  = "Race_DiscardC";

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
        return SmtVerifier.z3Available();
    }

    @Test
    void fastest_branch_commits_first_and_losers_are_structurally_cancelled() throws Exception {
        var net = buildNet();
        var bound = SubnetActions.bindComposed(net, buildBindings(
                Duration.ofMillis(20),   // fast
                Duration.ofMillis(120),  // medium
                Duration.ofMillis(300)));// slow

        var registry = SessionExecutorRegistry.strongOwned();
        var agent = PetriAgent.builder("race_agent", registry,
                key -> PetriRunner.builder(bound)
                        .environmentPlace(AdkColours.USER_IN)
                        .orchestratorExecutor(EXECUTOR)
                        .start())
                .description("Speculative race across three branches")
                .build();

        try {
            var runner = new InMemoryRunner(agent);
            var session = runner.sessionService()
                    .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                    .blockingGet();

            var events = runner.runAsync(
                            session.userId(), session.id(),
                            userMessage("which branch wins?"),
                            RunConfig.builder().build())
                    .toList().blockingGet();

            // The agent emits exactly one Event — the fast branch's result.
            // The losers' tokens, if any landed, drained via T_DiscardX.
            var agentEvents = events.stream()
                    .filter(e -> "race_agent".equals(e.author())).toList();
            assertThat(agentEvents).hasSize(1);
            assertThat(agentEvents.get(0).content().get().text()).contains("fast");
        } finally {
            registry.closeAll();
        }
    }

    /**
     * Replays the marking that made the inhibitor encoding commit twice on
     * the real executor: every branch result is ready in the same pass. The
     * permit admits exactly one commit; the other results drain to discard.
     */
    @Test
    void two_results_ready_in_one_pass_commit_exactly_once() {
        var net = SubnetActions.bindComposed(buildNet(), buildBindings(
                Duration.ofMillis(20), Duration.ofMillis(120), Duration.ofMillis(300)));
        Map<Place<?>, List<Token<?>>> initial = Map.of(
                BRANCH_A_DONE, List.of(Token.of(new BranchResult("fast", "answer from fast"))),
                BRANCH_B_DONE, List.of(Token.of(new BranchResult("medium", "answer from medium"))),
                BRANCH_C_DONE, List.of(Token.of(new BranchResult("slow", "answer from slow"))),
                RACE_PERMIT, List.of(Token.of((Void) null)));

        var marking = BitmapNetExecutor.builder(net, initial).build().run();

        assertWithMessage("RACE_WON").that(marking.peekTokens(RACE_WON)).hasSize(1);
        assertWithMessage("EVENT_OUT").that(marking.peekTokens(AdkColours.EVENT_OUT)).hasSize(1);
        assertWithMessage("RACE_DISCARDED").that(marking.peekTokens(RACE_DISCARDED)).hasSize(2);
        assertWithMessage("RACE_PERMIT").that(marking.peekTokens(RACE_PERMIT)).isEmpty();
    }

    @Test
    @EnabledIf("z3Available")
    void race_net_proves_at_most_one_commit_per_turn() {
        // No assumeAtomicFiring: libpetri 8.0 splits every commit into start
        // and completion (VER-004), which is exactly the window the inhibitor
        // encoding lost. These hold with the split because the permit is
        // consumed at start.
        SmtProofs.assertEachProven(boundNet(),
                v -> v.initialMarking(b -> b.tokens(AdkColours.USER_IN, 1))
                        .sinkPlaces(AdkColours.EVENT_OUT, RACE_WON, RACE_DISCARDED)
                        // Strict deadlock-freedom (libpetri 5.0+) reads a resting
                        // token on a non-sink place as a stranding. Losing branches
                        // that never started keep their trigger: the inhibitor on
                        // RACE_WON is the structural cancellation. Excuse those
                        // triggers only once the race is won, so a trigger stranded
                        // without a winner would still be reported.
                        .sinkPlacesWhen(RACE_WON, TRIGGER_A, TRIGGER_B, TRIGGER_C),
                Map.of(
                        "one commit per turn: placeBound(RACE_WON, 1)",
                        SmtProperty.placeBound(RACE_WON, 1),
                        "one egress event per turn: placeBound(EVENT_OUT, 1)",
                        SmtProperty.placeBound(AdkColours.EVENT_OUT, 1),
                        "deadlockFree",
                        SmtProperty.deadlockFree()));
    }

    /**
     * A second user turn must not stack a second permit on the first:
     * {@code T_StartRace} resets the place before it seeds.
     *
     * <p>{@code assumeAtomicFiring(true)} is exact here. Without it the only
     * counterexample is {@code Race_Start} starting again while its earlier
     * firing is in flight, which libpetri's report flags (CONC-002) as
     * impossible on the Java executor.
     *
     * <p>This is the only bound claimed across turns. {@code RACE_WON} and
     * {@code EVENT_OUT} are per-turn bounds: a turn that starts while the
     * previous turn's commit is still in flight sees that commit's
     * {@code RACE_WON} land after its reset, and the demo does not correlate
     * branch results with the turn that started them.
     */
    @Test
    @EnabledIf("z3Available")
    void race_permit_never_stacks_across_two_turns() {
        SmtProofs.assertEachProven(boundNet(),
                v -> v.environmentPlaces(EnvironmentPlace.of(AdkColours.USER_IN))
                        .environmentMode(EnvironmentAnalysisMode.arrivals(2, 2))
                        .assumeAtomicFiring(true),
                Map.of("permit never stacks: placeBound(RACE_PERMIT, 1)",
                        SmtProperty.placeBound(RACE_PERMIT, 1)));
    }

    /**
     * CORE-043 (libpetri 2.14+): a transition declaring an output spec must
     * carry a producing action at verification as well as at execution.
     * Verify the bound net, the one that actually runs, rather than an
     * unbound skeleton that could never fire. The actions are never invoked
     * by the verifier; only the structure is encoded.
     */
    private static PetriNet boundNet() {
        return SubnetActions.bindComposed(buildNet(), buildBindings(
                Duration.ofMillis(20),
                Duration.ofMillis(120),
                Duration.ofMillis(300)));
    }

    // ============================================================
    //  Net construction
    // ============================================================

    public static PetriNet buildNet() {
        return PetriNet.builder("speculative-race")
                .place(AdkColours.USER_IN)
                .place(AdkColours.EVENT_OUT)
                .place(TRIGGER_A).place(TRIGGER_B).place(TRIGGER_C)
                .place(BRANCH_A_DONE).place(BRANCH_B_DONE).place(BRANCH_C_DONE)
                .place(RACE_PERMIT)
                .place(RACE_WON)
                .place(RACE_DISCARDED)
                // Start: consume USER_IN, wipe per-turn state, fan out to 3 triggers
                // and seed the turn's single commit permit. The reset arcs clear
                // whatever a prior turn left behind before the new race begins:
                // its winner marker, the triggers of branches it cancelled, any
                // undrained results and an unspent permit.
                .transition(Transition.builder(T_START_RACE)
                        .inputs(Arc.In.one(AdkColours.USER_IN))
                        .resets(RACE_PERMIT, RACE_WON, TRIGGER_A, TRIGGER_B, TRIGGER_C,
                                BRANCH_A_DONE, BRANCH_B_DONE, BRANCH_C_DONE, RACE_DISCARDED)
                        .outputs(Arc.Out.and(TRIGGER_A, TRIGGER_B, TRIGGER_C, RACE_PERMIT))
                        .build())
                .transition(runBranch(T_RUN_A, TRIGGER_A, BRANCH_A_DONE))
                .transition(runBranch(T_RUN_B, TRIGGER_B, BRANCH_B_DONE))
                .transition(runBranch(T_RUN_C, TRIGGER_C, BRANCH_C_DONE))
                .transition(commit(T_COMMIT_A, BRANCH_A_DONE))
                .transition(commit(T_COMMIT_B, BRANCH_B_DONE))
                .transition(commit(T_COMMIT_C, BRANCH_C_DONE))
                .transition(discard(T_DISCARD_A, BRANCH_A_DONE))
                .transition(discard(T_DISCARD_B, BRANCH_B_DONE))
                .transition(discard(T_DISCARD_C, BRANCH_C_DONE))
                .build();
    }

    private static Transition runBranch(String name, Place<Void> trigger, Place<BranchResult> done) {
        return Transition.builder(name)
                .inputs(Arc.In.one(trigger))
                .inhibitor(RACE_WON)                  // don't even start if race already decided
                .outputs(Arc.Out.place(done))
                .build();
    }

    private static Transition commit(String name, Place<BranchResult> done) {
        return Transition.builder(name)
                // Consuming the turn's only permit is the exclusion. An
                // inhibitor on RACE_WON is not: it reads the pass-start marking.
                .inputs(Arc.In.one(done), Arc.In.one(RACE_PERMIT))
                .outputs(Arc.Out.and(AdkColours.EVENT_OUT, RACE_WON))
                .priority(10)                         // beat discard if both enabled
                .build();
    }

    private static Transition discard(String name, Place<BranchResult> done) {
        return Transition.builder(name)
                .inputs(Arc.In.one(done))
                .read(RACE_WON)                       // only drain once race is over
                .outputs(Arc.Out.place(RACE_DISCARDED))
                .priority(-10)
                .build();
    }

    // ============================================================
    //  Action bindings (closures over per-branch identity)
    // ============================================================

    private static Map<String, TransitionAction> buildBindings(
            Duration delayA, Duration delayB, Duration delayC) {
        Map<String, TransitionAction> m = new LinkedHashMap<>();
        m.put(T_START_RACE, startRaceAction());
        m.put(T_RUN_A, branchAction("fast",   delayA, TRIGGER_A, BRANCH_A_DONE));
        m.put(T_RUN_B, branchAction("medium", delayB, TRIGGER_B, BRANCH_B_DONE));
        m.put(T_RUN_C, branchAction("slow",   delayC, TRIGGER_C, BRANCH_C_DONE));
        m.put(T_COMMIT_A, commitAction(BRANCH_A_DONE));
        m.put(T_COMMIT_B, commitAction(BRANCH_B_DONE));
        m.put(T_COMMIT_C, commitAction(BRANCH_C_DONE));
        m.put(T_DISCARD_A, discardAction(BRANCH_A_DONE));
        m.put(T_DISCARD_B, discardAction(BRANCH_B_DONE));
        m.put(T_DISCARD_C, discardAction(BRANCH_C_DONE));
        return m;
    }

    private static TransitionAction startRaceAction() {
        return ctx -> {
            ctx.input(AdkColours.USER_IN);
            ctx.output(TRIGGER_A, (Void) null);
            ctx.output(TRIGGER_B, (Void) null);
            ctx.output(TRIGGER_C, (Void) null);
            ctx.output(RACE_PERMIT, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction branchAction(
            String branchId, Duration delay,
            Place<Void> trigger, Place<BranchResult> done) {
        return ctx -> {
            ctx.input(trigger);
            return CompletableFuture.runAsync(() -> {
                try {
                    Thread.sleep(delay.toMillis());
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
                ctx.output(done, new BranchResult(branchId, "answer from " + branchId));
            }, EXECUTOR);
        };
    }

    private static TransitionAction commitAction(Place<BranchResult> done) {
        return ctx -> {
            BranchResult result = ctx.input(done);
            ctx.input(RACE_PERMIT);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .invocationId("race-" + result.branchId())
                    .author("race_agent")
                    .content(Content.builder().role("model")
                            .parts(List.of(Part.fromText(result.text())))
                            .build())
                    .build());
            ctx.output(RACE_WON, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction discardAction(Place<BranchResult> done) {
        return ctx -> {
            BranchResult late = ctx.input(done);
            ctx.output(RACE_DISCARDED, late);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static Content userMessage(String text) {
        return Content.builder().role("user")
                .parts(List.of(Part.fromText(text))).build();
    }
}
