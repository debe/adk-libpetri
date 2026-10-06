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
 * → {@code RECOVER_SILENT_MODEL} escalation ladder for Live-API / BIDI
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
 * <h2>Cancel on activity</h2>
 * <p>Each rung is a place drained by a timed transition:
 * {@code RESPONSE_AWAITED} by {@code Nudge}, {@code RECOVERY_PENDING} by
 * {@code Recover}. Both are inhibitor-guarded on {@code MODEL_ACTIVE}, so
 * neither can fire while the model is producing output. The model
 * answering also <i>cancels</i> the rung it lands on: {@code Answered}
 * consumes {@code RESPONSE_AWAITED} and {@code AnsweredLate} consumes
 * {@code RECOVERY_PENDING}, each reading {@code MODEL_ACTIVE} at a higher
 * priority than the timers. Once the model has answered, nothing of the
 * ladder survives, so a later silence cannot escalate from a stale rung.
 *
 * <p>{@code MODEL_ACTIVE} has an in-net consumer: when the model stops
 * producing output, the caller injects {@code MODEL_QUIET}, and
 * {@code ModelQuiet} consumes it together with every {@code MODEL_ACTIVE}
 * token ({@code In.all}, so stacked activity injections clear in one
 * firing). {@code IgnoreQuiet} sinks a {@code MODEL_QUIET} that arrives
 * when the model is not marked active. The ladder restarts on the
 * caller's next {@code RESPONSE_AWAITED}.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [RESPONSE_AWAITED] --Nudge--> Out.and([NUDGE_NEEDED], [RECOVERY_PENDING])
 *     timing: Timing.delayed(nudgeAfter)
 *     inhibitor: MODEL_ACTIVE   (model talking → don't fire)
 *
 *   [RECOVERY_PENDING] --Recover--> [RECONNECT_NEEDED]
 *     timing: Timing.delayed(reconnectAfter)
 *     inhibitor: MODEL_ACTIVE   (model resumed → don't reconnect)
 *
 *   [RESPONSE_AWAITED] --Answered-->       (nothing)   read: MODEL_ACTIVE, prio +10
 *   [RECOVERY_PENDING] --AnsweredLate-->   (nothing)   read: MODEL_ACTIVE, prio +10
 *
 *   [MODEL_QUIET] + all([MODEL_ACTIVE]) --ModelQuiet--> (nothing)
 *   [MODEL_QUIET] --IgnoreQuiet--> [QUIET_IGNORED]     inhibitor: MODEL_ACTIVE
 *                                                      reset: QUIET_IGNORED (holds at most 1)
 * </pre>
 *
 * <h2>Caller wiring</h2>
 * <p>The host net is responsible for:
 * <ul>
 *   <li>Injecting a {@code RESPONSE_AWAITED} token when "the model
 *       should be replying but isn't" — typically right after a
 *       {@code turnComplete=true} is sent.</li>
 *   <li>Injecting {@code MODEL_ACTIVE} when the model produces output
 *       and {@code MODEL_QUIET} when it stops.</li>
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
        public static final String ANSWERED      = NAME + "_Answered";
        public static final String ANSWERED_LATE = NAME + "_AnsweredLate";
        public static final String MODEL_QUIET   = NAME + "_ModelQuiet";
        public static final String IGNORE_QUIET  = NAME + "_IgnoreQuiet";
        private Transitions() {}
    }

    public static final class Places {
        /** Caller signals "model should be talking but isn't" by injecting a token here. */
        public static final Place<Void> RESPONSE_AWAITED =
                Place.of(NAME + "_responseAwaited", Void.class);

        /**
         * Caller injects when the model produces output. Inhibits both timed
         * rungs, cancels the rung it lands on, and is cleared in-net by
         * {@link #MODEL_QUIET}.
         */
        public static final Place<Void> MODEL_ACTIVE =
                Place.of(NAME + "_modelActive", Void.class);

        /** Caller injects when the model stops producing output; clears {@link #MODEL_ACTIVE}. */
        public static final Place<Void> MODEL_QUIET =
                Place.of(NAME + "_modelQuiet", Void.class);

        /**
         * Sink for a {@link #MODEL_QUIET} that arrives while the model is not
         * marked active. Holds at most one token: each such firing resets it.
         */
        public static final Place<Void> QUIET_IGNORED =
                Place.of(NAME + "_quietIgnored", Void.class);

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

        // The model answered: cancel the rung it landed on. Priority above
        // the timers, which the MODEL_ACTIVE inhibitor already holds off.
        var answered = Transition.builder(Transitions.ANSWERED)
                .inputs(Arc.In.one(Places.RESPONSE_AWAITED))
                .read(Places.MODEL_ACTIVE)
                .priority(10)
                .build();

        var answeredLate = Transition.builder(Transitions.ANSWERED_LATE)
                .inputs(Arc.In.one(Places.RECOVERY_PENDING))
                .read(Places.MODEL_ACTIVE)
                .priority(10)
                .build();

        // The model went quiet: clear every stacked MODEL_ACTIVE token.
        // In.all enables only on 1+ tokens; IgnoreQuiet covers zero.
        var modelQuiet = Transition.builder(Transitions.MODEL_QUIET)
                .inputs(Arc.In.one(Places.MODEL_QUIET), Arc.In.all(Places.MODEL_ACTIVE))
                .build();

        // The reset keeps the sink at one token, so a stream of quiet
        // signals with the model idle does not grow the marking.
        var ignoreQuiet = Transition.builder(Transitions.IGNORE_QUIET)
                .inputs(Arc.In.one(Places.MODEL_QUIET))
                .inhibitor(Places.MODEL_ACTIVE)
                .reset(Places.QUIET_IGNORED)
                .outputs(Arc.Out.place(Places.QUIET_IGNORED))
                .build();

        return SubnetDef.builder(NAME)
                .place(Places.RESPONSE_AWAITED)
                .place(Places.MODEL_ACTIVE)
                .place(Places.MODEL_QUIET)
                .place(Places.RECOVERY_PENDING)
                .place(Places.NUDGE_NEEDED)
                .place(Places.RECONNECT_NEEDED)
                .place(Places.QUIET_IGNORED)
                .transition(nudge)
                .transition(recover)
                .transition(answered)
                .transition(answeredLate)
                .transition(modelQuiet)
                .transition(ignoreQuiet)
                .inputPort("responseAwaited",  Places.RESPONSE_AWAITED)
                .inoutPort("modelActive",      Places.MODEL_ACTIVE)
                .inputPort("modelQuiet",       Places.MODEL_QUIET)
                .outputPort("nudgeNeeded",     Places.NUDGE_NEEDED)
                .outputPort("reconnectNeeded", Places.RECONNECT_NEEDED)
                .outputPort("quietIgnored",    Places.QUIET_IGNORED)
                .build();
    }

    /**
     * Default bindings — every transition has a pure ctx-only action
     * (no business logic): consume input, produce outputs. The
     * "behaviour" is the topology + timing + inhibitors.
     */
    public static Map<String, TransitionAction> actionBindings(Config config) {
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.NUDGE,   nudgeAction());
        session.put(Transitions.RECOVER, recoverAction());
        session.put(Transitions.ANSWERED,      consumeOnly());
        session.put(Transitions.ANSWERED_LATE, consumeOnly());
        session.put(Transitions.MODEL_QUIET,   consumeOnly());
        session.put(Transitions.IGNORE_QUIET,  ignoreQuietAction());
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

    /** No outputs: the firing itself consumes the inputs. */
    private static TransitionAction consumeOnly() {
        return ctx -> CompletableFuture.completedFuture(null);
    }

    private static TransitionAction ignoreQuietAction() {
        return ctx -> {
            ctx.input(Places.MODEL_QUIET);
            ctx.output(Places.QUIET_IGNORED, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private LiveApiRecoverySubnet() {}
}
