package org.libpetri.adk.subnet;

import com.google.adk.events.Event;
import com.google.adk.events.EventActions;
import com.google.adk.sessions.BaseSessionService;
import com.google.adk.sessions.Session;
import java.time.Duration;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.function.Supplier;
import org.libpetri.core.Arc;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Timing;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;

/**
 * Stock subnet that persists {@link AdkColours.LegacySessionWrite} tokens via
 * {@link BaseSessionService#appendEvent(Session, Event)}.
 *
 * <p>This is the <b>only</b> place state writes happen in an adk-libpetri
 * net: every {@link AdkColours#LEGACY_SESSION_WRITE} token funnels
 * through the single {@link Transitions#PERSIST} transition, so
 * concurrent envelope-producing transitions in the surrounding net
 * cannot race on {@link Session#state()}. libpetri's single
 * orchestrator thread fires the Persist transition serially per
 * arriving token; the AND-join happens at the marking level, not in
 * user code.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [LEGACY_SESSION_WRITE] --T_Persist (deadline 5s)--> (no output — terminal sink)
 * </pre>
 *
 * <p>The transition has no output spec — it consumes the envelope
 * token and the action's side effect is the {@code appendEvent} call.
 * Per-fire timing is bounded by {@link Timing#deadline} so a hung
 * {@link BaseSessionService} can't deadlock the net.
 *
 * <h2>Configuration</h2>
 * <p>Bind via {@link #actionBindings(Config)}. {@link Config} carries:
 * <ul>
 *   <li>A {@link Supplier} for the session — captures the per-net
 *       session reference, evaluated per fire.</li>
 *   <li>The {@link BaseSessionService} that does the persistence.</li>
 *   <li>An {@code author} string (becomes {@link Event#author()}) and an
 *       invocation-id supplier (becomes {@link Event#invocationId()}).</li>
 * </ul>
 *
 * <p>The 5-second persistence deadline is structural — bound on the
 * Persist transition in {@link #DEF} so it survives any
 * {@code bindActions} pass. Users who need a different deadline build
 * their own single-transition subnet with the same shape rather than
 * trying to override it from a per-instance config.
 *
 * <p>This is a convenience template — users who need different
 * persistence semantics (custom event shape, transactional batching,
 * alternative session storage) compose their own subnet with the same
 * shape (single transition with {@code Arc.In.one(LEGACY_SESSION_WRITE)})
 * and keep the structural race-freedom guarantee.
 */
public final class PersistStateSubnet {

    public static final String NAME = "PersistState";

    public static final class Transitions {
        public static final String PERSIST = NAME + "_Persist";
        private Transitions() {}
    }

    public record Config(
            String author,
            Supplier<String> invocationIdSupplier,
            Supplier<Session> sessionSupplier,
            BaseSessionService sessionService) {

        public Config {
            Objects.requireNonNull(author, "author");
            Objects.requireNonNull(invocationIdSupplier, "invocationIdSupplier");
            Objects.requireNonNull(sessionSupplier, "sessionSupplier");
            Objects.requireNonNull(sessionService, "sessionService");
        }

        public static Builder builder(String author,
                                      BaseSessionService sessionService,
                                      Supplier<Session> sessionSupplier) {
            return new Builder(author, sessionService, sessionSupplier);
        }

        public static final class Builder {
            private final String author;
            private final BaseSessionService sessionService;
            private final Supplier<Session> sessionSupplier;
            private Supplier<String> invocationIdSupplier = () -> UUID.randomUUID().toString();

            private Builder(String author, BaseSessionService sessionService,
                            Supplier<Session> sessionSupplier) {
                this.author = author;
                this.sessionService = sessionService;
                this.sessionSupplier = sessionSupplier;
            }

            public Builder invocationIdSupplier(Supplier<String> s) { this.invocationIdSupplier = s; return this; }

            public Config build() {
                return new Config(author, invocationIdSupplier, sessionSupplier, sessionService);
            }
        }
    }

    /**
     * Stateless subnet definition. Note the {@link Timing#deadline}
     * bound on the Persist transition — applied at definition time so it
     * survives unchanged through any {@link org.libpetri.core.PetriNet#bindActions}
     * pass.
     */
    public static final SubnetDef<Void> DEF = SubnetDef.builder(NAME)
            .place(AdkColours.LEGACY_SESSION_WRITE)
            .transition(Transition.builder(Transitions.PERSIST)
                    .inputs(Arc.In.one(AdkColours.LEGACY_SESSION_WRITE))
                    // No output spec — pure consumer; appendEvent is a side effect.
                    .timing(Timing.deadline(Duration.ofSeconds(5)))
                    .build())
            .inputPort("legacySessionWrite", AdkColours.LEGACY_SESSION_WRITE)
            .build();

    public static Map<String, TransitionAction> actionBindings(Config config) {
        Objects.requireNonNull(config, "config");
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.PERSIST, persistAction(config));
        return SubnetActions.bind(DEF, session);
    }

    private static TransitionAction persistAction(Config config) {
        return ctx -> {
            AdkColours.LegacySessionWrite delta = ctx.input(AdkColours.LEGACY_SESSION_WRITE);

            EventActions actions = EventActions.builder()
                    .stateDelta(delta.delta())
                    .build();

            Event event = Event.builder()
                    .invocationId(config.invocationIdSupplier().get())
                    .author(config.author())
                    .actions(actions)
                    .build();

            CompletableFuture<Void> done = new CompletableFuture<>();
            config.sessionService()
                    .appendEvent(config.sessionSupplier().get(), event)
                    .subscribe(
                            persisted -> done.complete(null),
                            err -> done.completeExceptionally(err));
            return done;
        };
    }

    private PersistStateSubnet() {}

    // ============================================================
    //  Envelope construction — narrow factories that make the legacy
    //  bridge intent obvious at every call site.
    // ============================================================

    /**
     * Build a {@link AdkColours.LegacySessionWrite} envelope from a raw
     * {@code Map<String, Object>}.
     *
     * <p>This is the <b>only</b> sanctioned way to construct an
     * envelope. Named loudly because the {@code Map<String, Object>}
     * shape is the legacy-ADK-Session boundary, not an endorsement of
     * a kitchen-sink in-net state representation. For in-net state,
     * declare typed {@link org.libpetri.core.Place}s per domain
     * concept and Read them — the in-net conversation-place pattern.
     */
    public static AdkColours.LegacySessionWrite legacyWrite(Map<String, Object> delta) {
        Objects.requireNonNull(delta, "delta");
        return new AdkColours.LegacySessionWrite(delta);
    }

    /**
     * Merge several envelopes in insertion order. Use when batching
     * multiple delta tokens before the single Persist transition fires
     * — keeps the call-site explicit about the legacy bridge.
     */
    public static AdkColours.LegacySessionWrite merge(Collection<AdkColours.LegacySessionWrite> envelopes) {
        var merged = new LinkedHashMap<String, Object>();
        for (var e : envelopes) merged.putAll(e.delta());
        return new AdkColours.LegacySessionWrite(merged);
    }
}
