package org.libpetri.adk.demos.voice;

import static com.google.common.truth.Truth.assertThat;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
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
import org.libpetri.core.TransitionAction;
import org.libpetri.event.EventStore;
import org.libpetri.analysis.MarkingState;
import org.libpetri.analysis.StateClassGraph;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmStreamingStepSubnet;
import org.libpetri.runtime.BitmapNetExecutor;
import org.libpetri.runtime.Marking;
import org.libpetri.runtime.PetriNetExecutor;

class LiveApiRecoverySubnetTest {

    private static final LiveApiRecoverySubnet.Config FAST =
            new LiveApiRecoverySubnet.Config(Duration.ofMillis(80), Duration.ofMillis(80));

    @Test
    void silent_model_triggers_nudge_then_reconnect() throws Exception {
        var fixture = drive(FAST, executor -> {
            // Caller signals "model should be responding but isn't"
            executor.inject(env(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED), (Void) null);
            // Wait long enough for both nudge (80ms) and reconnect (80ms) to fire.
            sleep(Duration.ofMillis(400));
        });

        // Nudge fired, then reconnect fired.
        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.NUDGE_NEEDED)).hasSize(1);
        assertThat(fixture.tokensAt(LiveApiRecoverySubnet.Places.RECONNECT_NEEDED)).hasSize(1);
    }

    @Test
    void model_active_inhibits_nudge() throws Exception {
        var fixture = drive(FAST, executor -> {
            // Model is actively responding — drop MODEL_ACTIVE token first.
            executor.inject(env(LiveApiRecoverySubnet.Places.MODEL_ACTIVE), (Void) null);
            executor.inject(env(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED), (Void) null);
            sleep(Duration.ofMillis(300));
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
        var fixture = drive(FAST, executor -> {
            executor.inject(env(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED), (Void) null);
            sleep(Duration.ofMillis(40));
            executor.inject(env(LiveApiRecoverySubnet.Places.MODEL_ACTIVE), (Void) null);
            sleep(Duration.ofMillis(300));
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
        var scgBindings = new LinkedHashMap<String, TransitionAction>();
        scgBindings.putAll(LlmStreamingStepSubnet.actionBindings(
                scgStubLlm(), streamConfig));
        scgBindings.putAll(BargeInSubnet.actionBindings());
        scgBindings.putAll(LiveApiRecoverySubnet.actionBindings(FAST));

        var net = PetriNet.builder("voice-scg-check")
                .compose(LlmStreamingStepSubnet.DEF)
                .compose(BargeInSubnet.DEF)
                .compose(recoveryDef)
                .build()
                .bindActions(scgBindings);

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
        var fixture = drive(FAST, executor -> {
            executor.inject(env(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED), (Void) null);
            // Wait for the nudge to actually fire, rather than sleeping 160ms and
            // betting the MODEL_ACTIVE inject wins the race against the reconnect
            // deadline that lands at roughly the same moment. On a loaded box that
            // bet loses, reconnect fires first, and the assertion below goes red
            // for reasons that have nothing to do with the code.
            awaitMarked(executor, LiveApiRecoverySubnet.Places.NUDGE_NEEDED, 5_000);
            // Now the model finally responds — caller signals MODEL_ACTIVE.
            executor.inject(env(LiveApiRecoverySubnet.Places.MODEL_ACTIVE), (Void) null);
            // Recover would have fired after another 80ms; the inhibitor blocks it.
            sleep(Duration.ofMillis(200));
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

        var executor = BitmapNetExecutor.builder(net, Map.of())
                .environmentPlaces(
                        env(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED),
                        env(LiveApiRecoverySubnet.Places.MODEL_ACTIVE))
                .eventStore(EventStore.inMemory())
                .build();

        var pool = Executors.newVirtualThreadPerTaskExecutor();
        var task = CompletableFuture.supplyAsync(executor::run, pool);

        script.run(executor);

        executor.drain();
        var finalMarking = task.get(5, TimeUnit.SECONDS);
        pool.shutdown();
        return new Fixture(finalMarking);
    }

    @SuppressWarnings("unchecked")
    private static <T> EnvironmentPlace<T> env(Place<T> place) {
        return EnvironmentPlace.of(place);
    }

    private static void sleep(Duration d) {
        try {
            Thread.sleep(d.toMillis());
        } catch (InterruptedException e) {
            // Do not swallow. Every test in this class positions events relative
            // to real deadlines, so a shortened wait does not fail loudly, it
            // silently runs a different scenario and asserts the old one.
            Thread.currentThread().interrupt();
            throw new AssertionError("interrupted while waiting " + d, e);
        }
    }

    /** Polls the live marking until {@code place} holds a token, or fails. */
    @SuppressWarnings("BusyWait")
    private static void awaitMarked(BitmapNetExecutor executor, Place<?> place, long timeoutMillis)
            throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (System.currentTimeMillis() < deadline) {
            if (!executor.marking().peekTokens(place).isEmpty()) return;
            Thread.sleep(5);
        }
        throw new AssertionError(
                "place '" + place.name() + "' was not marked within " + timeoutMillis + "ms");
    }

    @FunctionalInterface
    private interface ExecutorScript {
        void run(BitmapNetExecutor executor) throws Exception;
    }
}
