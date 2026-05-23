package org.libpetri.adk.demos.voice;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import org.libpetri.adk.subnet.SubnetActions;
import org.libpetri.core.Arc;
import org.libpetri.core.Place;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Timing;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;

/**
 * Two-stage silence-recovery subnet — the {@code NUDGE_TURN_COMPLETE}
 * → {@code RECOVER_SILENT_MODEL} escalation for Live-API / BIDI
 * voice sessions.
 *
 * <h2>Why this is a real win for BIDI agents</h2>
 * <p>Live-API and BIDI voice agents share a class of "model went
 * silent mid-turn" failures: the connection is up, the audio channel
 * is live, but the model produces no output for several seconds. Two
 * actions are required: a soft nudge first (re-send
 * {@code turnComplete=true} hoping it shakes the model loose), then a
 * hard reconnect if silence continues. Both must <i>not</i> fire if
 * the model actually responds. Implementing this with callbacks and
 * timers is the classic source of off-by-one bugs (the nudge fires
 * just as the model starts speaking, the reconnect throws away an
 * in-flight reply, the timer wasn't reset on resume…).
 *
 * <p>Petri net topology makes the failure mode structurally impossible:
 * both recovery transitions are <b>inhibitor-guarded on {@code MODEL_ACTIVE}</b>.
 * When the model produces output, the caller injects a token into
 * {@code MODEL_ACTIVE}; the inhibitor blocks both timed transitions
 * <i>atomically</i>, regardless of how close to the deadline they
 * were. When the model goes silent again, the caller drains
 * {@code MODEL_ACTIVE} and the timers run fresh.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [RESPONSE_AWAITED] --T_Nudge--> Out.and([NUDGE_NEEDED], [RECOVERY_PENDING])
 *     timing: Timing.delayed(nudgeAfter)
 *     inhibitor: MODEL_ACTIVE   (model talking → don't fire)
 *
 *   [RECOVERY_PENDING] --T_Recover--> [RECONNECT_NEEDED]
 *     timing: Timing.delayed(reconnectAfter)
 *     inhibitor: MODEL_ACTIVE   (model resumed → don't reconnect)
 * </pre>
 *
 * <h2>Caller wiring</h2>
 * <p>The host net is responsible for:
 * <ul>
 *   <li>Injecting a {@code RESPONSE_AWAITED} token when "the model
 *       should be replying but isn't" — typically right after a
 *       {@code turnComplete=true} is sent.</li>
 *   <li>Injecting / draining {@code MODEL_ACTIVE} as the model
 *       produces / stops producing output chunks.</li>
 *   <li>Watching {@code NUDGE_NEEDED} and re-sending
 *       {@code turnComplete=true} on the Live-API session when it appears.</li>
 *   <li>Watching {@code RECONNECT_NEEDED} and reconnecting the
 *       Live-API session when it appears.</li>
 * </ul>
 *
 * <p>None of these are special interaction modes — every signal is an
 * env-place token (in or out) per the runtime model.
 */
public final class LiveApiRecoverySubnet {

    public static final String NAME = "LiveApiRecovery";

    public static final class Transitions {
        public static final String NUDGE    = NAME + "_Nudge";
        public static final String RECOVER  = NAME + "_Recover";
        private Transitions() {}
    }

    public static final class Places {
        /** Caller signals "model should be talking but isn't" by injecting a token here. */
        public static final Place<Void> RESPONSE_AWAITED =
                Place.of(NAME + "_responseAwaited", Void.class);

        /** Caller injects/drains as model produces/stops output. Inhibitor source for both recovery transitions. */
        public static final Place<Void> MODEL_ACTIVE =
                Place.of(NAME + "_modelActive", Void.class);

        /** Internal — intermediate between Nudge and Recover (carries the in-flight nudge state). */
        public static final Place<Void> RECOVERY_PENDING =
                Place.of(NAME + "_recoveryPending", Void.class);

        /** Caller observes — fires when the model should be nudged with another turnComplete. */
        public static final Place<Void> NUDGE_NEEDED =
                Place.of(NAME + "_nudgeNeeded", Void.class);

        /** Caller observes — fires when the model should be reconnected (Live-API session re-established). */
        public static final Place<Void> RECONNECT_NEEDED =
                Place.of(NAME + "_reconnectNeeded", Void.class);

        private Places() {}
    }

    public record Config(Duration nudgeAfter, Duration reconnectAfter) {
        public Config {
            Objects.requireNonNull(nudgeAfter, "nudgeAfter");
            Objects.requireNonNull(reconnectAfter, "reconnectAfter");
            if (nudgeAfter.isNegative() || nudgeAfter.isZero()) {
                throw new IllegalArgumentException("nudgeAfter must be positive: " + nudgeAfter);
            }
            if (reconnectAfter.isNegative() || reconnectAfter.isZero()) {
                throw new IllegalArgumentException("reconnectAfter must be positive: " + reconnectAfter);
            }
        }

        /** Sensible defaults: 3s silence triggers nudge, +3s more triggers reconnect. */
        public static Config defaults() {
            return new Config(Duration.ofSeconds(3), Duration.ofSeconds(3));
        }
    }

    public static SubnetDef<Void> def(Config config) {
        Objects.requireNonNull(config, "config");

        var nudge = Transition.builder(Transitions.NUDGE)
                .inputs(Arc.In.one(Places.RESPONSE_AWAITED))
                .inhibitor(Places.MODEL_ACTIVE)
                .outputs(Arc.Out.and(Places.NUDGE_NEEDED, Places.RECOVERY_PENDING))
                .timing(Timing.delayed(config.nudgeAfter()))
                .build();

        var recover = Transition.builder(Transitions.RECOVER)
                .inputs(Arc.In.one(Places.RECOVERY_PENDING))
                .inhibitor(Places.MODEL_ACTIVE)
                .outputs(Arc.Out.place(Places.RECONNECT_NEEDED))
                .timing(Timing.delayed(config.reconnectAfter()))
                .build();

        return SubnetDef.builder(NAME)
                .place(Places.RESPONSE_AWAITED)
                .place(Places.MODEL_ACTIVE)
                .place(Places.RECOVERY_PENDING)
                .place(Places.NUDGE_NEEDED)
                .place(Places.RECONNECT_NEEDED)
                .transition(nudge)
                .transition(recover)
                .inputPort("responseAwaited",  Places.RESPONSE_AWAITED)
                .inoutPort("modelActive",      Places.MODEL_ACTIVE)
                .outputPort("nudgeNeeded",     Places.NUDGE_NEEDED)
                .outputPort("reconnectNeeded", Places.RECONNECT_NEEDED)
                .build();
    }

    /**
     * Default bindings — both transitions have pure ctx-only actions
     * (no business logic): consume input, produce outputs. The
     * "behaviour" is the topology + timing + inhibitors.
     */
    public static Map<String, TransitionAction> actionBindings(Config config) {
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.NUDGE,   nudgeAction());
        session.put(Transitions.RECOVER, recoverAction());
        return SubnetActions.bind(def(config), session);
    }

    private static TransitionAction nudgeAction() {
        return ctx -> {
            ctx.input(Places.RESPONSE_AWAITED);
            ctx.output(Places.NUDGE_NEEDED, (Void) null);
            ctx.output(Places.RECOVERY_PENDING, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction recoverAction() {
        return ctx -> {
            ctx.input(Places.RECOVERY_PENDING);
            ctx.output(Places.RECONNECT_NEEDED, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private LiveApiRecoverySubnet() {}
}
