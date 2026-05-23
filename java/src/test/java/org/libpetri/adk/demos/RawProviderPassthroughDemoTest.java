package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.colours.AdkColours.RawProviderEvent;
import org.libpetri.adk.colours.AdkColours.RawProviderRequest;
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
 * <p>The shipped library contributes exactly <i>one</i> thing here: the opaque
 * boundary colour pair {@link AdkColours#RAW_PROVIDER_REQUEST} /
 * {@link AdkColours#RAW_PROVIDER_EVENT}. The transition that calls the raw API is
 * <b>user code at the call site</b> — there is no stock subnet and no SPI.
 * This test <i>is</i> that ~15-line transition.
 *
 * <h2>The pattern</h2>
 * <pre>
 *   [RAW_PROVIDER_REQUEST]env --T_CallRaw--> [RAW_PROVIDER_EVENT]
 *       action calls the raw genai/transport API directly with the opaque payload
 * </pre>
 *
 * <p>In production {@code RAW_PROVIDER_REQUEST} is an env place injected via
 * {@code runner.inject(AdkColours.RAW_PROVIDER_REQUEST, Token.of(req))} the moment
 * the application needs a not-yet-modelled feature; downstream transitions read
 * {@code RAW_PROVIDER_EVENT} and fold the result back into the typed flow. Here we
 * seed the initial marking and run to quiescence to keep the example deterministic.
 */
class RawProviderPassthroughDemoTest {

    /**
     * Stand-in for the raw provider call the typed SDK doesn't expose yet — e.g.
     * a brand-new Live-API control frame or an experimental {@code generateContent}
     * config field. In real code this is a direct call into the genai SDK / a raw
     * HTTP request; here it just transforms the payload so the test can assert the
     * round-trip.
     */
    private static String callRawApi(String feature, Object payload) {
        return "raw[" + feature + "]:" + payload;
    }

    @Test
    void raw_request_is_routed_through_a_user_transition_to_a_raw_event() {
        var net = PetriNet.builder("raw-passthrough")
                .place(AdkColours.RAW_PROVIDER_REQUEST)
                .place(AdkColours.RAW_PROVIDER_EVENT)
                .transition(Transition.builder("T_CallRaw")
                        .inputs(Arc.In.one(AdkColours.RAW_PROVIDER_REQUEST))
                        .outputs(Arc.Out.place(AdkColours.RAW_PROVIDER_EVENT))
                        .build())
                .build()
                .bindActions(Map.of("T_CallRaw", callRawAction()));

        var initial = new LinkedHashMap<Place<?>, List<Token<?>>>();
        initial.put(AdkColours.RAW_PROVIDER_REQUEST, List.of(
                Token.of(new RawProviderRequest("live.experimentalVad", "frame-7"))));

        Marking quiescent = BitmapNetExecutor.builder(net, initial)
                .eventStore(EventStore.inMemory())
                .build()
                .run();

        var events = quiescent.peekTokens(AdkColours.RAW_PROVIDER_EVENT);
        assertThat(events).hasSize(1);
        RawProviderEvent event = (RawProviderEvent) events.iterator().next().value();
        assertThat(event.feature()).isEqualTo("live.experimentalVad");
        assertThat(event.payload()).isEqualTo("raw[live.experimentalVad]:frame-7");
        // The request token was consumed — no leftover on the inbound boundary.
        assertThat(quiescent.peekTokens(AdkColours.RAW_PROVIDER_REQUEST)).isEmpty();
    }

    /** The user-supplied escape-hatch transition: opaque payload in, raw call, opaque result out. */
    private static TransitionAction callRawAction() {
        return ctx -> {
            RawProviderRequest req = ctx.input(AdkColours.RAW_PROVIDER_REQUEST);
            Object result = callRawApi(req.feature(), req.payload());
            ctx.output(AdkColours.RAW_PROVIDER_EVENT, new RawProviderEvent(req.feature(), result));
            return CompletableFuture.completedFuture(null);
        };
    }
}
