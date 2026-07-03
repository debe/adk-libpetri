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
import org.libpetri.smt.SmtProperty;
import org.libpetri.smt.SmtVerifier;

/**
 * Pattern A — speculative race with structural cancellation.
 *
 * <p>Three branches dispatch concurrently on one {@code USER_IN}. The first
 * one to deposit a result token commits to {@code EVENT_OUT}; the act of
 * committing produces a token on {@code RACE_WON}, which structurally
 * cancels (via inhibitor arc) the still-pending commits of the losers.
 * Late results drain to a sink — they cannot commit.
 *
 * <h2>Why this exists</h2>
 * <p>Stock ADK {@code ParallelAgent} is a barrier-join: it waits for every
 * sub-agent to emit before completing. There is no "first-wins" agent type
 * and no path to structural cancellation — the only way to express
 * speculative racing in plain ADK is to escape the agent tree entirely
 * with an {@code Flowable.merge(...).firstElement()} inside a custom
 * {@code BaseAgent}. See {@code PatternA_AdkOnlyFoilTest} for the paired
 * counter-example.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [USER_IN] --T_StartRace--> AND(TriggerA, TriggerB, TriggerC)
 *                              reset(RACE_WON, BRANCH_*_DONE)
 *
 *   [TriggerX] --T_RunBranchX--> [BRANCH_X_DONE]       (inhibitor: RACE_WON)
 *   [BRANCH_X_DONE] --T_CommitX--> AND(EVENT_OUT, RACE_WON)  (inhibitor: RACE_WON, prio +10)
 *   [BRANCH_X_DONE] --T_DiscardX--> [RACE_DISCARDED]   (read: RACE_WON, prio -10)
 * </pre>
 *
 * <h2>Structural properties</h2>
 * <ul>
 *   <li><b>At-most-once commit per turn:</b> {@code PlaceBound(RACE_WON, 1)}
 *       — once any commit fires, every other commit is inhibited
 *       permanently for the turn.</li>
 *   <li><b>Bounded output:</b> {@code PlaceBound(EVENT_OUT, 1)} per turn —
 *       follows from the above plus the topology that only commit
 *       transitions write to {@code EVENT_OUT}.</li>
 *   <li><b>Losers are reachable as discard, not commit:</b> the discard
 *       transitions become enabled iff {@code RACE_WON} is present, so
 *       in-flight results from losing branches drain safely.</li>
 * </ul>
 *
 * <p>All three properties are topology-level — they hold regardless of
 * how the branch actions are implemented.
 */
class PatternA_SpeculativeRaceDemoTest {

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
        try { new Context().close(); return true; }
        catch (UnsatisfiedLinkError | NoClassDefFoundError _) { return false; }
    }

    @Test
    void fastest_branch_commits_first_and_losers_are_structurally_cancelled() throws Exception {
        var net = buildNet();
        var bound = net.bindActions(buildBindings(
                Duration.ofMillis(20),   // fast
                Duration.ofMillis(120),  // medium
                Duration.ofMillis(300)));// slow

        var registry = SessionExecutorRegistry.cleanerOwned();
        ConcurrentMap<SessionKey, Object> sessionOwners = new ConcurrentHashMap<>();
        var agent = PetriAgent.of(
                "race_agent",
                "Speculative race across three branches",
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

        registry.closeAll();
    }

    @Test
    @EnabledIf("z3Available")
    void race_net_proves_at_most_one_commit_per_turn() {
        var net = buildNet();
        var result = SmtVerifier.forNet(net)
                .initialMarking(b -> b.tokens(AdkColours.USER_IN, 1))
                .sinkPlaces(
                        AdkColours.EVENT_OUT,
                        RACE_WON,
                        RACE_DISCARDED)
                // RACE_WON acts as the structural mutex: at most one commit
                // ever produces a token there, and the inhibitor on every
                // other commit guarantees mutual exclusion.
                .property(SmtProperty.placeBound(RACE_WON, 1))
                .property(SmtProperty.placeBound(AdkColours.EVENT_OUT, 1))
                .property(SmtProperty.deadlockFree())
                .verify();
        assertThat(result.isViolated()).isFalse();
    }

    // ============================================================
    //  Net construction
    // ============================================================

    private static PetriNet buildNet() {
        return PetriNet.builder("speculative-race")
                .place(AdkColours.USER_IN)
                .place(AdkColours.EVENT_OUT)
                .place(TRIGGER_A).place(TRIGGER_B).place(TRIGGER_C)
                .place(BRANCH_A_DONE).place(BRANCH_B_DONE).place(BRANCH_C_DONE)
                .place(RACE_WON)
                .place(RACE_DISCARDED)
                // Start: consume USER_IN, wipe per-turn state, fan out to 3 triggers.
                // Reset arcs on RACE_WON and the per-branch done-places clear any
                // stale tokens from a prior turn before the new race begins.
                .transition(Transition.builder(T_START_RACE)
                        .inputs(Arc.In.one(AdkColours.USER_IN))
                        .resets(RACE_WON, BRANCH_A_DONE, BRANCH_B_DONE, BRANCH_C_DONE,
                                RACE_DISCARDED)
                        .outputs(Arc.Out.and(TRIGGER_A, TRIGGER_B, TRIGGER_C))
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
                .inputs(Arc.In.one(done))
                .inhibitor(RACE_WON)                  // first to fire wins; rest inhibited
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
