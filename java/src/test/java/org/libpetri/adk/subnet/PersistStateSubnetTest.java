package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.events.Event;
import com.google.adk.sessions.BaseSessionService;
import com.google.adk.sessions.GetSessionConfig;
import com.google.adk.sessions.InMemorySessionService;
import com.google.adk.sessions.ListEventsResponse;
import com.google.adk.sessions.ListSessionsResponse;
import com.google.adk.sessions.Session;
import io.reactivex.rxjava3.core.Completable;
import io.reactivex.rxjava3.core.Maybe;
import io.reactivex.rxjava3.core.Single;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.runtime.BitmapNetExecutor;

class PersistStateSubnetTest {

    // ============================================================
    //  Isolation — single StateDelta lands in the session
    // ============================================================

    @Test
    void single_state_delta_appends_event_with_state_delta_actions() {
        var svc = new InMemorySessionService();
        var session = svc.createSession("app", "user", (Map<String, Object>) null, "sess-1").blockingGet();

        var config = PersistStateSubnet.Config.builder("agent", svc, () -> session)
                .invocationIdSupplier(() -> "inv-fixed")
                .build();

        runWith(config, PersistStateSubnet.legacyWrite(Map.of("foo", "bar")));

        assertThat(session.events()).hasSize(1);
        assertThat(session.events().get(0).author()).isEqualTo("agent");
        assertThat(session.events().get(0).invocationId()).isEqualTo("inv-fixed");
        assertThat(session.state()).containsEntry("foo", "bar");
    }

    @Test
    void two_state_deltas_in_initial_marking_both_persist_in_order() {
        var svc = new InMemorySessionService();
        var session = svc.createSession("app", "user", (Map<String, Object>) null, "sess-2").blockingGet();

        var counter = new AtomicInteger();
        var config = PersistStateSubnet.Config.builder("agent", svc, () -> session)
                .invocationIdSupplier(() -> "inv-" + counter.incrementAndGet())
                .build();

        runWith(config,
                PersistStateSubnet.legacyWrite(Map.of("k1", "v1")),
                PersistStateSubnet.legacyWrite(Map.of("k2", "v2")));

        assertThat(session.events()).hasSize(2);
        // Both keys are merged into state. (Order of merging is the orchestrator's
        // serial fire order — both end up present regardless.)
        assertThat(session.state()).containsAtLeast("k1", "v1", "k2", "v2");
        // Each event got a fresh invocationId from the supplier.
        var ids = session.events().stream().map(Event::invocationId).toList();
        assertThat(ids).containsExactly("inv-1", "inv-2").inOrder();
    }

    @Test
    void overlapping_keys_apply_in_serial_fire_order() {
        // Both deltas write to the same key. The orchestrator fires one,
        // then the other, on the single orchestrator thread — no race.
        // Whichever fires LAST wins (stateDelta is applied at appendEvent).
        var svc = new InMemorySessionService();
        var session = svc.createSession("app", "user", (Map<String, Object>) null, "sess-3").blockingGet();

        runWith(
                PersistStateSubnet.Config.builder("agent", svc, () -> session).build(),
                PersistStateSubnet.legacyWrite(Map.of("k", "first")),
                PersistStateSubnet.legacyWrite(Map.of("k", "second")));

        assertThat(session.events()).hasSize(2);
        // Two events appended; final state has either "first" or "second" —
        // libpetri picks a deterministic fire order from the initial-marking
        // iteration order, so this test asserts on the final state without
        // caring which value won (the point is structural correctness, not
        // which value happened to be picked).
        assertThat(session.state().get("k")).isAnyOf("first", "second");
    }

    // ============================================================
    //  Race-free composition — two parallel producers funnel into
    //  the single Persist transition. libpetri's single orchestrator
    //  thread serializes the writes — no ConcurrentModificationException,
    //  no lost updates.
    // ============================================================

    @Test
    void two_parallel_producer_transitions_funnel_through_single_persist_writer() {
        // Topology:
        //   [start] --T_fork--> Out.and([branchA], [branchB])
        //   [branchA] --T_producerA--> [STATE_DELTA]
        //   [branchB] --T_producerB--> [STATE_DELTA]
        //   [STATE_DELTA] --T_Persist (PersistStateSubnet)--> (consumed)
        //
        // Each producer writes a distinct key. With libpetri's structural
        // serialization at the single Persist transition, both keys
        // land in session.state and both events appear in session.events.
        var svc = new InMemorySessionService();
        var session = svc.createSession("app", "user", (Map<String, Object>) null, "sess-race").blockingGet();

        var start    = Place.of("start", Void.class);
        var branchA  = Place.of("branchA", Void.class);
        var branchB  = Place.of("branchB", Void.class);

        var forkAction = TransitionAction.fork();   // copies single input → all outputs
        var producerAAction = (TransitionAction) (ctx -> {
            ctx.input(branchA);
            ctx.output(AdkColours.LEGACY_SESSION_WRITE,
                    PersistStateSubnet.legacyWrite(Map.of("from", "A")));
            return CompletableFuture.completedFuture(null);
        });
        var producerBAction = (TransitionAction) (ctx -> {
            ctx.input(branchB);
            ctx.output(AdkColours.LEGACY_SESSION_WRITE,
                    PersistStateSubnet.legacyWrite(Map.of("from-b", "B")));
            return CompletableFuture.completedFuture(null);
        });

        var persistBindings = PersistStateSubnet.actionBindings(
                PersistStateSubnet.Config.builder("agent", svc, () -> session).build());

        var net = PetriNet.builder("race")
                .transition(Transition.builder("Fork")
                        .inputs(Arc.In.one(start))
                        .outputs(Arc.Out.and(branchA, branchB))
                        .action(forkAction)
                        .build())
                .transition(Transition.builder("ProducerA")
                        .inputs(Arc.In.one(branchA))
                        .outputs(Arc.Out.place(AdkColours.LEGACY_SESSION_WRITE))
                        .action(producerAAction)
                        .build())
                .transition(Transition.builder("ProducerB")
                        .inputs(Arc.In.one(branchB))
                        .outputs(Arc.Out.place(AdkColours.LEGACY_SESSION_WRITE))
                        .action(producerBAction)
                        .build())
                .compose(PersistStateSubnet.DEF)
                .build()
                // Use function-based bindActions so the inline actions on Fork /
                // ProducerA / ProducerB are preserved (returning null leaves the
                // existing action untouched). Map-based bindActions would default
                // them to passthrough() and the producers would emit nothing.
                .bindActions(name -> persistBindings.get(name));

        var executor = BitmapNetExecutor.builder(net,
                        Map.of(start, List.of(Token.of((Void) null))))
                .eventStore(EventStore.inMemory())
                .build();
        executor.run();

        // Both deltas persisted serially through the single Persist transition.
        assertThat(session.events()).hasSize(2);
        assertThat(session.state()).containsAtLeast("from", "A", "from-b", "B");
    }

    // ============================================================
    //  Action failure surfaces as TransitionFailed (caller decides)
    // ============================================================

    @Test
    void session_service_error_surfaces_as_transition_failure() {
        var failing = new BaseSessionService() {
            @Override public Single<Session> createSession(String a, String u,
                                                            ConcurrentMap<String,Object> s,
                                                            String i) {
                return Single.error(new UnsupportedOperationException());
            }
            @Override public Single<Session> createSession(String a, String u, Map<String,Object> s, String i) {
                return Single.error(new UnsupportedOperationException());
            }
            @Override public Maybe<Session> getSession(
                    String a, String u, String s,
                    Optional<GetSessionConfig> cfg) {
                return Maybe.empty();
            }
            @Override public Single<ListSessionsResponse> listSessions(String a, String u) {
                return Single.error(new UnsupportedOperationException());
            }
            @Override public Completable deleteSession(String a, String u, String s) {
                return Completable.complete();
            }
            @Override public Single<ListEventsResponse> listEvents(String a, String u, String s) {
                return Single.error(new UnsupportedOperationException());
            }
            @Override public Single<Event> appendEvent(Session session, Event event) {
                return Single.error(new RuntimeException("db down"));
            }
        };

        var session = Session.builder("s").appName("app").userId("u").build();
        var config = PersistStateSubnet.Config.builder("agent", failing, () -> session).build();

        var store = EventStore.inMemory();
        var net = PetriNet.builder("test")
                .compose(PersistStateSubnet.DEF)
                .build()
                .bindActions(PersistStateSubnet.actionBindings(config));

        var executor = BitmapNetExecutor.builder(net,
                        Map.of(AdkColours.LEGACY_SESSION_WRITE, List.of(
                                Token.of(PersistStateSubnet.legacyWrite(Map.of("k", "v"))))))
                .eventStore(store)
                .build();
        executor.run();

        var failed = store.events().stream()
                .filter(NetEvent.TransitionFailed.class::isInstance)
                .toList();
        assertThat(failed).hasSize(1);
    }

    /** Drive a net with just PersistStateSubnet + StateDelta tokens in the initial marking. */
    private static void runWith(PersistStateSubnet.Config config, AdkColours.LegacySessionWrite... deltas) {
        var net = PetriNet.builder("test")
                .compose(PersistStateSubnet.DEF)
                .build()
                .bindActions(PersistStateSubnet.actionBindings(config));

        List<Token<?>> tokens = new ArrayList<>();
        for (var d : deltas) tokens.add(Token.of(d));
        Map<Place<?>, List<Token<?>>> initial = Map.of(AdkColours.LEGACY_SESSION_WRITE, tokens);

        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(EventStore.inMemory())
                .build();
        executor.run();
    }

    @Test
    void subnet_def_has_one_transition_and_one_port() {
        assertThat(PersistStateSubnet.DEF.body().transitions().stream()
                .map(t -> t.name()).toList())
                .containsExactly(PersistStateSubnet.Transitions.PERSIST);
        assertThat(PersistStateSubnet.DEF.iface().ports().stream()
                .map(p -> p.name()).toList())
                .containsExactly("legacySessionWrite");
    }

    // ============================================================
    //  Concurrency: append serialization
    //
    //  The single-Persist invariant is structural — only the orchestrator
    //  thread fires Persist transitions, so two StateDelta tokens cannot
    //  be persisted simultaneously. This test confirms appendEvent calls
    //  are serial via a latch that would deadlock under parallel firing.
    // ============================================================

    @Test
    void appendEvent_calls_are_serialized() throws Exception {
        var calls = new AtomicInteger();
        var inFlight = new AtomicInteger();
        var maxInFlight = new AtomicInteger();
        var bothScheduled = new CountDownLatch(2);

        var svc = new BaseSessionService() {
            @Override public Single<Session> createSession(String a, String u,
                                                            ConcurrentMap<String,Object> s, String i) {
                return Single.never();
            }
            @Override public Single<Session> createSession(String a, String u, Map<String,Object> s, String i) {
                return Single.never();
            }
            @Override public Maybe<Session> getSession(
                    String a, String u, String s,
                    Optional<GetSessionConfig> cfg) {
                return Maybe.empty();
            }
            @Override public Single<ListSessionsResponse> listSessions(String a, String u) {
                return Single.never();
            }
            @Override public Completable deleteSession(String a, String u, String s) {
                return Completable.complete();
            }
            @Override public Single<ListEventsResponse> listEvents(String a, String u, String s) {
                return Single.never();
            }
            @Override public Single<Event> appendEvent(Session session, Event event) {
                calls.incrementAndGet();
                int now = inFlight.incrementAndGet();
                maxInFlight.accumulateAndGet(now, Math::max);
                bothScheduled.countDown();
                try {
                    // Brief sleep — if Persist transitions actually fired in parallel,
                    // we'd see maxInFlight=2 here.
                    Thread.sleep(50);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
                inFlight.decrementAndGet();
                return Single.just(event);
            }
        };

        var session = Session.builder("s").appName("app").userId("u").build();
        var config = PersistStateSubnet.Config.builder("agent", svc, () -> session).build();

        var net = PetriNet.builder("test")
                .compose(PersistStateSubnet.DEF)
                .build()
                .bindActions(PersistStateSubnet.actionBindings(config));

        var initial = Map.<Place<?>, List<Token<?>>>of(
                AdkColours.LEGACY_SESSION_WRITE, List.of(
                        Token.of(PersistStateSubnet.legacyWrite(Map.of("k1", "v1"))),
                        Token.of(PersistStateSubnet.legacyWrite(Map.of("k2", "v2")))));

        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(EventStore.inMemory())
                .build();
        executor.run();

        assertThat(calls.get()).isEqualTo(2);
        // Real assertion: the orchestrator schedules the second Persist only
        // after the first fire's action completes. If parallel, maxInFlight
        // would be 2; serialized, it's 1.
        assertThat(maxInFlight.get()).isEqualTo(1);
        // Both fires did happen (latch reached 0) — sanity check.
        assertThat(bothScheduled.await(0, TimeUnit.SECONDS)).isTrue();
    }
}
