package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.event.EventStore;
import org.libpetri.runtime.BitmapNetExecutor;
import org.libpetri.runtime.Marking;

/**
 * Worked example for the <b>generic raw-provider passthrough</b> escape hatch.
 * When ADK's typed surface does not model a provider feature yet,
 * the user reaches it without forking ADK and without waiting for a release —
 * while ADK and the stock subnets stay the brain for everything they do model.
 *
 * <p>The shipped library contributes <b>nothing</b> here, deliberately. The
 * colours, the record and the transition are all declared below, in user code,
 * in about fifteen lines. There is no stock subnet and no SPI, and there is no
 * shared {@code RAW_PROVIDER_*} colour in {@link AdkColours} either: an opaque
 * {@code (String feature, Object payload)} pair on one global place would be
 * the state-bag shape the catalog exists to forbid, and two unrelated escape
 * hatches declaring {@code In.one} on it would each be enabled by the other's
 * token. Declare a place typed to <i>your</i> feature instead, as here.
 *
 * <h2>The pattern</h2>
 * <pre>
 *   [VAD_FRAME]env --T_CallRaw--> [VAD_RESULT]
 *       action calls the raw genai/transport API directly
 * </pre>
 *
 * <p>In production {@code VAD_FRAME} is an env place injected via
 * {@code runner.inject(VAD_FRAME, Token.of(frame))} the moment the application
 * needs a not-yet-modelled feature; downstream transitions read
 * {@code VAD_RESULT} and fold the result back into the typed flow. Here we seed
 * the initial marking and run to quiescence to keep the example deterministic.
 */
class RawProviderPassthroughDemoTest {

    /**
     * The feature-specific payload, typed. Not {@code Object}: the whole point
     * is that the escape hatch stays as typed as the feature allows, so the
     * marking still says what it is carrying.
     */
    private record VadFrame(String sessionId, byte[] audio) {}

    /** The raw call's result, likewise typed to this feature. */
    private record VadResult(String sessionId, boolean speechDetected) {}

    /** Declared here, by the caller, for this feature only. */
    private static final Place<VadFrame> VAD_FRAME =
            Place.of("demo.vadFrame", VadFrame.class);
    private static final Place<VadResult> VAD_RESULT =
            Place.of("demo.vadResult", VadResult.class);

    /**
     * Stand-in for the raw provider call the typed SDK doesn't expose yet — e.g.
     * a brand-new Live-API control frame or an experimental {@code generateContent}
     * config field. In real code this is a direct call into the genai SDK / a raw
     * HTTP request; here it just transforms the payload so the test can assert the
     * round-trip.
     */
    private static boolean callRawApi(byte[] audio) {
        return audio.length > 0;
    }

    @Test
    void raw_request_is_routed_through_a_user_transition_to_a_raw_event() {
        var net = PetriNet.builder("raw-passthrough")
                .place(VAD_FRAME)
                .place(VAD_RESULT)
                .transition(Transition.builder("T_CallRaw")
                        .inputs(Arc.In.one(VAD_FRAME))
                        .outputs(Arc.Out.place(VAD_RESULT))
                        .build())
                .build()
                .bindActions(Map.of("T_CallRaw", callRawAction()));

        var initial = new LinkedHashMap<Place<?>, List<Token<?>>>();
        initial.put(VAD_FRAME, List.of(
                Token.of(new VadFrame("sess-1", new byte[] {1, 2, 3}))));

        Marking quiescent = BitmapNetExecutor.builder(net, initial)
                .eventStore(EventStore.inMemory())
                .build()
                .run();

        var events = quiescent.peekTokens(VAD_RESULT);
        assertThat(events).hasSize(1);
        VadResult result = (VadResult) events.iterator().next().value();
        assertThat(result.sessionId()).isEqualTo("sess-1");
        assertThat(result.speechDetected()).isTrue();
        // The request token was consumed, so nothing is left on the inbound boundary.
        assertThat(quiescent.peekTokens(VAD_FRAME)).isEmpty();
    }

    /** The user-supplied escape-hatch transition: typed frame in, raw call, typed result out. */
    private static TransitionAction callRawAction() {
        return ctx -> {
            VadFrame frame = ctx.input(VAD_FRAME);
            ctx.output(VAD_RESULT, new VadResult(frame.sessionId(), callRawApi(frame.audio())));
            return CompletableFuture.completedFuture(null);
        };
    }
}
