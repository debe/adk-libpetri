package org.libpetri.adk.runner;

import com.google.adk.agents.BaseAgent;
import com.google.adk.agents.InvocationContext;
import com.google.adk.agents.LiveRequestQueue;
import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.common.collect.ImmutableList;
import com.google.genai.types.Content;
import com.google.genai.types.LiveServerMessage;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.core.Single;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.CompletableFuture;
import java.util.function.BiConsumer;
import java.util.function.Function;
import org.libpetri.adk.bridge.OtelEventStore;
import org.libpetri.adk.Experimental;
import org.libpetri.adk.colours.AdkColours;

/**
 * Thin {@link BaseAgent} adapter that lets a libpetri net work as a
 * drop-in inside ADK's stock {@code com.google.adk.runner.Runner} —
 * <b>no source changes to ADK required</b>.
 *
 * <p>On each {@code runAsyncImpl(ctx)} call:
 * <ol>
 *   <li>Derives a {@link SessionKey} from {@code ctx.session()}.</li>
 *   <li>Derives a <b>lifetime owner</b> via the configured
 *       {@code ownerExtractor} — see the lifetime contract below.</li>
 *   <li>Asks the {@link SessionExecutorRegistry} for the per-session
 *       {@link PetriRunner}, creating it on first call (one net per
 *       user, kept alive across invocations). The runner's lifetime is
 *       now bound to the lifetime-owner object — when that object is
 *       GC'd, the runner is automatically shut down.</li>
 *   <li>{@code runner.inject(USER_IN, userContent)} injects the user's
 *       message onto the net's {@code USER_IN} env place.</li>
 *   <li>Returns {@code runner.adkEvents().take(1)} — emits the next
 *       {@link Event} produced into the net's {@code EVENT_OUT} place
 *       and then {@code onComplete}s, satisfying the per-invocation
 *       termination contract that ADK {@code Runner} expects.</li>
 * </ol>
 *
 * <h2>Lifetime owner — load-bearing</h2>
 * <p>The {@code ownerExtractor} must return an object whose <b>reference
 * identity</b> is <i>stable across every invocation for the same
 * session</i>, and whose lifetime corresponds to "this session is
 * over." When the owner is collected, the {@link java.lang.ref.Cleaner Cleaner}
 * attached by {@link SessionExecutorRegistry} tears down the runner
 * and removes it from the registry — no possibility of leaking
 * orchestrator threads, hot processors, or executor state. Typical
 * choices:
 * <ul>
 *   <li>The application's websocket-session / connection object
 *       (released by the server on disconnect).</li>
 *   <li>A {@code Map<SessionKey, Object>} the application maintains
 *       and clears on session end.</li>
 * </ul>
 * <p><b>Do not</b> return {@code ctx.session()} directly when backed by
 * {@code InMemorySessionService} — it returns defensive copies, so each
 * call sees a fresh instance that becomes GC-eligible immediately,
 * triggering premature shutdown.
 *
 * <h2>Why {@code take(1)} (and what that misses)</h2>
 * <p>The stock {@link org.libpetri.adk.subnet.LlmAgentSubnet} emits
 * exactly one final {@link Event} per user message (either via the
 * Router's text-only branch or via the reask-budget fallback) — so
 * {@code take(1)} is a perfect match for one-invocation = one-event.
 * Streaming partials ({@code RunConfig.StreamingMode.SSE}) and
 * bidi audio require multi-event semantics with a proper
 * end-of-turn signal — a future variant of this adapter would use
 * {@code takeUntil(e -> e.turnComplete().orElse(false))} or filter by
 * invocation id once the subnet propagates one through the net.
 *
 * <h2>{@code subAgents()} is empty by design</h2>
 * <p>The Petri net <i>is</i> the topology — there are no
 * {@code BaseAgent} sub-instances to expose. Plugins / tooling that
 * walks {@code agent.subAgents()} recursively sees nothing here; the
 * net itself is fully introspectable via {@code PetriNet.places()} /
 * {@code .transitions()} and is strictly more expressive than the agent
 * tree.
 *
 * <h2>{@code runLiveImpl} (BIDI / Live-API)</h2>
 * <p>This adapter is the <b>egress half</b> of the bridge between ADK's
 * BIDI runtime and the per-session libpetri net. {@code runLiveImpl}
 * resolves (or creates) the runner exactly like {@code runAsyncImpl}
 * and returns {@link PetriRunner#adkEvents()} directly — the hot
 * {@code Flowable} of {@link Event} tokens that the net produces into
 * {@link AdkColours#EVENT_OUT}. The ADK {@code Runner.runLive} pipeline
 * forwards those events to the BIDI client.
 *
 * <p>Full BIDI input wiring is handled by {@link BidiPetriAgent#bridge}
 * when a consumer supplies a provider-specific {@link LiveConnection}.
 * ADK delivers BIDI input as {@code com.google.adk.agents.LiveRequestQueue}
 * frames — audio chunks, text fragments, close signals — and the bridge
 * forwards those frames to the connection while the consumer callback decodes
 * raw provider server messages into typed env-place injections: model content
 * via {@link PetriRunner#inject(org.libpetri.core.Place, Object)} and turn/signal
 * edges via {@link PetriRunner#signal(org.libpetri.core.Place)}. The net (not the
 * bridge) authors the outbound {@link Event} and sets {@code partial}/{@code
 * turnComplete}. Provider-specific frame transformation remains caller-side because
 * raw PCM vs. Opus, voice-activity edges, tool routing, and reconnect policy vary
 * per transport. Sketch:
 *
 * <pre>{@code
 * return BidiPetriAgent.bridge(ctx.liveRequestQueue(), connection, runner,
 *     (serverMessage, r) -> {
 *         modelContentOf(serverMessage).ifPresent(c -> r.inject(MODEL_CHUNK, c));
 *         decodeSignals(serverMessage).forEach(s -> r.signal(placeFor(s)));
 *     });
 * }</pre>
 *
 * <p>The non-BIDI path ({@code runAsyncImpl}) <i>replaces</i> ADK
 * orchestration — the Petri net is the brain. The BIDI path is a bridge,
 * not a replacement: ADK owns the live runtime (bidi WebSocket, audio
 * framing, turn detection); the net provides the orchestration brain
 * underneath (barge-in policy, silence recovery, multi-agent transfer,
 * tool dispatch).
 */
public final class PetriAgent extends BaseAgent {

    private final SessionExecutorRegistry registry;
    private final Function<SessionKey, PetriRunner> runnerFactory;
    private final Function<InvocationContext, Object> ownerExtractor;
    private final Tracer tracer;                       // nullable
    private final OtelEventStore otelEventStore;       // nullable
    private final LiveConfig liveConfig;               // nullable
    // Per-session: the open invocation span from the most recent runAsyncImpl
    // call. End it when superseded by the next invocation on the same session,
    // so it lives long enough to cover any late TransitionCompleted emits that
    // the orchestrator produces after take(1) completed.
    private final ConcurrentMap<SessionKey, Span> openInvocationSpans = new ConcurrentHashMap<>();

    /**
     * Configuration for the shipped BIDI/live bridge path.
     *
     * @param connectionFactory creates the provider-specific live connection for this invocation
     * @param onServerMessage observes raw provider messages and may inject signals into the runner
     */
    @Experimental
    public record LiveConfig(Function<InvocationContext, LiveConnection> connectionFactory,
                             BiConsumer<LiveServerMessage, PetriRunner> onServerMessage) {
        public LiveConfig {
            Objects.requireNonNull(connectionFactory, "connectionFactory");
            Objects.requireNonNull(onServerMessage, "onServerMessage");
        }
    }

    private PetriAgent(String name,
                       String description,
                       SessionExecutorRegistry registry,
                       Function<SessionKey, PetriRunner> runnerFactory,
                       Function<InvocationContext, Object> ownerExtractor,
                       Tracer tracer,
                       OtelEventStore otelEventStore,
                       LiveConfig liveConfig) {
        super(name, description, ImmutableList.of(), /*beforeAgentCallback*/ null, /*afterAgentCallback*/ null);
        this.registry = Objects.requireNonNull(registry, "registry");
        this.runnerFactory = Objects.requireNonNull(runnerFactory, "runnerFactory");
        this.ownerExtractor = Objects.requireNonNull(ownerExtractor, "ownerExtractor");
        // Either both tracer + otelEventStore are provided (full OT root-span wiring),
        // or both are null (no-op observability). Mixing them would leak orphan spans
        // (tracer without store) or orphan child spans (store without parent set), so
        // disallow at construction.
        if ((tracer == null) != (otelEventStore == null)) {
            throw new IllegalArgumentException(
                    "tracer and otelEventStore must be either both provided or both null");
        }
        this.tracer = tracer;
        this.otelEventStore = otelEventStore;
        this.liveConfig = liveConfig;
    }

    /**
     * @param name           agent name (per ADK rules: identifier-shaped, not "user")
     * @param description    human-readable description
     * @param registry       shared {@link SessionExecutorRegistry}; one
     *                       {@code PetriAgent} typically owns its own,
     *                       but a registry can be shared across multiple
     *                       agents that want to coordinate session
     *                       runners
     * @param runnerFactory  lazy factory invoked on first call per
     *                       session — gets the {@link SessionKey} so it
     *                       can build a per-session-customised
     *                       {@link PetriRunner} (e.g., per-user OT
     *                       baggage, per-session initial marking)
     * @param ownerExtractor returns the <b>lifetime owner</b> object for
     *                       an invocation. Must return the same object
     *                       identity for every invocation in the same
     *                       session; the runner is torn down when this
     *                       object becomes unreachable. See the class
     *                       javadoc for the lifetime contract.
     */
    public static PetriAgent of(String name,
                                String description,
                                SessionExecutorRegistry registry,
                                Function<SessionKey, PetriRunner> runnerFactory,
                                Function<InvocationContext, Object> ownerExtractor) {
        return new PetriAgent(name, description, registry, runnerFactory, ownerExtractor, null, null, null);
    }

    /**
     * Overload that wires OpenTelemetry root-span observability.
     * Opens a {@code petri.invocation.<agent>} span around each
     * {@link #runAsyncImpl} call and binds its {@link Context} onto the
     * supplied {@link OtelEventStore} for the duration of the invocation,
     * so every transition span emitted by that store attaches as a child.
     * Pass the SAME {@code OtelEventStore} instance that's chained into
     * the runner's {@code eventStore(...)} — otherwise binding has no
     * effect.
     */
    public static PetriAgent of(String name,
                                String description,
                                SessionExecutorRegistry registry,
                                Function<SessionKey, PetriRunner> runnerFactory,
                                Function<InvocationContext, Object> ownerExtractor,
                                Tracer tracer,
                                OtelEventStore otelEventStore) {
        return new PetriAgent(name, description, registry, runnerFactory, ownerExtractor,
                tracer, otelEventStore, null);
    }


    /**
     * Factory for BIDI/live agents that should use the shipped bridge path.
     */
    @Experimental
    public static PetriAgent ofLive(String name,
                                    String description,
                                    SessionExecutorRegistry registry,
                                    Function<SessionKey, PetriRunner> runnerFactory,
                                    Function<InvocationContext, Object> ownerExtractor,
                                    LiveConfig liveConfig) {
        return new PetriAgent(name, description, registry, runnerFactory, ownerExtractor,
                null, null, Objects.requireNonNull(liveConfig, "liveConfig"));
    }

    /**
     * Factory for BIDI/live agents that also wire OpenTelemetry root-span observability.
     */
    @Experimental
    public static PetriAgent ofLive(String name,
                                    String description,
                                    SessionExecutorRegistry registry,
                                    Function<SessionKey, PetriRunner> runnerFactory,
                                    Function<InvocationContext, Object> ownerExtractor,
                                    LiveConfig liveConfig,
                                    Tracer tracer,
                                    OtelEventStore otelEventStore) {
        return new PetriAgent(name, description, registry, runnerFactory, ownerExtractor,
                tracer, otelEventStore, Objects.requireNonNull(liveConfig, "liveConfig"));
    }

    @Override
    protected Flowable<Event> runAsyncImpl(InvocationContext ctx) {
        SessionKey key = SessionKey.from(ctx.session());
        Object owner = Objects.requireNonNull(ownerExtractor.apply(ctx),
                "ownerExtractor returned null — every invocation must yield a lifetime owner");
        PetriRunner runner = registry.getOrCreate(key, owner, runnerFactory);

        Content userContent = ctx.userContent().orElse(null);
        if (userContent == null) {
            return Flowable.empty();
        }

        // Open the per-invocation root span (if OTel is wired) BEFORE attaching
        // the egress subscriber and injecting USER_IN — so the binding is live
        // by the time any transition fires. We deliberately do NOT end the
        // span (or unbind the context) when the returned Flowable terminates:
        // the orchestrator emits TokenAdded(EVENT_OUT, ...) and the subsequent
        // TransitionCompleted on the same thread, and the take(1)→onComplete
        // chain runs synchronously inside that critical section — so a doFinally
        // would clear the binding BEFORE the last transition's span is emitted.
        // Instead, we hold the span open and bound until the NEXT invocation on
        // this session supersedes it (closeAndReplacePreviousSpan), which is
        // safe under ADK's turn-based per-session semantics.
        openInvocationSpan(ctx, key);

        if (ctx.runConfig().streamingMode() == RunConfig.StreamingMode.SSE) {
            var egress = runner.adkEvents()
                    .map(e -> e.toBuilder().invocationId(ctx.invocationId()).build())
                    .takeUntil((Event e) -> !e.partial().orElse(false));
            var replayed = egress.replay();
            replayed.connect();
            runner.inject(AdkColours.USER_IN, userContent);
            return replayed;
        }

        // The runner.events() Flowable is hot (PublishProcessor) — a subscriber
        // attaching after send() could miss an early emission. To eliminate
        // the race, attach a one-shot CompletableFuture sink BEFORE sending,
        // then return a Flowable that bridges off the future. The
        // .subscribe() call below materialises the upstream subscription
        // synchronously, guaranteeing the bridge is hot before the inject.
        // Non-SSE turns complete on the next terminal event; partials are
        // ignored so a streaming-capable net run under NONE still preserves
        // the legacy one-final-event contract.
        CompletableFuture<Event> nextEvent = new CompletableFuture<>();
        runner.adkEvents()
                .filter(e -> !e.partial().orElse(false))
                .take(1)
                .subscribe(
                        nextEvent::complete,
                        nextEvent::completeExceptionally,
                        () -> nextEvent.completeExceptionally(new IllegalStateException(
                                "net event stream completed without emitting a terminal Event for this invocation")));
        runner.inject(AdkColours.USER_IN, userContent);
        return Single.fromCompletionStage(nextEvent).toFlowable();
    }

    /**
     * Ends every still-open per-session invocation span — call before
     * agent shutdown or before reading exported spans in tests. Production
     * use: invoke when the agent is no longer in use (e.g. on application
     * shutdown alongside {@link SessionExecutorRegistry#closeAll()}). No-op
     * when OTel isn't wired or when no span is currently open.
     */
    public void endAllOpenInvocationSpans() {
        openInvocationSpans.values().forEach(Span::end);
        openInvocationSpans.clear();
    }

    /**
     * Opens the per-invocation root span and binds its {@link Context} to
     * the configured {@link OtelEventStore} so transition spans attach as
     * children. Closes the previously-open span for the same session (if
     * any) — only one invocation per session is live at a time under ADK's
     * turn-based model, and supersession is the trigger for the previous
     * span's end. No-op when OTel isn't wired.
     */
    private void openInvocationSpan(InvocationContext ctx, SessionKey key) {
        if (tracer == null) {
            return;
        }
        Span span = tracer.spanBuilder("petri.invocation." + name())
                .setAttribute("petri.agent.name", name())
                .setAttribute("adk.invocation.id", ctx.invocationId())
                .setAttribute("adk.session.id", ctx.session().id())
                .startSpan();
        otelEventStore.bindInvocationContext(Context.current().with(span));
        Span previous = openInvocationSpans.put(key, span);
        if (previous != null) {
            previous.end();
        }
    }

    @Override
    protected Flowable<Event> runLiveImpl(InvocationContext ctx) {
        SessionKey key = SessionKey.from(ctx.session());
        Object owner = Objects.requireNonNull(ownerExtractor.apply(ctx),
                "ownerExtractor returned null — every invocation must yield a lifetime owner");
        PetriRunner runner = registry.getOrCreate(key, owner, runnerFactory);
        if (liveConfig == null) {
            return runner.adkEvents();
        }
        LiveRequestQueue inbound = ctx.liveRequestQueue().orElseThrow(
                () -> new IllegalStateException("runLive requires a LiveRequestQueue"));
        LiveConnection conn = liveConfig.connectionFactory().apply(ctx);
        return BidiPetriAgent.bridge(inbound, conn, runner, liveConfig.onServerMessage());
    }
}
