package org.libpetri.adk.demos.voice;

import static com.google.common.truth.Truth.assertThat;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import io.reactivex.rxjava3.core.Flowable;
import org.libpetri.core.EnvironmentPlace;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.analysis.MarkingState;
import org.libpetri.analysis.StateClassGraph;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmStreamingStepSubnet;
import org.libpetri.adk.ManualClock;
import org.libpetri.adk.subnet.SubnetActions;
import org.libpetri.runtime.BitmapNetExecutor;
import org.libpetri.runtime.Marking;
import org.libpetri.runtime.PetriNetExecutor;

class LiveApiRecoverySubnetTest {

    private static final LiveApiRecoverySubnet.Config FAST =
            new LiveApiRecoverySubnet.Config(Duration.ofMillis(80), Duration.ofMillis(80));

    // Timing tests run on a ManualClock: each step is settled before time
    // moves, so a timer fires at an exact logical instant and the scenarios
    // assert boundaries (79 ms vs 80 ms) that real sleeps could only race.

    @Test
    void silent_model_triggers_nudge_then_reconnect() throws Exception {
        var fixture = drive(FAST, (executor, clock) -> {
            // Caller signals "model should be responding but isn't".
            clock.settle(() -> inject(executor, LiveApiRecoverySubnet.Places.RESPONSE_AWAITED));

            clock.advanceAndSettle(Duration.ofMillis(79));
            assertThat(marked(executor, LiveApiRecoverySubnet.Places.NUDGE_NEEDED)).isFalse();
            clock.advanceAndSettle(Duration.ofMillis(1));
            assertThat(marked(executor, LiveApiRecoverySubnet.Places.NUDGE_NEEDED)).isTrue();

            // The reconnect window starts when the nudge fires, not before.
            clock.advanceAndSettle(Duration.ofMillis(79));
            assertThat(marked(executor, LiveApiRecoverySubnet.Places.RECONNECT_NEEDED)).isFalse();
            clock.advanceAndSettle(Duration.ofMillis(1));
        });

        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.NUDGE_NEEDED)).hasSize(1);
        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.RECONNECT_NEEDED)).hasSize(1);
    }

    @Test
    void model_active_inhibits_nudge() throws Exception {
        var fixture = drive(FAST, (executor, clock) -> {
            // Model is actively responding — MODEL_ACTIVE lands first.
            clock.settle(() -> inject(executor, LiveApiRecoverySubnet.Places.MODEL_ACTIVE));
            clock.settle(() -> inject(executor, LiveApiRecoverySubnet.Places.RESPONSE_AWAITED));
            clock.advanceAndSettle(Duration.ofSeconds(10));
        });

        // Neither nudge nor reconnect fired — model presence inhibited both.
        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.NUDGE_NEEDED)).isEmpty();
        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.RECONNECT_NEEDED)).isEmpty();
    }

    @Test
    void model_active_resumes_mid_window_blocks_both_recovery_stages() throws Exception {
        // RESPONSE_AWAITED injected, then 40ms later (well before the 80ms
        // nudge deadline) MODEL_ACTIVE arrives — the inhibitor atomically
        // blocks both Nudge and Reconnect.
        var fixture = drive(FAST, (executor, clock) -> {
            clock.settle(() -> inject(executor, LiveApiRecoverySubnet.Places.RESPONSE_AWAITED));
            clock.advanceAndSettle(Duration.ofMillis(40));
            clock.settle(() -> inject(executor, LiveApiRecoverySubnet.Places.MODEL_ACTIVE));
            clock.advanceAndSettle(Duration.ofSeconds(10));
        });

        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.NUDGE_NEEDED)).isEmpty();
        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.RECONNECT_NEEDED)).isEmpty();
    }

    // ============================================================
    //  Composed BIDI voice-net structural check — the three voice
    //  subnets (streaming + barge-in + recovery) together must yield
    //  a bounded reachable state space. SCG construction terminates
    //  under a finite bound = topology is bounded.
    // ============================================================

    @Test
    void composed_bidi_voice_net_scg_terminates_under_bound() {
        var recoveryDef = LiveApiRecoverySubnet.def(FAST);

        // CORE-043 (libpetri 2.14+): a transition declaring an output spec
        // must carry a producing action at analysis time as well as at
        // execution. Bind all three subnets so the state-class graph is
        // built over the net that runs. The actions are never invoked here;
        // only the structure is explored.
        var streamConfig = LlmStreamingStepSubnet.Config.builder("scg")
                .executorRef(new AtomicReference<PetriNetExecutor>())
                .build();
        var net = SubnetActions.bindComposed(
                PetriNet.builder("voice-scg-check")
                        .compose(LlmStreamingStepSubnet.DEF)
                        .compose(BargeInSubnet.DEF)
                        .compose(recoveryDef)
                        .build(),
                LlmStreamingStepSubnet.actionBindings(scgStubLlm(), streamConfig),
                BargeInSubnet.actionBindings(),
                LiveApiRecoverySubnet.actionBindings(FAST));

        var initial = MarkingState.builder()
                .tokens(AdkColours.LLM_REQUEST, 1)
                .build();

        var scg = StateClassGraph.build(net, initial, 256);

        assertThat(scg.size()).isGreaterThan(0);
        // isComplete(), not size() <= 256. The cap is build()'s own argument, so
        // asserting it back is unfalsifiable: an UNBOUNDED net truncates at
        // exactly 256 and the assertion still passes, while the README claims
        // this confirms a finite reachable state space. isComplete() is false
        // precisely when exploration was truncated, so it is the real predicate.
        assertThat(scg.isComplete()).isTrue();
        assertThat(scg.size()).isAtMost(256);
    }

    @Test
    void model_active_appears_between_nudge_and_reconnect_stops_recovery() throws Exception {
        var fixture = drive(FAST, (executor, clock) -> {
            clock.settle(() -> inject(executor, LiveApiRecoverySubnet.Places.RESPONSE_AWAITED));
            clock.advanceAndSettle(Duration.ofMillis(80));
            assertThat(marked(executor, LiveApiRecoverySubnet.Places.NUDGE_NEEDED)).isTrue();
            // The model responds 79ms into the 80ms reconnect window. On a real
            // clock this test had to poll for the nudge and then bet the
            // inject would beat a deadline landing at about the same moment.
            clock.advanceAndSettle(Duration.ofMillis(79));
            clock.settle(() -> inject(executor, LiveApiRecoverySubnet.Places.MODEL_ACTIVE));
            clock.advanceAndSettle(Duration.ofSeconds(10));
        });

        // Nudge fired (the model went silent for >= 80ms before responding),
        // but Reconnect did NOT (model became active before the 80ms post-nudge window).
        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.NUDGE_NEEDED)).hasSize(1);
        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.RECONNECT_NEEDED)).isEmpty();
    }

    // ============================================================
    //  Shape
    // ============================================================

    @Test
    void config_rejects_zero_or_negative_durations() {
        Assertions.assertThrows(
                IllegalArgumentException.class,
                () -> new LiveApiRecoverySubnet.Config(Duration.ZERO, Duration.ofSeconds(1)));
        Assertions.assertThrows(
                IllegalArgumentException.class,
                () -> new LiveApiRecoverySubnet.Config(Duration.ofSeconds(1), Duration.ofMillis(-1)));
    }

    @Test
    void def_declares_two_transitions_and_four_ports() {
        var def = LiveApiRecoverySubnet.def(LiveApiRecoverySubnet.Config.defaults());
        var transitions = def.body().transitions().stream()
                .map(t -> t.name()).sorted().toList();
        assertThat(transitions).containsExactly(
                LiveApiRecoverySubnet.Transitions.NUDGE,
                LiveApiRecoverySubnet.Transitions.RECOVER);

        var ports = def.iface().ports().stream().map(p -> p.name()).sorted().toList();
        assertThat(ports).containsExactly(
                "modelActive", "nudgeNeeded", "reconnectNeeded", "responseAwaited");
    }

    // ============================================================
    //  Fixtures
    // ============================================================

    private record Fixture(Marking finalMarking) {
        List<Token<?>> tokensAt(Place<?> p) {
            Collection<? extends Token<?>> raw = finalMarking.peekTokens(p);
            return new ArrayList<>(raw);
        }
    }

    /** Structure-only stub: the SCG never invokes actions, it only walks arcs. */
    private static BaseLlm scgStubLlm() {
        return new BaseLlm("scg-stub") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.empty();
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    private static Fixture drive(LiveApiRecoverySubnet.Config config,
                                  ExecutorScript script) throws Exception {
        var def = LiveApiRecoverySubnet.def(config);
        var net = PetriNet.builder("test")
                .compose(def)
                .build()
                .bindActions(LiveApiRecoverySubnet.actionBindings(config));

        var clock = new ManualClock();
        var executor = BitmapNetExecutor.builder(net, Map.of())
                .environmentPlaces(
                        env(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED),
                        env(LiveApiRecoverySubnet.Places.MODEL_ACTIVE))
                .eventStore(EventStore.inMemory())
                .environment(clock)
                .deadlineTolerance(Duration.ZERO)
                .build();

        var pool = Executors.newVirtualThreadPerTaskExecutor();
        var task = CompletableFuture.supplyAsync(executor::run, pool);

        script.run(executor, clock);

        executor.drain();
        var finalMarking = task.get(5, TimeUnit.SECONDS);
        pool.shutdown();
        return new Fixture(finalMarking);
    }

    @SuppressWarnings("unchecked")
    private static <T> EnvironmentPlace<T> env(Place<T> place) {
        return EnvironmentPlace.of(place);
    }

    private static void inject(BitmapNetExecutor executor, Place<Void> place) {
        executor.inject(env(place), (Void) null).join();
    }

    private static boolean marked(BitmapNetExecutor executor, Place<?> place) {
        return !executor.marking().peekTokens(place).isEmpty();
    }

    @FunctionalInterface
    private interface ExecutorScript {
        void run(BitmapNetExecutor executor, ManualClock clock) throws Exception;
    }
}
