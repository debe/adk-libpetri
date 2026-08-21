package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.runner.InMemoryRunner;
import com.google.genai.types.Blob;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import com.microsoft.z3.Context;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.reactivex.rxjava3.core.Completable;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.disposables.Disposable;
import io.reactivex.rxjava3.processors.PublishProcessor;
import io.reactivex.rxjava3.subscribers.TestSubscriber;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.demos.voice.BargeInSubnet;
import org.libpetri.adk.demos.voice.LiveApiRecoverySubnet;
import org.libpetri.adk.subnet.LlmStreamingStepSubnet;
import org.libpetri.adk.subnet.RouterSubnet;
import org.libpetri.adk.runner.PetriAgent;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.adk.runner.SessionExecutorRegistry;
import org.libpetri.adk.runner.SessionKey;
import org.libpetri.core.Arc;
import org.libpetri.core.EnvironmentPlace;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.Transition;
import org.libpetri.adk.bridge.OtelEventStore;
import org.libpetri.adk.verify.AdkNetInvariants;
import org.libpetri.core.TransitionAction;
import org.libpetri.event.EventStore;
import org.libpetri.runtime.PetriNetExecutor;
import org.libpetri.smt.SmtProperty;
import org.libpetri.smt.SmtVerifier;

/**
 * BIDI / Live-API voice-session demo — composes the streaming subnet,
 * barge-in subnet, and silence-recovery subnet into one long-lived
 * per-user net driven through the <b>stock ADK {@code Runner}</b> via
 * the {@link PetriAgent} adapter.
 *
 * <h2>What this demo shows</h2>
 * <ol>
 *   <li><b>Multi-env-place injection through the ADK adapter</b> — the
 *       {@link PetriRunner} declares six typed env places ({@link
 *       AdkColours#USER_IN} for ADK ingress, plus five voice signals).
 *       Side-channel signals (voice activity, interrupt, response-awaited,
 *       model-active) inject via {@link
 *       PetriRunner#inject(Place, Object)} from any thread; the user
 *       utterance arrives through {@link InMemoryRunner#runAsync}.</li>
 *   <li><b>Per-chunk env-place injection</b> —
 *       {@link LlmStreamingStepSubnet} injects each partial chunk
 *       individually via {@code executor.inject}; subscribers on
 *       {@link PetriRunner#adkEvents()} see partial Events as they
 *       arrive, not at the end. The streaming subnet's executor ref is
 *       typed against {@link PetriNetExecutor}, so it works with any
 *       executor impl the runner picks.</li>
 *   <li><b>Marking-bounded budget</b> — {@code CHUNK_BUDGET} stays
 *       pinned at K via the consume-and-return invariant on the
 *       streaming emit transition.</li>
 *   <li><b>Barge-in via inhibitor/read pair</b> — when the user starts
 *       speaking mid-response, exactly one of two competing transitions
 *       fires (send vs discard), driven by a single shared
 *       {@code VOICE_ACTIVITY_OPEN} place.</li>
 *   <li><b>Two-stage silence recovery</b> —
 *       {@link LiveApiRecoverySubnet} runs Nudge after 80ms of silence,
 *       Reconnect 80ms after that if the model stays silent.</li>
 *   <li><b>{@code T_StartStream}</b> — a tiny in-net transition that
 *       consumes ADK's {@code USER_IN} and seeds the streaming subnet's
 *       {@code LLM_REQUEST}. Demonstrates the standard recipe for
 *       turning an ADK turn into a request token for a downstream
 *       subnet without bypassing the env-place contract.</li>
 * </ol>
 */
class VoiceSessionDemoTest {

    private static final LiveApiRecoverySubnet.Config FAST_RECOVERY =
            new LiveApiRecoverySubnet.Config(Duration.ofMillis(80), Duration.ofMillis(80));

    private static final String T_START_STREAM = "VoiceDemo_StartStream";

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    static boolean z3Available() {
        try {
            new Context().close();
            return true;
        } catch (UnsatisfiedLinkError | NoClassDefFoundError _) {
            return false;
        }
    }

    @Test
    void adk_driven_voice_session_streams_partials_handles_barge_in_and_recovers_silence()
            throws Exception {
        // ============================================================
        //  1. Compose the BIDI net + T_StartStream (USER_IN → LLM_REQUEST).
        // ============================================================
        var execRef = new AtomicReference<PetriNetExecutor>();
        var streamingConfig = LlmStreamingStepSubnet.Config.builder("voice_agent")
                .chunkBudget(4)
                .executorRef(execRef)
                .build();
        var recoveryDef = LiveApiRecoverySubnet.def(FAST_RECOVERY);

        var startStream = Transition.builder(T_START_STREAM)
                .inputs(Arc.In.one(AdkColours.USER_IN))
                .outputs(Arc.Out.place(AdkColours.LLM_REQUEST))
                .build();

        var chunks = List.of(
                chunkResponse("Sure, "),
                chunkResponse("the answer "),
                chunkResponse("is 42."));
        var llm = streamingLlm(chunks);

        var net = PetriNet.builder("voice-session")
                .compose(LlmStreamingStepSubnet.DEF)
                .compose(RouterSubnet.DEF)
                .compose(BargeInSubnet.DEF)
                .compose(recoveryDef)
                .transition(startStream)
                .build();

        Map<String, TransitionAction> allBindings = new LinkedHashMap<>();
        allBindings.putAll(LlmStreamingStepSubnet.actionBindings(llm, streamingConfig));
        allBindings.putAll(RouterSubnet.actionBindings(RouterSubnet.Config.of("voice_agent")));
        allBindings.putAll(BargeInSubnet.actionBindings());
        allBindings.putAll(LiveApiRecoverySubnet.actionBindings(FAST_RECOVERY));
        allBindings.put(T_START_STREAM, ctx -> {
            Content userContent = ctx.input(AdkColours.USER_IN);
            ctx.output(AdkColours.LLM_REQUEST, requestFor(userContent));
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        });
        var bound = net.bindActions(allBindings);

        // ============================================================
        //  2. Wire the ADK-integrated runner with SIX typed env places —
        //     one for the ADK utterance, five for voice signals.
        // ============================================================
        var registry = SessionExecutorRegistry.cleanerOwned();
        ConcurrentMap<SessionKey, Object> sessionOwners = new ConcurrentHashMap<>();

        var agent = PetriAgent.of(
                "voice_agent",
                "BIDI voice agent — streaming, barge-in, silence recovery",
                registry,
                key -> PetriRunner.builder(bound)
                        .environmentPlace(AdkColours.USER_IN)
                        .environmentPlace(LlmStreamingStepSubnet.Places.CHUNK)
                        .environmentPlace(BargeInSubnet.Places.INTERRUPTED)
                        .environmentPlace(BargeInSubnet.Places.VOICE_ACTIVITY_OPEN)
                        .environmentPlace(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED)
                        .environmentPlace(LiveApiRecoverySubnet.Places.MODEL_ACTIVE)
                        .deferredExecutorRef(execRef)
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
        //  3. Force per-session runner creation BEFORE side-channel
        //     injects — the realistic application pattern (websocket
        //     open creates the runner before any signal injects).
        //     Populates execRef so the streaming subnet can inject per
        //     chunk from inside its T_LlmCallStream action.
        // ============================================================
        var sessionKey = SessionKey.from(session);
        Object owner = sessionOwners.computeIfAbsent(sessionKey, k -> new Object());
        var runner = registry.getOrCreate(sessionKey, owner,
                key -> PetriRunner.builder(bound)
                        .environmentPlace(AdkColours.USER_IN)
                        .environmentPlace(LlmStreamingStepSubnet.Places.CHUNK)
                        .environmentPlace(BargeInSubnet.Places.INTERRUPTED)
                        .environmentPlace(BargeInSubnet.Places.VOICE_ACTIVITY_OPEN)
                        .environmentPlace(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED)
                        .environmentPlace(LiveApiRecoverySubnet.Places.MODEL_ACTIVE)
                        .deferredExecutorRef(execRef)
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start());

        // ============================================================
        //  4. Subscribe to ALL partials on the runner's egress Flowable
        //     BEFORE driving the ADK turn (the Runner's take(1) only
        //     surfaces the first event back through runAsync — the rest
        //     still land on EVENT_OUT and are visible via adkEvents()).
        // ============================================================
        TestSubscriber<Event> egress = runner.adkEvents().test();

        // ============================================================
        //  5. Side-channel: the user's voice window opens *before* the
        //     utterance arrives. This is what an audio frontend would
        //     signal when it detects voice activity. Void-typed env
        //     places go through runner.executor() since PetriRunner's
        //     inject() rejects null tokens at the public surface.
        // ============================================================
        injectVoid(runner, BargeInSubnet.Places.VOICE_ACTIVITY_OPEN);

        // ============================================================
        //  6. Drive an ADK turn. PetriAgent injects USER_IN; T_StartStream
        //     converts it to LLM_REQUEST; LlmStreamingStepSubnet kicks
        //     off streaming. RouterSubnet turns the merged LLM_RESPONSE into
        //     the terminal Event that completes the non-SSE runAsync call;
        //     partials continue to flow to EVENT_OUT and are observed through
        //     the direct runner subscription below.
        // ============================================================
        var firstTurnEvents = adkRunner.runAsync(
                        session.userId(),
                        session.id(),
                        userMessage("what's 6*7"),
                        RunConfig.builder().build())
                .toList().blockingGet();

        var terminalAgentEvent = firstTurnEvents.stream()
                .filter(e -> "voice_agent".equals(e.author()))
                .findFirst()
                .orElseThrow();
        assertThat(terminalAgentEvent.partial().orElse(false)).isFalse();
        assertThat(terminalAgentEvent.content().get().text())
                .isEqualTo("Sure, the answer is 42.");

        // ============================================================
        //  7. Mid/post-stream side-channel signals: a barge-in interrupt
        //     while the voice window is still open, and a response-
        //     awaited signal that the silence-recovery subnet times out
        //     into Nudge → Reconnect.
        // ============================================================
        injectVoid(runner, BargeInSubnet.Places.INTERRUPTED);
        injectVoid(runner, LiveApiRecoverySubnet.Places.RESPONSE_AWAITED);

        // Wait past the chained timed deadlines (nudge 80ms + reconnect 80ms),
        // then poll for quiescence. The leading sleep avoids a race where
        // env-place injections have been accepted but not yet propagated
        // into the in-net marking — without it, the first quiescent-poll
        // observes enabledCount==0 transiently before the timed
        // transitions have a chance to enable.
        Thread.sleep(300);
        awaitQuiescent(runner, 2_000);

        // ============================================================
        //  8. Verify the structural outcomes — partials seen on the ADK
        //     egress, marking-level facts on the runner's executor.
        // ============================================================
        var partialCount = egress.values().stream()
                .filter(e -> e.partial().orElse(false))
                .count();
        assertThat(partialCount).isEqualTo(3L);

        var finalMarking = runner.executor().marking();

        // Barge-in routed to BARGE_IN_SENT (voice window was open).
        assertThat(finalMarking.peekTokens(BargeInSubnet.Places.BARGE_IN_SENT))
                .hasSize(1);
        assertThat(finalMarking.peekTokens(BargeInSubnet.Places.INTERRUPT_DISCARDED))
                .isEmpty();

        // Silence recovery fired both stages.
        assertThat(finalMarking.peekTokens(LiveApiRecoverySubnet.Places.NUDGE_NEEDED))
                .isNotEmpty();
        assertThat(finalMarking.peekTokens(LiveApiRecoverySubnet.Places.RECONNECT_NEEDED))
                .isNotEmpty();

        // Budget invariant: CHUNK_BUDGET back at K after all emits.
        assertThat(finalMarking.peekTokens(LlmStreamingStepSubnet.Places.CHUNK_BUDGET))
                .hasSize(4);

        registry.closeAll();
    }

    // ============================================================
    //  BIDI demo: a custom BaseLlmConnection bridges the Live API into
    //  the libpetri net via env-place injection. The connection's send
    //  side is invoked from a transition action; its receive() Flowable
    //  is pumped frame-by-frame into an LLM_RESPONSE env place by the
    //  application layer — the live connection and its bridge live in
    //  application code, without baking a custom agent into the library.
    // ============================================================

    /**
     * Turn edge the application signals from the transport's turn-complete frame.
     * The net, not the bridge, turns it into the terminal {@code Event}.
     */
    private static final Place<Void> BIDI_TURN_COMPLETE =
            Place.of("bidiDemo_turnComplete", Void.class);

    @Test
    void bidi_voice_via_baselllmconnection_bridges_frames_through_net() throws Exception {
        // ============================================================
        //  0. OTel session-long root span. Unlike the per-invocation
        //     pattern PetriAgent uses for runAsync, BIDI sessions are
        //     long-lived and have side-channel transitions (BargeIn etc.)
        //     firing between turns — those need a parent too. The
        //     application opens ONE rootSpan for the whole BIDI session
        //     and binds it to OtelEventStore for the runner's lifetime.
        //     Every transition span (invocation-driven OR background)
        //     attaches as a child.
        // ============================================================
        var exporter = InMemorySpanExporter.create();
        var tracerProvider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter))
                .build();
        Tracer tracer = OpenTelemetrySdk.builder()
                .setTracerProvider(tracerProvider)
                .build()
                .getTracer("voice-bidi-demo");
        Span sessionRootSpan = tracer.spanBuilder("VoiceSession")
                .setAttribute("session.kind", "bidi")
                .startSpan();
        var otelEventStore = new OtelEventStore(tracer, EventStore.logging());
        AutoCloseable sessionBinding = otelEventStore.bindInvocationContext(
                io.opentelemetry.context.Context.current().with(sessionRootSpan));

        // ============================================================
        //  1. The "Gemini Live" connection mock — a BaseLlmConnection that
        //     records sends and exposes a PublishProcessor we drive from
        //     the test as if it were the model talking back.
        // ============================================================
        var connection = new MockLiveConnection();

        // ============================================================
        //  2. Net topology — two custom transitions that bridge env places
        //     against the connection's send/receive surfaces, composed with
        //     stock BargeIn for the voice-feel demo.
        //
        //     LLM_REQUEST (env, Content from app)
        //       --> Bidi_SendToConnection (action: connection.sendContent)
        //     LLM_RESPONSE (env, LlmResponse pumped from connection.receive())
        //       --> Bidi_RouteResponse (action: build partial ADK Event) --> EVENT_OUT
        //     TURN_COMPLETE (env, Void signalled by the app on the turn edge)
        //       --> Bidi_EmitTurnEnd (action: build terminal ADK Event)  --> EVENT_OUT
        //       inhibited by LLM_RESPONSE: no terminal while chunks are queued
        //
        //     The turn shape is the NET's: `partial` and `turnComplete` are set by
        //     whichever transition fired, not by the transport bridge (which authors
        //     no events at all; see BidiPetriAgent.bridge). So suppressing,
        //     coalescing, cancelling or ORDERING chunks is a marking-level decision.
        //     The inhibitor is that last one: egress order is an arc, not a rule the
        //     application layer has to remember to follow.
        //
        //     BargeIn observes VOICE_ACTIVITY_OPEN and INTERRUPTED env places
        //     and routes barge-in events to BARGE_IN_SENT vs INTERRUPT_DISCARDED.
        // ============================================================
        var sendToConnection = Transition.builder("Bidi_SendToConnection")
                .inputs(Arc.In.one(AdkColours.LLM_REQUEST))
                .build();
        var routeResponse = Transition.builder("Bidi_RouteResponse")
                .inputs(Arc.In.one(AdkColours.LLM_RESPONSE))
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();
        var emitTurnEnd = Transition.builder("Bidi_EmitTurnEnd")
                .inputs(Arc.In.one(BIDI_TURN_COMPLETE))
                // Orders egress structurally: the terminal cannot fire while response
                // chunks are still queued, so no application-side await is needed.
                .inhibitor(AdkColours.LLM_RESPONSE)
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();

        var net = PetriNet.builder("bidi-bridge")
                .compose(BargeInSubnet.DEF)
                .transition(sendToConnection)
                .transition(routeResponse)
                .transition(emitTurnEnd)
                .build();

        Map<String, TransitionAction> bindings = new LinkedHashMap<>();
        bindings.putAll(BargeInSubnet.actionBindings());
        bindings.put("Bidi_SendToConnection", ctx -> {
            LlmRequest request = ctx.input(AdkColours.LLM_REQUEST);
            Content userContent = request.contents().get(0);
            return connection.sendContent(userContent)
                    .toCompletionStage(null)
                    .toCompletableFuture()
                    .thenApply(v -> null);
        });
        bindings.put("Bidi_RouteResponse", ctx -> {
            LlmResponse resp = ctx.input(AdkColours.LLM_RESPONSE);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .invocationId("bidi-1")
                    .author("bidi_agent")
                    .content(resp.content().orElse(Content.builder().build()))
                    .partial(true)
                    .build());
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        });
        bindings.put("Bidi_EmitTurnEnd", ctx -> {
            ctx.input(BIDI_TURN_COMPLETE);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .invocationId("bidi-1")
                    .author("bidi_agent")
                    .partial(false)
                    .turnComplete(true)
                    .build());
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        });
        var bound = net.bindActions(bindings);

        // ============================================================
        //  3. Runner + agent. The connection lives outside the net; the
        //     test wires connection.receive() → runner.inject(LLM_RESPONSE)
        //     explicitly (the "application layer" bridge). In a real BIDI
        //     deployment the WebSocket handler does this wiring once per
        //     session — same pattern.
        // ============================================================
        var registry = SessionExecutorRegistry.cleanerOwned();
        ConcurrentMap<SessionKey, Object> sessionOwners = new ConcurrentHashMap<>();
        var agent = PetriAgent.of(
                "bidi_agent",
                "BIDI bridge demo via custom BaseLlmConnection",
                registry,
                key -> PetriRunner.builder(bound)
                        .environmentPlace(AdkColours.USER_IN)
                        .environmentPlace(AdkColours.LLM_REQUEST)
                        .environmentPlace(AdkColours.LLM_RESPONSE)
                        .environmentPlace(BargeInSubnet.Places.INTERRUPTED)
                        .environmentPlace(BargeInSubnet.Places.VOICE_ACTIVITY_OPEN)
                        .environmentPlace(BIDI_TURN_COMPLETE)
                        .eventStore(otelEventStore)
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start(),
                ctx -> sessionOwners.computeIfAbsent(
                        SessionKey.from(ctx.session()), k -> new Object()));

        var adkRunner = new InMemoryRunner(agent);
        var session = adkRunner.sessionService()
                .createSession(adkRunner.appName(), "u", (Map<String, Object>) null, "s").blockingGet();

        var sessionKey = SessionKey.from(session);
        Object owner = sessionOwners.computeIfAbsent(sessionKey, k -> new Object());
        var runner = registry.getOrCreate(sessionKey, owner,
                key -> PetriRunner.builder(bound)
                        .environmentPlace(AdkColours.USER_IN)
                        .environmentPlace(AdkColours.LLM_REQUEST)
                        .environmentPlace(AdkColours.LLM_RESPONSE)
                        .environmentPlace(BargeInSubnet.Places.INTERRUPTED)
                        .environmentPlace(BargeInSubnet.Places.VOICE_ACTIVITY_OPEN)
                        .environmentPlace(BIDI_TURN_COMPLETE)
                        .eventStore(otelEventStore)
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start());

        // The application-layer bridge from connection.receive() into the net.
        Disposable receivePump = connection.receive()
                .subscribe(frame -> runner.inject(AdkColours.LLM_RESPONSE, frame));

        // Observe net egress as the BIDI client would, via the runLive Flowable
        // surface (PetriAgent.runLiveImpl returns runner.adkEvents() directly).
        TestSubscriber<Event> egress = runner.adkEvents().test();

        // ============================================================
        //  4. Drive the BIDI loop. The application sends a user content
        //     to the LLM via the net (LLM_REQUEST env). The transition
        //     forwards to the connection. Then we simulate two response
        //     frames from "Gemini" arriving on the connection's receive
        //     Flowable.
        // ============================================================
        runner.inject(AdkColours.LLM_REQUEST, LlmRequest.builder()
                        .model("gemini-live")
                        .contents(List.of(userMessage("what's the weather")))
                        .build())
                .get(1, java.util.concurrent.TimeUnit.SECONDS);
        // Open voice window and simulate a barge-in to exercise the BargeIn subnet.
        injectVoid(runner, BargeInSubnet.Places.VOICE_ACTIVITY_OPEN);

        connection.pushResponse(LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText("Sunny, "))).build())
                .build());
        connection.pushResponse(LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText("with light wind."))).build())
                .build());
        // Signalled straight after the chunks, with nothing awaited in between. The
        // receive pump's inject is asynchronous and fire-and-forget, so both chunks may
        // still be sitting in LLM_RESPONSE at this point. Bidi_EmitTurnEnd's inhibitor
        // on LLM_RESPONSE is what keeps the terminal behind them.
        injectVoid(runner, BIDI_TURN_COMPLETE);
        injectVoid(runner, BargeInSubnet.Places.INTERRUPTED);

        awaitQuiescent(runner, 2_000);

        // ============================================================
        //  5. Assert: the send went to the connection; two responses
        //     surfaced as ADK partial Events followed by one net-authored
        //     terminal event; the barge-in routed to BARGE_IN_SENT (voice
        //     window was open).
        // ============================================================
        assertThat(connection.sentContents).hasSize(1);
        assertThat(connection.sentContents.get(0).text()).isEqualTo("what's the weather");

        var agentEvents = egress.values().stream()
                .filter(e -> "bidi_agent".equals(e.author()))
                .toList();
        assertThat(agentEvents).hasSize(3);
        assertThat(agentEvents.get(0).content().get().text()).isEqualTo("Sunny, ");
        assertThat(agentEvents.get(0).partial()).hasValue(true);
        assertThat(agentEvents.get(1).content().get().text()).isEqualTo("with light wind.");
        assertThat(agentEvents.get(1).partial()).hasValue(true);

        // The turn boundary is the net's call: Bidi_EmitTurnEnd authored it, not the
        // transport bridge. ADK's runLive consumers can now tell partials from finals.
        assertThat(agentEvents.get(2).partial()).hasValue(false);
        assertThat(agentEvents.get(2).turnComplete()).hasValue(true);

        var finalMarking = runner.executor().marking();
        assertThat(finalMarking.peekTokens(BargeInSubnet.Places.BARGE_IN_SENT)).hasSize(1);

        // ============================================================
        //  6. OTel session-long parent-linkage assertion. Every transition
        //     span (including BargeIn's, which fires from a background
        //     env-place injection with no ADK invocation context) must be
        //     a child of the VoiceSession rootSpan — proving the
        //     session-long pattern covers both invocation-driven AND
        //     background side-channel transitions.
        // ============================================================
        receivePump.dispose();
        connection.close();
        registry.closeAll();
        sessionBinding.close();
        sessionRootSpan.end();
        tracerProvider.forceFlush().join(2, java.util.concurrent.TimeUnit.SECONDS);

        var spans = exporter.getFinishedSpanItems();
        var rootSpans = spans.stream()
                .filter(s -> s.getName().equals("VoiceSession")).toList();
        assertThat(rootSpans).hasSize(1);
        var rootSpanId = rootSpans.get(0).getSpanId();
        var transitionSpans = spans.stream()
                .filter(s -> !s.getName().equals("VoiceSession"))
                .toList();
        assertThat(transitionSpans).isNotEmpty();
        for (var ts : transitionSpans) {
            assertThat(ts.getParentSpanId()).isEqualTo(rootSpanId);
        }
        tracerProvider.close();
    }

    // ============================================================
    //  Barge-in chunk drop: the property net-authored egress buys
    //  that a bridge-authored one cannot. When the transport bridge
    //  maps frames straight to ADK Events, model content never enters
    //  the marking, so no transition can reach it and a barge-in can
    //  only stop *future* chunks. With the content in a place, the
    //  already-queued backlog is a reset arc away.
    // ============================================================

    /** Observed: a barge-in wiped the queued turn. */
    private static final Place<Void> BIDI_CHUNKS_DROPPED =
            Place.of("bidiDemo_chunksDropped", Void.class);

    @Test
    void barge_in_structurally_drops_the_queued_model_chunks() throws Exception {
        // ============================================================
        //  Net: stock BargeIn decides whether an interrupt is a real
        //  barge-in, and the drop transition hangs off its verdict.
        //
        //    LLM_RESPONSE --Bidi_EmitChunk--> EVENT_OUT
        //                     o---[BARGE_IN_SENT]   stop emitting once
        //                                           barge-in is decided
        //    BARGE_IN_SENT --Bidi_DropQueuedTurn--> CHUNKS_DROPPED
        //                     reset(LLM_RESPONSE)   wipe the backlog
        // ============================================================
        var emitChunk = Transition.builder("Bidi_EmitChunk")
                .inputs(Arc.In.one(AdkColours.LLM_RESPONSE))
                .inhibitor(BargeInSubnet.Places.BARGE_IN_SENT)
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();
        var dropQueuedTurn = Transition.builder("Bidi_DropQueuedTurn")
                .inputs(Arc.In.one(BargeInSubnet.Places.BARGE_IN_SENT))
                .reset(AdkColours.LLM_RESPONSE)
                .outputs(Arc.Out.place(BIDI_CHUNKS_DROPPED))
                .build();

        var net = PetriNet.builder("barge-in-drop")
                .compose(BargeInSubnet.DEF)
                .place(BIDI_CHUNKS_DROPPED)
                .transition(emitChunk)
                .transition(dropQueuedTurn)
                .build();

        Map<String, TransitionAction> bindings = new LinkedHashMap<>(BargeInSubnet.actionBindings());
        bindings.put("Bidi_EmitChunk", ctx -> {
            LlmResponse resp = ctx.input(AdkColours.LLM_RESPONSE);
            // Emission costs something real (a socket write), which is exactly why a
            // backlog builds up in LLM_RESPONSE faster than it drains.
            try {
                Thread.sleep(5);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .invocationId("barge-1")
                    .author("bidi_agent")
                    .content(resp.content().orElse(Content.builder().build()))
                    .partial(true)
                    .build());
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        });
        bindings.put("Bidi_DropQueuedTurn", ctx -> {
            ctx.input(BargeInSubnet.Places.BARGE_IN_SENT);
            ctx.output(BIDI_CHUNKS_DROPPED, (Void) null);
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        });

        var runner = PetriRunner.builder(net.bindActions(bindings))
                .environmentPlace(AdkColours.LLM_RESPONSE)
                .environmentPlace(BargeInSubnet.Places.INTERRUPTED)
                .environmentPlace(BargeInSubnet.Places.VOICE_ACTIVITY_OPEN)
                .actionExecutor(EXECUTOR)
                .orchestratorExecutor(EXECUTOR)
                .start();

        try {
            TestSubscriber<Event> egress = runner.adkEvents().test();

            // The user is speaking, so the interrupt is a genuine barge-in.
            injectVoid(runner, BargeInSubnet.Places.VOICE_ACTIVITY_OPEN);

            // A model turn streams in faster than it can be emitted, then the user
            // cuts in. Everything still queued must never reach the client.
            int pushed = 8;
            for (int i = 0; i < pushed; i++) {
                runner.inject(AdkColours.LLM_RESPONSE, LlmResponse.builder()
                        .content(Content.builder().role("model")
                                .parts(List.of(Part.fromText("chunk " + i))).build())
                        .build());
            }
            injectVoid(runner, BargeInSubnet.Places.INTERRUPTED);

            // Wait on the property, not on quiescence. Acceptance completes inside the
            // orchestrator's external-event drain, before enablement is recomputed, so a
            // quiescence poll taken right after an inject can read the pre-injection
            // state and return early.
            awaitMarked(runner, BIDI_CHUNKS_DROPPED, 5_000);
            awaitQuiescent(runner, 5_000);

            var marking = runner.executor().marking();
            assertThat(marking.peekTokens(BIDI_CHUNKS_DROPPED)).hasSize(1);
            // The backlog is gone from the marking, not merely un-subscribed downstream.
            assertThat(marking.peekTokens(AdkColours.LLM_RESPONSE)).isEmpty();
            // And it never became an Event. This is the assertion the pre-1.3 merged
            // bridge could not have satisfied at any marking, because the chunks were
            // Events the moment they left the transport.
            assertThat(egress.values().size()).isLessThan(pushed);
        } finally {
            runner.shutdown();
        }
    }

    // ============================================================
    //  Reset-arc demo: each new utterance wipes the in-net intent
    //  place and seeds the new one, all through the ADK adapter and a
    //  dedicated typed env place for incoming utterance signals.
    // ============================================================

    /** Dedicated env place for utterance signals — separate from the ADK
     *  USER_IN ingress so the demo can prime intents before driving a
     *  turn, mirroring the multi-env-place pattern. */
    private static final Place<Content> UTTERANCE_IN =
            Place.of("voiceDemo_utteranceIn", Content.class);

    /** In-net derived intent — at most one token, wiped by the reset arc. */
    private static final Place<String> CURRENT_INTENT =
            Place.of("voiceDemo_currentIntent", String.class);

    private static final String T_ON_NEW_UTTERANCE = "VoiceDemo_OnNewUtterance";
    private static final String T_ECHO_INTENT      = "VoiceDemo_EchoIntent";

    @Test
    void new_utterance_resets_in_net_intent_state_through_adk_egress() throws Exception {
        // ============================================================
        //  Net: T_OnNewUtterance consumes from UTTERANCE_IN, resets
        //  CURRENT_INTENT (wipes any prior intent), seeds the new
        //  derived intent. T_EchoIntent consumes USER_IN, reads
        //  CURRENT_INTENT, emits the response on EVENT_OUT.
        // ============================================================
        var onNewUtterance = Transition.builder(T_ON_NEW_UTTERANCE)
                .inputs(Arc.In.one(UTTERANCE_IN))
                .reset(CURRENT_INTENT)
                .outputs(Arc.Out.place(CURRENT_INTENT))
                .build();

        var echoIntent = Transition.builder(T_ECHO_INTENT)
                .inputs(Arc.In.one(AdkColours.USER_IN))
                .read(CURRENT_INTENT)
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();

        var net = PetriNet.builder("reset-arc-demo")
                .place(AdkColours.USER_IN)
                .place(AdkColours.EVENT_OUT)
                .place(UTTERANCE_IN)
                .place(CURRENT_INTENT)
                .transition(onNewUtterance)
                .transition(echoIntent)
                .build()
                .bindActions(Map.of(
                        T_ON_NEW_UTTERANCE, onNewUtteranceAction(),
                        T_ECHO_INTENT,      echoIntentAction()));

        // ============================================================
        //  ADK wiring — two typed env places, one ADK runner.
        // ============================================================
        var registry = SessionExecutorRegistry.cleanerOwned();
        ConcurrentMap<SessionKey, Object> sessionOwners = new ConcurrentHashMap<>();

        var agent = PetriAgent.of(
                "reset_arc_agent",
                "Demonstrates reset-arc wipe of in-net intent state per utterance",
                registry,
                key -> PetriRunner.builder(net)
                        .environmentPlace(AdkColours.USER_IN)
                        .environmentPlace(UTTERANCE_IN)
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start(),
                ctx -> sessionOwners.computeIfAbsent(
                        SessionKey.from(ctx.session()), k -> new Object()));

        var adkRunner = new InMemoryRunner(agent);
        var session = adkRunner.sessionService()
                .createSession(adkRunner.appName(), "u", (Map<String, Object>) null, "s").blockingGet();

        var sessionKey = SessionKey.from(session);
        Object owner = sessionOwners.computeIfAbsent(sessionKey, k -> new Object());
        var runner = registry.getOrCreate(sessionKey, owner,
                key -> PetriRunner.builder(net)
                        .environmentPlace(AdkColours.USER_IN)
                        .environmentPlace(UTTERANCE_IN)
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start());

        // ============================================================
        //  Side-channel: two consecutive utterance events. The second
        //  triggers the reset arc, wiping the intent from the first.
        // ============================================================
        runner.inject(UTTERANCE_IN, userMessage("what time is it"))
                .get(1, java.util.concurrent.TimeUnit.SECONDS);
        runner.inject(UTTERANCE_IN, userMessage("tell me a joke"))
                .get(1, java.util.concurrent.TimeUnit.SECONDS);
        awaitQuiescent(runner, 1_000);

        // ============================================================
        //  ADK turn — T_EchoIntent reads the surviving CURRENT_INTENT.
        // ============================================================
        var events = adkRunner.runAsync(
                        session.userId(), session.id(),
                        userMessage("respond now"),
                        RunConfig.builder().build())
                .toList().blockingGet();

        var agentEvent = events.stream()
                .filter(e -> "reset_arc_agent".equals(e.author()))
                .findFirst()
                .orElseThrow();
        assertThat(agentEvent.content().get().text())
                .isEqualTo("intent-from:tell me a joke");

        // Exactly ONE intent token survives — the second utterance reset
        // the first, then deposited the new one. No stale leftover.
        var finalMarking = runner.executor().marking();
        var intents = finalMarking.peekTokens(CURRENT_INTENT).stream()
                .map(Token::value).toList();
        assertThat(intents).containsExactly("intent-from:tell me a joke");

        registry.closeAll();
    }

    private static TransitionAction onNewUtteranceAction() {
        return ctx -> {
            Content utterance = ctx.input(UTTERANCE_IN);
            ctx.output(CURRENT_INTENT, "intent-from:" + utterance.text());
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction echoIntentAction() {
        return ctx -> {
            ctx.input(AdkColours.USER_IN);
            String intent = ctx.read(CURRENT_INTENT);
            ctx.output(AdkColours.EVENT_OUT, Event.builder()
                    .invocationId("reset-arc-demo")
                    .author("reset_arc_agent")
                    .content(Content.builder().role("model")
                            .parts(List.of(Part.fromText(intent))).build())
                    .build());
            return java.util.concurrent.CompletableFuture.completedFuture(null);
        };
    }

    @Test
    @EnabledIf("z3Available")
    void composed_voice_demo_net_has_smt_checked_bounded_chunk_budget() {
        var recoveryDef = LiveApiRecoverySubnet.def(FAST_RECOVERY);
        var startStream = Transition.builder(T_START_STREAM)
                .inputs(Arc.In.one(AdkColours.USER_IN))
                .outputs(Arc.Out.place(AdkColours.LLM_REQUEST))
                .build();
        var net = PetriNet.builder("voice-dlf-check")
                .compose(LlmStreamingStepSubnet.DEF)
                .compose(BargeInSubnet.DEF)
                .compose(recoveryDef)
                .transition(startStream)
                .build();

        var userInEnv         = EnvironmentPlace.of(AdkColours.USER_IN);
        var chunkEnv          = EnvironmentPlace.of(LlmStreamingStepSubnet.Places.CHUNK);
        var interruptedEnv    = EnvironmentPlace.of(BargeInSubnet.Places.INTERRUPTED);
        var voiceOpenEnv      = EnvironmentPlace.of(BargeInSubnet.Places.VOICE_ACTIVITY_OPEN);
        var responseAwaitedEnv = EnvironmentPlace.of(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED);
        var modelActiveEnv    = EnvironmentPlace.of(LiveApiRecoverySubnet.Places.MODEL_ACTIVE);

        var result = SmtVerifier.forNet(net)
                .initialMarking(b -> b.tokens(AdkColours.USER_IN, 1))
                .environmentPlaces(userInEnv, chunkEnv, interruptedEnv, voiceOpenEnv,
                                   responseAwaitedEnv, modelActiveEnv)
                .sinkPlaces(
                        AdkColours.EVENT_OUT,
                        AdkColours.LLM_RESPONSE,
                        BargeInSubnet.Places.BARGE_IN_SENT,
                        BargeInSubnet.Places.INTERRUPT_DISCARDED,
                        LiveApiRecoverySubnet.Places.NUDGE_NEEDED,
                        LiveApiRecoverySubnet.Places.RECONNECT_NEEDED)
                .property(AdkNetInvariants.reaskBudgetIsBounded(
                        LlmStreamingStepSubnet.Places.CHUNK_BUDGET, 4))
                .verify();

        assertThat(result.isViolated()).isFalse();
    }

    // ============================================================
    //  Fixtures
    // ============================================================

    private static Content userMessage(String text) {
        return Content.builder().role("user")
                .parts(List.of(Part.fromText(text))).build();
    }

    private static LlmRequest requestFor(Content userContent) {
        return LlmRequest.builder()
                .model("fake")
                .contents(List.of(userContent))
                .build();
    }

    private static LlmResponse chunkResponse(String text) {
        return LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText(text))).build())
                .build();
    }

    private static BaseLlm streamingLlm(List<LlmResponse> chunks) {
        return new BaseLlm("streaming") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.fromIterable(chunks);
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    /**
     * Minimal {@link BaseLlmConnection} suitable for BIDI demos and tests.
     * Records all {@code send*} calls and exposes a {@link PublishProcessor}
     * the test drives via {@link #pushResponse} to simulate frames arriving
     * from the model side.
     */
    private static final class MockLiveConnection implements BaseLlmConnection {

        final List<Content> sentContents = new CopyOnWriteArrayList<>();
        final List<Blob> sentRealtime = new CopyOnWriteArrayList<>();
        final List<Content> sentHistory = new CopyOnWriteArrayList<>();
        private final PublishProcessor<LlmResponse> receiveProcessor = PublishProcessor.create();

        @Override public Completable sendHistory(List<Content> history) {
            sentHistory.addAll(history);
            return Completable.complete();
        }

        @Override public Completable sendContent(Content content) {
            sentContents.add(content);
            return Completable.complete();
        }

        @Override public Completable sendRealtime(Blob blob) {
            sentRealtime.add(blob);
            return Completable.complete();
        }

        @Override public Flowable<LlmResponse> receive() {
            return receiveProcessor.hide();
        }

        @Override public void close() {
            receiveProcessor.onComplete();
        }

        @Override public void close(Throwable throwable) {
            receiveProcessor.onError(throwable);
        }

        void pushResponse(LlmResponse response) {
            receiveProcessor.onNext(response);
        }
    }

    private static void injectVoid(PetriRunner runner, Place<Void> place) throws Exception {
        runner.executor().inject(runner.envPlace(place), (Void) null)
                .get(1, java.util.concurrent.TimeUnit.SECONDS);
    }

    /** Await a token on {@code place}, which is a stronger barrier than quiescence. */
    @SuppressWarnings("BusyWait")
    private static void awaitMarked(PetriRunner runner, Place<?> place, long timeoutMillis)
            throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (System.currentTimeMillis() < deadline) {
            if (!runner.executor().marking().peekTokens(place).isEmpty()) {
                return;
            }
            Thread.sleep(10);
        }
        throw new AssertionError(
                place.name() + " was not marked within " + timeoutMillis + "ms");
    }

    @SuppressWarnings("BusyWait")
    private static void awaitQuiescent(PetriRunner runner, long timeoutMillis)
            throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (System.currentTimeMillis() < deadline) {
            if (runner.executor().isQuiescent() && runner.executor().inFlightCount() == 0) {
                return;
            }
            Thread.sleep(10);
        }
        throw new AssertionError("Runner did not reach quiescence within " + timeoutMillis + "ms");
    }
}
