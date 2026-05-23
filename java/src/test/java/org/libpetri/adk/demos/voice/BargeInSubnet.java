package org.libpetri.adk.demos.voice;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import org.libpetri.adk.subnet.SubnetActions;
import org.libpetri.core.Arc;
import org.libpetri.core.Place;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;

/**
 * Voice-activity-gated barge-in subnet — the
 * {@code BARGE_IN_SENT} / {@code INTERRUPT_DISCARDED}
 * inhibitor-pair pattern.
 *
 * <h2>Why this is a real win for BIDI agents</h2>
 * <p>Barge-in is the classic source of voice-agent race conditions:
 * the user starts speaking, the system needs to decide "is the user
 * still speaking right now, or did they stop?" and route the
 * interrupt accordingly. Implementing this with shared mutable flags
 * is where the lost-update / stale-read bugs live. A Petri net
 * sidesteps the entire bug class with two competing transitions on
 * the <i>same</i> {@code INTERRUPTED} token, one with
 * {@code read(VOICE_ACTIVITY_OPEN)} and one with
 * {@code inhibitor(VOICE_ACTIVITY_OPEN)}. The marking is the single
 * source of truth for "is the user speaking" — at most one transition
 * can be enabled at any time, and the orchestrator picks
 * deterministically.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [INTERRUPTED] --T_SendBargeIn-----> [BARGE_IN_SENT]
 *                  read(VOICE_ACTIVITY_OPEN)   (user IS speaking → send)
 *
 *   [INTERRUPTED] --T_DiscardInterrupt-> [INTERRUPT_DISCARDED]
 *                  inhibitor(VOICE_ACTIVITY_OPEN)   (user stopped → no-op)
 * </pre>
 *
 * <p>Both transitions take {@code INTERRUPTED} as input ({@code In.one});
 * the {@code Read} and {@code Inhibitor} arcs are mutually
 * exclusive on the {@code VOICE_ACTIVITY_OPEN} place. At any given
 * marking exactly one of the two transitions is enabled — the
 * decision is structural, not procedural.
 *
 * <h2>Caller wiring</h2>
 * <ul>
 *   <li>Inject a token into {@code INTERRUPTED} when the user's
 *       voice-activity-detection fires.</li>
 *   <li>Inject (and later drain) {@code VOICE_ACTIVITY_OPEN} as the
 *       voice window opens and closes.</li>
 *   <li>Watch {@code BARGE_IN_SENT} and cancel the in-flight model
 *       audio + send a barge-in signal to the Live-API connection.</li>
 *   <li>{@code INTERRUPT_DISCARDED} is the no-op branch — usually
 *       just logged for observability.</li>
 * </ul>
 */
public final class BargeInSubnet {

    public static final String NAME = "BargeIn";

    public static final class Transitions {
        public static final String SEND_BARGE_IN     = NAME + "_SendBargeIn";
        public static final String DISCARD_INTERRUPT = NAME + "_DiscardInterrupt";
        private Transitions() {}
    }

    public static final class Places {
        /** Caller injects when voice-activity-detection fires. */
        public static final Place<Void> INTERRUPTED =
                Place.of(NAME + "_interrupted", Void.class);

        /** Caller injects/drains as the user's voice window opens/closes. */
        public static final Place<Void> VOICE_ACTIVITY_OPEN =
                Place.of(NAME + "_voiceActivityOpen", Void.class);

        /** Caller observes — fires when a barge-in signal should be sent. */
        public static final Place<Void> BARGE_IN_SENT =
                Place.of(NAME + "_bargeInSent", Void.class);

        /** Caller observes — fires when the interrupt was correctly ignored (user already stopped). */
        public static final Place<Void> INTERRUPT_DISCARDED =
                Place.of(NAME + "_interruptDiscarded", Void.class);

        private Places() {}
    }

    public static final SubnetDef<Void> DEF = SubnetDef.builder(NAME)
            .place(Places.INTERRUPTED)
            .place(Places.VOICE_ACTIVITY_OPEN)
            .place(Places.BARGE_IN_SENT)
            .place(Places.INTERRUPT_DISCARDED)
            .transition(Transition.builder(Transitions.SEND_BARGE_IN)
                    .inputs(Arc.In.one(Places.INTERRUPTED))
                    .read(Places.VOICE_ACTIVITY_OPEN)
                    .outputs(Arc.Out.place(Places.BARGE_IN_SENT))
                    .build())
            .transition(Transition.builder(Transitions.DISCARD_INTERRUPT)
                    .inputs(Arc.In.one(Places.INTERRUPTED))
                    .inhibitor(Places.VOICE_ACTIVITY_OPEN)
                    .outputs(Arc.Out.place(Places.INTERRUPT_DISCARDED))
                    .build())
            .inputPort("interrupted",        Places.INTERRUPTED)
            .inoutPort("voiceActivityOpen",  Places.VOICE_ACTIVITY_OPEN)
            .outputPort("bargeInSent",       Places.BARGE_IN_SENT)
            .outputPort("interruptDiscarded", Places.INTERRUPT_DISCARDED)
            .build();

    public static Map<String, TransitionAction> actionBindings() {
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.SEND_BARGE_IN,     sendAction());
        session.put(Transitions.DISCARD_INTERRUPT, discardAction());
        return SubnetActions.bind(DEF, session);
    }

    private static TransitionAction sendAction() {
        return ctx -> {
            ctx.input(Places.INTERRUPTED);
            ctx.output(Places.BARGE_IN_SENT, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction discardAction() {
        return ctx -> {
            ctx.input(Places.INTERRUPTED);
            ctx.output(Places.INTERRUPT_DISCARDED, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private BargeInSubnet() {}
}
