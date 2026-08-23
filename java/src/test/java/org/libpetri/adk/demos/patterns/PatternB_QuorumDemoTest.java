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
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;
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
 * Pattern B — late-join / K-of-N quorum.
 *
 * <p>Five branches dispatch concurrently and each deposits a token onto
 * a shared {@code RESULT} place. The synthesis transition fires the
 * instant the K-th token arrives (here K=3), driven by an
 * {@link Arc.In#exactly(int, Place)} input arc. Late results — the
 * 4th and 5th branches that finish after the quorum closed — drain to a
 * dedicated sink via a {@code read(QUORUM_MET)}-gated absorber.
 *
 * <h2>Why this exists</h2>
 * <p>{@code ParallelAgent}'s {@code Flowable.merge(...)} only completes
 * when <i>every</i> sub-agent finishes. There is no K-of-N agent type;
 * the user has to escape into Rx {@code .buffer(K)} or {@code .take(K)}
 * outside the agent tree, which loses per-sub-agent state isolation and
 * silently drops any late arrivals. See
 * {@code PatternB_AdkOnlyFoilTest} for the paired counter-example.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [USER_IN] --T_StartQuorum--> AND(TRIGGER_1..5)
 *                                reset(RESULT, QUORUM_MET, DISCARDED)
 *   [TRIGGER_i] --T_RunBranch_i--> [RESULT]
 *   [RESULT]x3   --T_Synthesize  --> AND(EVENT_OUT, QUORUM_MET)
 *                In.exactly(3, RESULT), inhibitor(QUORUM_MET), prio +10
 *   [RESULT]     --T_AbsorbLate  --> [DISCARDED]
 *                read(QUORUM_MET), prio -10
 * </pre>
 *
 * <h2>Structural properties</h2>
 * <ul>
 *   <li><b>K lives in the topology, not the action:</b> the input arc
 *       carries the K value via {@link Arc.In.Exactly} — visible to
 *       structural analysers and SMT verifiers.</li>
 *   <li><b>Synthesis is at-most-once per turn:</b>
 *       {@code PlaceBound(QUORUM_MET, 1)}.</li>
 *   <li><b>Output is exactly-once per turn:</b>
 *       {@code PlaceBound(EVENT_OUT, 1)}.</li>
 * </ul>
 */
class PatternB_QuorumDemoTest {

    record BranchResult(String branchId, String text) {}

    private static final int K = 3;

    private static final Place<Void> TRIGGER_1 = Place.of("trigger1", Void.class);
    private static final Place<Void> TRIGGER_2 = Place.of("trigger2", Void.class);
    private static final Place<Void> TRIGGER_3 = Place.of("trigger3", Void.class);
    private static final Place<Void> TRIGGER_4 = Place.of("trigger4", Void.class);
    private static final Place<Void> TRIGGER_5 = Place.of("trigger5", Void.class);
    private static final List<Place<Void>> TRIGGERS =
            List.of(TRIGGER_1, TRIGGER_2, TRIGGER_3, TRIGGER_4, TRIGGER_5);

    private static final Place<BranchResult> RESULT =
            Place.of("quorumResult", BranchResult.class);
    private static final Place<Void> QUORUM_MET = Place.of("quorumMet", Void.class);
    private static final Place<BranchResult> DISCARDED =
            Place.of("quorumDiscarded", BranchResult.class);

    private static final String T_START_QUORUM = "Quorum_Start";
    private static final String T_RUN_PREFIX   = "Quorum_RunBranch_";
    private static final String T_SYNTHESIZE   = "Quorum_Synthesize";
    private static final String T_ABSORB_LATE  = "Quorum_AbsorbLate";

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
    void synthesis_fires_at_kth_result_without_waiting_for_slower_branches() throws Exception {
        // Delays chosen so the first K=3 branches finish well before the
        // slower 4th and 5th. Synthesis should fire shortly after branch 3.
        var delays = List.of(
                Duration.ofMillis(30),
                Duration.ofMillis(60),
                Duration.ofMillis(90),
                Duration.ofMillis(400),
                Duration.ofMillis(800));

        var bound = buildNet().bindActions(buildBindings(delays));

        var registry = SessionExecutorRegistry.cleanerOwned();
        ConcurrentMap<SessionKey, Object> sessionOwners = new ConcurrentHashMap<>();
        var agent = PetriAgent.of(
                "quorum_agent",
                "K-of-N quorum across five branches",
                registry,
                key -> PetriRunner.builder(bound)
                        .environmentPlace(AdkColours.USER_IN)
                        .orchestratorExecutor(EXECUTOR)
                        .start(),
                ctx -> sessionOwners.computeIfAbsent(
                        SessionKey.from(ctx.session()), k -> new Object()));

        var runner = new InMemoryRunner(agent);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s")
                .blockingGet();

        long start = System.nanoTime();
        var events = runner.runAsync(
                        session.userId(), session.id(),
                        userMessage("synthesize a consensus answer"),
                        RunConfig.builder().build())
                .toList().blockingGet();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        // Exactly one synthesised event from the agent — the quorum fires
        // once and the late arrivals drain.
        var agentEvents = events.stream()
                .filter(e -> "quorum_agent".equals(e.author())).toList();
        assertThat(agentEvents).hasSize(1);

        // The synthesis content references the three winning branches.
        String text = agentEvents.get(0).content().get().text();
        // The quorum is the three FASTEST branches, in completion order.
        // contains("synth(") only proved synthesizeAction ran at all, which it
        // always does; it said nothing about which branches formed the quorum.
        assertThat(text).isEqualTo("synth(b1,b2,b3)");

        // The load-bearing assertion: the agent completed well before the
        // slowest branch's delay (400ms). With a barrier-join (e.g.
        // ParallelAgent in the foil test), this elapsed would be >= 800ms.
        // We leave a generous margin to keep CI stable.
        assertThat(elapsedMs).isLessThan(400L);

        registry.closeAll();
    }

    @Test
    @EnabledIf("z3Available")
    void quorum_net_proves_exactly_one_synthesis_per_turn() {
        // CORE-043 (libpetri 2.14+): a transition declaring an output spec
        // must carry a producing action at verification as well as at
        // execution. Verify the bound net, the one that actually runs,
        // rather than an unbound skeleton that could never fire. The
        // actions are never invoked here; only the structure is encoded.
        var net = buildNet().bindActions(buildBindings(List.of(
                Duration.ofMillis(30),
                Duration.ofMillis(60),
                Duration.ofMillis(90),
                Duration.ofMillis(400),
                Duration.ofMillis(800))));
        var result = SmtVerifier.forNet(net)
                .initialMarking(b -> b.tokens(AdkColours.USER_IN, 1))
                .sinkPlaces(
                        AdkColours.EVENT_OUT,
                        QUORUM_MET,
                        DISCARDED,
                        // RESULT can hold up to N-K leftover tokens until
                        // the absorber drains them; declaring it as a sink
                        // lets the deadlock-free check ignore the natural
                        // post-quorum tail.
                        RESULT)
                .property(SmtProperty.placeBound(QUORUM_MET, 1))
                .property(SmtProperty.placeBound(AdkColours.EVENT_OUT, 1))
                .property(SmtProperty.deadlockFree())
                .verify();
        // libpetri 3.0.1 discharges an IC3 certificate before returning
        // Proven and replays every counterexample, so a verdict that cannot
        // be re-validated comes back Unknown. Assert the strong form: this
        // project claims a proof here, and isViolated()==false alone would
        // also pass on Unknown, letting the claim rot silently.
        assertThat(result.isProven()).isTrue();
        assertThat(result.isViolated()).isFalse();
    }

    // ============================================================
    //  Net construction
    // ============================================================

    private static PetriNet buildNet() {
        var builder = PetriNet.builder("kofn-quorum")
                .place(AdkColours.USER_IN)
                .place(AdkColours.EVENT_OUT)
                .place(RESULT)
                .place(QUORUM_MET)
                .place(DISCARDED);
        for (var trigger : TRIGGERS) {
            builder.place(trigger);
        }

        // Start: fan out to N triggers, wipe per-turn state.
        builder.transition(Transition.builder(T_START_QUORUM)
                .inputs(Arc.In.one(AdkColours.USER_IN))
                .resets(RESULT, QUORUM_MET, DISCARDED)
                .outputs(Arc.Out.and(TRIGGERS.toArray(new Place<?>[0])))
                .build());

        // Per-branch run transitions: trigger -> RESULT.
        for (int i = 0; i < TRIGGERS.size(); i++) {
            builder.transition(Transition.builder(T_RUN_PREFIX + (i + 1))
                    .inputs(Arc.In.one(TRIGGERS.get(i)))
                    .outputs(Arc.Out.place(RESULT))
                    .build());
        }

        // The load-bearing transition: synthesis fires the moment K
        // tokens accumulate on RESULT — K lives in the input arc, not in
        // an action guard. Inhibitor on QUORUM_MET makes it at-most-once.
        builder.transition(Transition.builder(T_SYNTHESIZE)
                .inputs(Arc.In.exactly(K, RESULT))
                .inhibitor(QUORUM_MET)
                .outputs(Arc.Out.and(AdkColours.EVENT_OUT, QUORUM_MET))
                .priority(10)
                .build());

        // Late absorber: drains the remaining RESULT tokens after quorum.
        builder.transition(Transition.builder(T_ABSORB_LATE)
                .inputs(Arc.In.one(RESULT))
                .read(QUORUM_MET)
                .outputs(Arc.Out.place(DISCARDED))
                .priority(-10)
                .build());

        return builder.build();
    }

    // ============================================================
    //  Action bindings
    // ============================================================

    private static Map<String, TransitionAction> buildBindings(List<Duration> delays) {
        Map<String, TransitionAction> m = new LinkedHashMap<>();
        m.put(T_START_QUORUM, startQuorumAction());
        for (int i = 0; i < TRIGGERS.size(); i++) {
            int idx = i;
            m.put(T_RUN_PREFIX + (i + 1),
                    branchAction("b" + (idx + 1), delays.get(idx), TRIGGERS.get(idx)));
        }
        m.put(T_SYNTHESIZE,  synthesizeAction());
        m.put(T_ABSORB_LATE, absorbLateAction());
        return m;
    }

    private static TransitionAction startQuorumAction() {
        return ctx -> {
            ctx.input(AdkColours.USER_IN);
            for (var trigger : TRIGGERS) {
                ctx.output(trigger, (Void) null);
            }
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction branchAction(
            String branchId, Duration delay, Place<Void> trigger) {
        return ctx -> {
            ctx.input(trigger);
            return CompletableFuture.runAsync(() -> {
                try { Thread.sleep(delay.toMillis()); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); return; }
                ctx.output(RESULT, new BranchResult(branchId, "answer from " + branchId));
            }, EXECUTOR);
        };
    }

    private static TransitionAction synthesizeAction() {
        return ctx -> {
            // ctx.inputs(...) returns the K BranchResult tokens consumed
            // from RESULT. The K value is enforced by the input arc, not
            // here — this action just shapes them into an Event.
            List<BranchResult> winners = ctx.inputs(RESULT);
            String summary = winners.stream()
                    .map(BranchResult::branchId)
                    .collect(Collectors.joining(",", "synth(", ")"));
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .invocationId("quorum-" + System.nanoTime())
                    .author("quorum_agent")
                    .content(Content.builder().role("model")
                            .parts(List.of(Part.fromText(summary)))
                            .build())
                    .build());
            ctx.output(QUORUM_MET, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction absorbLateAction() {
        return ctx -> {
            BranchResult late = ctx.input(RESULT);
            ctx.output(DISCARDED, late);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static Content userMessage(String text) {
        return Content.builder().role("user")
                .parts(List.of(Part.fromText(text))).build();
    }
}
