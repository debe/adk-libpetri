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
 * Voice-activity-detection <b>producer</b> subnet — the piece that was missing
 * from the BIDI catalog. {@link BargeInSubnet} and the recovery/streaming subnets
 * all <i>consume</i> a {@code VOICE_ACTIVITY_OPEN} window token; nothing produced
 * it, so every voice consumer hand-rolled the speech-edge → window bridge. This
 * subnet is that bridge, modelled structurally.
 *
 * <h2>Where the signals come from</h2>
 * <p>The Gemini Live API already emits automatic-VAD edges and a barge-in
 * signal. ADK Java's {@code GeminiLlmConnection} flattens them into
 * {@code LlmResponse} and drops the VAD edges — but they are public, typed API
 * on <i>genai</i>, so the consumer reads genai's Live session directly (no fork)
 * and forwards each signal off the raw {@code LiveServerMessage} to its env place:
 * <pre>{@code
 * AsyncSession session = client.async.live.connect(model, liveConfig).join();
 * session.receive(msg -> {
 *     msg.voiceActivity().flatMap(VoiceActivity::voiceActivityType).ifPresent(t -> {
 *         if (t.knownEnum() == VoiceActivityType.Known.ACTIVITY_START)
 *             runner.inject(VadSubnet.Places.SPEECH_STARTED, null);
 *         else if (t.knownEnum() == VoiceActivityType.Known.ACTIVITY_END)
 *             runner.inject(VadSubnet.Places.SPEECH_STOPPED, null);
 *     });
 *     msg.serverContent().flatMap(LiveServerContent::interrupted)
 *        .filter(Boolean::booleanValue)
 *        .ifPresent(i -> runner.inject(BargeInSubnet.Places.INTERRUPTED, null));
 * });
 * }</pre>
 *
 * <h2>Why a Petri net and not a boolean</h2>
 * <p>"Is the user speaking right now" is exactly the shared-mutable-flag that
 * causes barge-in races. Here it is the <i>marking</i> of one place
 * ({@link BargeInSubnet.Places#VOICE_ACTIVITY_OPEN}, reused by identity so the
 * window this subnet opens is the window barge-in reads). Open and close are
 * idempotent by structure: a redundant {@code activityStart} while already open,
 * or a stray {@code activityEnd} while already closed, is absorbed by an
 * inhibitor/read-guarded ignore branch instead of corrupting the count.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [SPEECH_STARTED] --T_OpenWindow---------> [VOICE_ACTIVITY_OPEN]
 *                      inhibitor(VOICE_ACTIVITY_OPEN)   (was closed → open it)
 *   [SPEECH_STARTED] --T_IgnoreRedundantStart-> [SPEECH_EDGE_IGNORED]
 *                      read(VOICE_ACTIVITY_OPEN)        (already open → absorb)
 *
 *   [SPEECH_STOPPED] + [VOICE_ACTIVITY_OPEN] --T_CloseWindow--> [UTTERANCE_ENDED]
 *                                                                (was open → close it)
 *   [SPEECH_STOPPED] --T_IgnoreRedundantStop--> [SPEECH_EDGE_IGNORED]
 *                      inhibitor(VOICE_ACTIVITY_OPEN)            (already closed → absorb)
 * </pre>
 *
 * <p>This is an <b>example</b>, not a shipped subnet (stock subnets are
 * templates; you compose your own): the Live-API edge shape is
 * transport-specific, so users copy and adapt it. The
 * value it documents is the structural VAD discipline, which is identical across
 * transports.
 */
public final class VadSubnet {

    public static final String NAME = "Vad";

    public static final class Transitions {
        public static final String OPEN_WINDOW           = NAME + "_OpenWindow";
        public static final String IGNORE_REDUNDANT_START = NAME + "_IgnoreRedundantStart";
        public static final String CLOSE_WINDOW          = NAME + "_CloseWindow";
        public static final String IGNORE_REDUNDANT_STOP = NAME + "_IgnoreRedundantStop";
        private Transitions() {}
    }

    public static final class Places {
        /** Caller injects on Live-API {@code activityStart} (speech began). */
        public static final Place<Void> SPEECH_STARTED =
                Place.of(NAME + "_speechStarted", Void.class);

        /** Caller injects on Live-API {@code activityEnd} (speech ended). */
        public static final Place<Void> SPEECH_STOPPED =
                Place.of(NAME + "_speechStopped", Void.class);

        /** Emitted when a real close happens — downstream "user finished" trigger. */
        public static final Place<Void> UTTERANCE_ENDED =
                Place.of(NAME + "_utteranceEnded", Void.class);

        /** Observability: a redundant start/stop edge was absorbed (no state change). */
        public static final Place<Void> SPEECH_EDGE_IGNORED =
                Place.of(NAME + "_speechEdgeIgnored", Void.class);

        private Places() {}
    }

    /**
     * The window place is reused <i>by identity</i> from {@link BargeInSubnet} so
     * that composing the two nets fuses them: the window this subnet opens is the
     * exact place barge-in (and recovery) read.
     */
    public static final Place<Void> VOICE_ACTIVITY_OPEN = BargeInSubnet.Places.VOICE_ACTIVITY_OPEN;

    public static final SubnetDef<Void> DEF = SubnetDef.builder(NAME)
            .place(Places.SPEECH_STARTED)
            .place(Places.SPEECH_STOPPED)
            .place(VOICE_ACTIVITY_OPEN)
            .place(Places.UTTERANCE_ENDED)
            .place(Places.SPEECH_EDGE_IGNORED)
            .transition(Transition.builder(Transitions.OPEN_WINDOW)
                    .inputs(Arc.In.one(Places.SPEECH_STARTED))
                    .inhibitor(VOICE_ACTIVITY_OPEN)
                    .outputs(Arc.Out.place(VOICE_ACTIVITY_OPEN))
                    .build())
            .transition(Transition.builder(Transitions.IGNORE_REDUNDANT_START)
                    .inputs(Arc.In.one(Places.SPEECH_STARTED))
                    .read(VOICE_ACTIVITY_OPEN)
                    .outputs(Arc.Out.place(Places.SPEECH_EDGE_IGNORED))
                    .build())
            .transition(Transition.builder(Transitions.CLOSE_WINDOW)
                    .inputs(Arc.In.one(Places.SPEECH_STOPPED), Arc.In.one(VOICE_ACTIVITY_OPEN))
                    .outputs(Arc.Out.place(Places.UTTERANCE_ENDED))
                    .build())
            .transition(Transition.builder(Transitions.IGNORE_REDUNDANT_STOP)
                    .inputs(Arc.In.one(Places.SPEECH_STOPPED))
                    .inhibitor(VOICE_ACTIVITY_OPEN)
                    .outputs(Arc.Out.place(Places.SPEECH_EDGE_IGNORED))
                    .build())
            .inputPort("speechStarted",     Places.SPEECH_STARTED)
            .inputPort("speechStopped",     Places.SPEECH_STOPPED)
            .inoutPort("voiceActivityOpen", VOICE_ACTIVITY_OPEN)
            .outputPort("utteranceEnded",   Places.UTTERANCE_ENDED)
            .outputPort("speechEdgeIgnored", Places.SPEECH_EDGE_IGNORED)
            .build();

    public static Map<String, TransitionAction> actionBindings() {
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.OPEN_WINDOW,            openWindowAction());
        session.put(Transitions.IGNORE_REDUNDANT_START, ignoreStartAction());
        session.put(Transitions.CLOSE_WINDOW,           closeWindowAction());
        session.put(Transitions.IGNORE_REDUNDANT_STOP,  ignoreStopAction());
        return SubnetActions.bind(DEF, session);
    }

    private static TransitionAction openWindowAction() {
        return ctx -> {
            ctx.input(Places.SPEECH_STARTED);
            ctx.output(VOICE_ACTIVITY_OPEN, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction ignoreStartAction() {
        return ctx -> {
            ctx.input(Places.SPEECH_STARTED);
            ctx.output(Places.SPEECH_EDGE_IGNORED, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction closeWindowAction() {
        return ctx -> {
            ctx.input(Places.SPEECH_STOPPED);
            ctx.input(VOICE_ACTIVITY_OPEN);
            ctx.output(Places.UTTERANCE_ENDED, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction ignoreStopAction() {
        return ctx -> {
            ctx.input(Places.SPEECH_STOPPED);
            ctx.output(Places.SPEECH_EDGE_IGNORED, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private VadSubnet() {}
}
