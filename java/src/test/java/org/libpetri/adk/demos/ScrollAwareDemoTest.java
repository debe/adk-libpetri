package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.RunConfig;
import com.google.adk.runner.InMemoryRunner;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.CompletableFuture;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.runner.PetriAgent;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.adk.runner.SessionExecutorRegistry;
import org.libpetri.adk.runner.SessionKey;
import com.google.adk.events.Event;

/**
 * End-to-end demo of the integration story for <b>non-{@link Content}
 * external signals</b>: a UI-style scroll event is injected onto a
 * dedicated typed env place from a separate thread, the running net
 * records it, and an ADK {@code Runner.runAsync} invocation reads the
 * recorded scroll count when emitting its response Event.
 *
 * <h2>What this demo proves</h2>
 * <ol>
 *   <li>The reshaped {@link PetriRunner.Builder#environmentPlace}
 *       lets an ADK-integrated runner declare <b>any number of typed
 *       env places</b> beyond {@link AdkColours#USER_IN}. Here we add
 *       a {@code Place<Scroll> SCROLL_IN}.</li>
 *   <li>The runner exposes a uniform {@link PetriRunner#inject(Place,
 *       Object)} surface — a non-ADK thread looks up the per-session
 *       runner via {@link SessionExecutorRegistry#get(SessionKey)} and
 *       injects without going through the ADK adapter.</li>
 *   <li>The ADK egress
 *       (`{@code runner.adkEvents()}` watching {@code EVENT_OUT})
 *       continues to work normally — it is the named ADK-contract
 *       bridge, not a generic observation API. Side-effects belong in
 *       transition actions; observability belongs in the EventStore
 *       chain.</li>
 * </ol>
 *
 * <h2>Topology</h2>
 * <pre>
 *   [SCROLL_IN] ---T_RecordScroll--> [SCROLL_COUNT]   (consume+produce: +1)
 *           input(SCROLL_COUNT) ---/
 *
 *   [USER_IN]  ---T_Echo----------> [EVENT_OUT]      (read SCROLL_COUNT)
 *           read(SCROLL_COUNT)  ---/
 * </pre>
 * The {@code SCROLL_COUNT} place holds exactly one token at all times
 * (seeded at {@code 0L} via initial marking; {@code T_RecordScroll}
 * consumes the old count and produces the incremented one).
 */
class ScrollAwareDemoTest {

    /** UI scroll event — the kind of non-ADK external signal the
     *  integration must accept. Any record type works; this one is
     *  deliberately trivial. */
    public record Scroll(int dx, int dy) {}

    /** External ingress for {@link Scroll} events. */
    private static final Place<Scroll> SCROLL_IN =
            Place.of("scrollIn", Scroll.class);

    /** In-net accumulator — the marking is the state. */
    private static final Place<Long> SCROLL_COUNT =
            Place.of("scrollCount", Long.class);

    private static final String T_RECORD_SCROLL = "Scroll_Record";
    private static final String T_ECHO          = "Scroll_Echo";

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    @Test
    void scroll_events_injected_from_a_separate_thread_are_visible_in_the_adk_response()
            throws Exception {
        // ============================================================
        // 1. Build the scroll-aware net.
        // ============================================================
        var net = PetriNet.builder("scroll-aware")
                .place(AdkColours.USER_IN)
                .place(AdkColours.EVENT_OUT)
                .place(SCROLL_IN)
                .place(SCROLL_COUNT)
                .transition(Transition.builder(T_RECORD_SCROLL)
                        .inputs(Arc.In.one(SCROLL_IN), Arc.In.one(SCROLL_COUNT))
                        .outputs(Arc.Out.place(SCROLL_COUNT))
                        .build())
                .transition(Transition.builder(T_ECHO)
                        .inputs(Arc.In.one(AdkColours.USER_IN))
                        .read(SCROLL_COUNT)
                        .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                        .build())
                .build()
                .bindActions(Map.of(
                        T_RECORD_SCROLL, recordScrollAction(),
                        T_ECHO,          echoAction()));

        // ============================================================
        // 2. Wire the ADK-integrated runner with TWO env places.
        // ============================================================
        var registry = new SessionExecutorRegistry();
        ConcurrentMap<SessionKey, Object> sessionOwners = new ConcurrentHashMap<>();

        var agent = PetriAgent.of(
                "scroll_aware_agent",
                "Echoes user message + recorded scroll count",
                registry,
                key -> PetriRunner.builder(net)
                        .environmentPlace(AdkColours.USER_IN)
                        .environmentPlace(SCROLL_IN)
                        .initialMarking(Map.of(
                                SCROLL_COUNT, List.of(Token.of(0L))))
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start(),
                ctx -> sessionOwners.computeIfAbsent(
                        SessionKey.from(ctx.session()), k -> new Object()));

        var adkRunner = new InMemoryRunner(agent);
        var session = adkRunner.sessionService()
                .createSession(adkRunner.appName(), "user-1", (Map<String, Object>) null, "session-1")
                .blockingGet();

        // ============================================================
        // 3. Force the per-session runner to be created BEFORE we
        //    inject scrolls — otherwise registry.get(key) is null
        //    (no active runner yet). The realistic application pattern
        //    is identical: the session-start path (e.g. websocket open)
        //    drives runner creation before any side-channel inject.
        //
        //    We do this by initiating one no-op ADK invocation first,
        //    or — more directly — by calling getOrCreate on the
        //    registry the way PetriAgent does. The dedicated init step
        //    is what an HTTP handler would also do.
        // ============================================================
        var sessionKey = SessionKey.from(session);
        Object owner = sessionOwners.computeIfAbsent(sessionKey, k -> new Object());
        registry.getOrCreate(sessionKey, owner,
                key -> PetriRunner.builder(net)
                        .environmentPlace(AdkColours.USER_IN)
                        .environmentPlace(SCROLL_IN)
                        .initialMarking(Map.of(
                                SCROLL_COUNT, List.of(Token.of(0L))))
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start());

        // ============================================================
        // 4. Inject scrolls from a separate (non-ADK) thread, BEFORE
        //    the ADK turn. This is the load-bearing demonstration:
        //    arbitrary external signals reach the running net through
        //    the same env-place injection model as USER_IN.
        // ============================================================
        var scrollerDone = CompletableFuture.runAsync(() -> {
            for (int i = 0; i < 5; i++) {
                registry.get(sessionKey)
                        .inject(SCROLL_IN, new Scroll(0, 40))
                        .join();
            }
        }, Executors.newSingleThreadExecutor());
        scrollerDone.get(2, TimeUnit.SECONDS);

        // Wait until the orchestrator has fired T_RecordScroll for every
        // injected scroll. Inject acceptance only guarantees the token
        // is in the env place; we need the recording transition to have
        // completed for SCROLL_COUNT to reflect the new total before
        // T_Echo reads it.
        awaitQuiescent(registry.get(sessionKey), 2_000);

        // ============================================================
        // 5. Drive an ADK turn. T_Echo reads SCROLL_COUNT and emits
        //    an Event reflecting the recorded scrolls.
        // ============================================================
        var events = adkRunner.runAsync(
                        session.userId(),
                        session.id(),
                        userMessage("hello"),
                        RunConfig.builder().build())
                .toList().blockingGet();

        var lastFromAgent = events.stream()
                .filter(e -> "scroll_aware_agent".equals(e.author()))
                .reduce((first, second) -> second)
                .orElseThrow();
        assertThat(lastFromAgent.content().get().text())
                .isEqualTo("you scrolled 5 times; you said: hello");

        // Ensure the registry shuts down cleanly via owner GC path.
        registry.closeAll();
    }

    // ============================================================
    //  Action bindings
    // ============================================================

    private static TransitionAction recordScrollAction() {
        return ctx -> {
            ctx.input(SCROLL_IN);                       // consume the scroll event
            long current = ctx.input(SCROLL_COUNT);     // consume the running count
            ctx.output(SCROLL_COUNT, current + 1L);     // produce incremented count
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction echoAction() {
        return ctx -> {
            Content userContent = ctx.input(AdkColours.USER_IN);
            long count = ctx.read(SCROLL_COUNT);
            String text = "you scrolled " + count + " times; you said: " + userContent.text();
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .invocationId("scroll-demo")
                    .author("scroll_aware_agent")
                    .content(Content.builder()
                            .role("model")
                            .parts(List.of(Part.fromText(text)))
                            .build())
                    .build());
            return CompletableFuture.completedFuture(null);
        };
    }

    @SuppressWarnings("BusyWait")
    private static void awaitQuiescent(PetriRunner runner, long timeoutMillis) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (System.currentTimeMillis() < deadline) {
            if (runner.executor().isQuiescent() && runner.executor().inFlightCount() == 0) {
                return;
            }
            Thread.sleep(10);
        }
        throw new AssertionError("Runner did not reach quiescence within " + timeoutMillis + "ms");
    }

    private static Content userMessage(String text) {
        return Content.builder()
                .role("user")
                .parts(List.of(Part.fromText(text)))
                .build();
    }
}
