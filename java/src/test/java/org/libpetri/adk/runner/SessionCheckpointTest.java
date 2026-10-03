package org.libpetri.adk.runner;

import static com.google.common.truth.Truth.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.subscribers.TestSubscriber;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.Transition;

class SessionCheckpointTest {

    private static final SessionKey KEY = new SessionKey("app", "user", "session");

    private static final Place<Integer> COUNTER = Place.of("counter", Integer.class);
    private static final Place<String> BUMP = Place.of("bump", String.class);
    private static final Place<String> WORK = Place.of("work", String.class);
    private static final Place<String> DONE = Place.of("done", String.class);

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    /**
     * A session closed at rest is saved, and a new runner for the same key
     * resumes from it: the agent's turn permit is back in the marking, as it
     * was, and the resumed session answers its next turn. Nothing else of the
     * finished turn rests to be saved. The delivered event is not saved
     * either: {@code EVENT_OUT} is egress, and never checkpointed.
     */
    @Test
    void a_session_closed_at_rest_resumes_with_its_marking() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var net = agentNet();
        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints)) {
            var runner = registry.getOrCreate(KEY, key -> startResuming(net, checkpoints, key));
            TestSubscriber<Event> egress = runner.adkEvents().test();
            runner.inject(AdkColours.USER_IN, userMessage("remember me")).get(1, TimeUnit.SECONDS);
            egress.awaitCount(1);
            var before = new LinkedHashMap<>(runner.snapshot().marking());
            assertThat(before).containsKey(AdkColours.EVENT_OUT.name());

            assertThat(registry.close(KEY)).isTrue();
            var saved = checkpoints.load(KEY).orElseThrow();
            before.remove(AdkColours.EVENT_OUT.name());
            assertThat(saved).isEqualTo(before);
            assertThat(saved.get(AdkColours.TURN_PERMIT.name())).hasSize(1);
            assertThat(saved).doesNotContainKey(LlmAgentSubnet.CONVERSATION.name());
            assertThat(saved).doesNotContainKey(LlmAgentSubnet.REASK_BUDGET.name());

            var resumed = registry.getOrCreate(KEY, key -> startResuming(net, checkpoints, key));
            // Restored, not seeded again on top: still exactly one permit.
            assertThat(resumed.snapshot().marking().get(AdkColours.TURN_PERMIT.name())).hasSize(1);
            TestSubscriber<Event> resumedEgress = resumed.adkEvents().test();
            resumed.inject(AdkColours.USER_IN, userMessage("still there?")).get(1, TimeUnit.SECONDS);
            resumedEgress.awaitCount(1);
            assertThat(resumedEgress.values().getFirst().content().get().text()).isEqualTo("ok");
        }
    }

    /**
     * Delivered events are never consumed in the net, so a checkpoint that
     * kept them would hand every resume all earlier sessions' events again,
     * one more batch per session. Across three sessions, none is saved.
     */
    @Test
    void delivered_events_do_not_accumulate_across_resumes() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var net = agentNet();
        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints)) {
            for (int i = 0; i < 3; i++) {
                var runner = registry.getOrCreate(KEY, key -> startResuming(net, checkpoints, key));
                TestSubscriber<Event> egress = runner.adkEvents().test();
                runner.inject(AdkColours.USER_IN, userMessage("turn " + i)).get(1, TimeUnit.SECONDS);
                egress.awaitCount(1);
                assertThat(registry.close(KEY)).isTrue();

                var saved = checkpoints.load(KEY).orElseThrow();
                assertThat(saved).doesNotContainKey(AdkColours.EVENT_OUT.name());
            }
        }
    }

    /** Further egress places can be left out alongside {@code EVENT_OUT}. */
    @Test
    void a_runner_names_further_places_to_leave_out() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var net = counterNet(new CountDownLatch(0));
        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints)) {
            var runner = registry.getOrCreate(KEY, key -> counterRunner(net, checkpoints, key)
                    .excludeFromCheckpoint(DONE)
                    .start());
            runner.inject(BUMP, "x").get(1, TimeUnit.SECONDS);
            runner.inject(WORK, "job").get(1, TimeUnit.SECONDS);
            registry.close(KEY);

            var saved = checkpoints.load(KEY).orElseThrow();
            assertThat(saved).doesNotContainKey(DONE.name());
            assertThat(counterOf(saved)).isEqualTo(1);
        }
    }

    /**
     * One factory serves the first start and every resume: the initial
     * marking seeds a session without a checkpoint, and a checkpoint found by
     * {@code resumeFrom} takes its place rather than clash with it.
     */
    @Test
    void a_checkpoint_takes_precedence_over_the_initial_marking() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var net = counterNet(new CountDownLatch(0));
        Function<SessionKey, PetriRunner> factory = key -> counterRunner(net, checkpoints, key).start();
        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints)) {
            var first = registry.getOrCreate(KEY, factory);
            first.inject(BUMP, "x").get(1, TimeUnit.SECONDS);
            registry.close(KEY);
            assertThat(counterOf(checkpoints.load(KEY).orElseThrow())).isEqualTo(1);

            var resumed = registry.getOrCreate(KEY, factory);
            assertThat(counterOf(resumed.snapshot().marking())).isEqualTo(1);
            resumed.inject(BUMP, "y").get(1, TimeUnit.SECONDS);
            registry.close(KEY);
            assertThat(counterOf(checkpoints.load(KEY).orElseThrow())).isEqualTo(2);
        }
    }

    /** An explicit restore next to an explicit seed is still a contradiction. */
    @Test
    void an_explicit_restore_with_an_initial_marking_is_rejected() {
        var builder = PetriRunner.builder(counterNet(new CountDownLatch(0)))
                .initialMarking(Map.of(COUNTER, List.of(Token.of(0))))
                .restore(Map.of(COUNTER.name(), List.of(Token.of(5))))
                .orchestratorExecutor(EXECUTOR);
        assertThrows(IllegalStateException.class, builder::start);
    }

    /**
     * Teardown drains before it saves: the runner refuses injects from the
     * moment close begins, the action in flight finishes, and its output is
     * in the checkpoint.
     */
    @Test
    void an_action_in_flight_completes_and_is_checkpointed() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var release = new CountDownLatch(1);
        var net = counterNet(release);
        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints)) {
            var runner = registry.getOrCreate(KEY, key -> counterRunner(net, checkpoints, key).start());
            runner.inject(BUMP, "x").get(1, TimeUnit.SECONDS);
            runner.inject(WORK, "job").get(1, TimeUnit.SECONDS);

            var closing = CompletableFuture.supplyAsync(() -> registry.close(KEY), EXECUTOR);
            awaitDraining(runner);
            assertThat(registry.get(KEY)).isNull();
            assertThat(runner.inject(BUMP, "late").get(1, TimeUnit.SECONDS)).isFalse();
            assertThat(closing.isDone()).isFalse();

            release.countDown();
            assertThat(closing.get(5, TimeUnit.SECONDS)).isTrue();

            var saved = checkpoints.load(KEY).orElseThrow();
            assertThat(saved.get(DONE.name()).getFirst().value()).isEqualTo("job");
            assertThat(counterOf(saved)).isEqualTo(1);
        }
    }

    /**
     * A runner that does not drain in time leaves no checkpoint behind: an
     * older one would be restored as if it were this session's last word.
     */
    @Test
    void a_session_that_does_not_drain_in_time_loses_its_stale_checkpoint() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var release = new CountDownLatch(1);
        var net = counterNet(release);
        checkpoints.save(KEY, Map.of(COUNTER.name(), List.of(Token.of(41))));
        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints, Duration.ofMillis(100))) {
            var runner = registry.getOrCreate(KEY, key -> counterRunner(net, checkpoints, key).start());
            runner.inject(WORK, "job").get(1, TimeUnit.SECONDS);

            var closing = CompletableFuture.supplyAsync(() -> registry.close(KEY), EXECUTOR);
            // The checkpoint goes once the timeout passes, while the action still runs.
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
            while ((checkpoints.load(KEY).isPresent() || registry.size() > 0)
                    && System.nanoTime() < deadline) {
                Thread.onSpinWait();
            }
            assertThat(checkpoints.load(KEY)).isEmpty();
            assertThat(registry.size()).isEqualTo(0);

            release.countDown();
            assertThat(closing.get(5, TimeUnit.SECONDS)).isTrue();
            assertThat(checkpoints.load(KEY)).isEmpty();
        }
    }

    /**
     * The race the closing slot exists for. While the old runner drains, a
     * {@code getOrCreate} for its key waits instead of resuming from the
     * checkpoint the old runner has yet to write; once the save lands, the
     * new runner resumes from exactly that marking. There is never a moment
     * with two serving runners for the key.
     */
    @Test
    void get_or_create_during_close_waits_and_resumes_from_the_final_marking() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var release = new CountDownLatch(1);
        var net = counterNet(release);
        // An older checkpoint, so resuming early would be visibly wrong.
        checkpoints.save(KEY, Map.of(COUNTER.name(), List.of(Token.of(7))));
        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints)) {
            var old = registry.getOrCreate(KEY, key -> counterRunner(net, checkpoints, key).start());
            assertThat(counterOf(old.snapshot().marking())).isEqualTo(7);
            old.inject(BUMP, "x").get(1, TimeUnit.SECONDS);
            old.inject(WORK, "job").get(1, TimeUnit.SECONDS);

            var closing = CompletableFuture.supplyAsync(() -> registry.close(KEY), EXECUTOR);
            awaitDraining(old);
            assertThat(registry.get(KEY)).isNull();

            var factoryCalls = new AtomicInteger();
            var oldTerminatedAtCreate = new AtomicReference<Boolean>();
            var replacing = CompletableFuture.supplyAsync(() -> registry.getOrCreate(KEY, key -> {
                factoryCalls.incrementAndGet();
                oldTerminatedAtCreate.set(old.awaitTermination(Duration.ZERO));
                return counterRunner(net, checkpoints, key).start();
            }), EXECUTOR);
            assertThrows(TimeoutException.class, () -> replacing.get(200, TimeUnit.MILLISECONDS));
            assertThat(factoryCalls.get()).isEqualTo(0);

            release.countDown();
            assertThat(closing.get(5, TimeUnit.SECONDS)).isTrue();
            var fresh = replacing.get(5, TimeUnit.SECONDS);

            assertThat(fresh).isNotSameInstanceAs(old);
            assertThat(factoryCalls.get()).isEqualTo(1);
            assertThat(oldTerminatedAtCreate.get()).isTrue();
            var marking = fresh.snapshot().marking();
            assertThat(counterOf(marking)).isEqualTo(8);
            assertThat(marking.get(DONE.name()).getFirst().value()).isEqualTo("job");
            assertThat(registry.get(KEY)).isSameInstanceAs(fresh);
        }
    }

    /**
     * An {@link Error} from the store reaches the caller, but only after the
     * runner is torn down and the key freed: a broken store never orphans a
     * session's runner. The checkpoint it could not replace is gone too.
     */
    @Test
    void an_error_from_the_store_still_tears_the_runner_down() throws Exception {
        var inner = SessionCheckpointStore.inMemory();
        inner.save(KEY, Map.of(COUNTER.name(), List.of(Token.of(41))));
        var broken = new SessionCheckpointStore() {
            @Override public void save(SessionKey key, Map<String, List<Token<?>>> marking) {
                throw new StoreBroke();
            }
            @Override public Optional<Map<String, List<Token<?>>>> load(SessionKey key) {
                return inner.load(key);
            }
            @Override public void remove(SessionKey key) {
                inner.remove(key);
            }
        };
        var net = counterNet(new CountDownLatch(0));
        try (var registry = SessionExecutorRegistry.strongOwned(broken)) {
            var runner = registry.getOrCreate(KEY, key -> counterRunner(net, broken, key).start());
            runner.inject(BUMP, "x").get(1, TimeUnit.SECONDS);

            assertThrows(StoreBroke.class, () -> registry.close(KEY));

            assertThat(runner.awaitTermination(Duration.ZERO)).isTrue();
            assertThat(registry.size()).isEqualTo(0);
            assertThat(inner.load(KEY)).isEmpty();
            var next = registry.getOrCreate(KEY, key -> counterRunner(net, broken, key).start());
            assertThat(counterOf(next.snapshot().marking())).isEqualTo(0);
            registry.discard(KEY);   // the registry's own close would hit the broken save again
        }
    }

    /** A store forgets a checkpoint on request. */
    @Test
    void remove_forgets_a_checkpoint() {
        var checkpoints = SessionCheckpointStore.inMemory();
        checkpoints.save(KEY, Map.of(COUNTER.name(), List.of(Token.of(1))));
        checkpoints.remove(KEY);
        assertThat(checkpoints.load(KEY)).isEmpty();
        checkpoints.remove(KEY);   // no-op
        assertThat(checkpoints.load(KEY)).isEmpty();
    }

    /**
     * {@code discard} ends a session without saving it and drops what was
     * saved before, so the next runner for the key starts from its seed.
     */
    @Test
    void discard_ends_a_session_without_a_checkpoint() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var net = counterNet(new CountDownLatch(0));
        Function<SessionKey, PetriRunner> factory = key -> counterRunner(net, checkpoints, key).start();
        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints)) {
            var runner = registry.getOrCreate(KEY, factory);
            runner.inject(BUMP, "x").get(1, TimeUnit.SECONDS);
            registry.close(KEY);
            assertThat(checkpoints.load(KEY)).isPresent();

            var resumed = registry.getOrCreate(KEY, factory);
            resumed.inject(BUMP, "y").get(1, TimeUnit.SECONDS);
            assertThat(registry.discard(KEY)).isTrue();
            assertThat(resumed.awaitTermination(Duration.ZERO)).isTrue();
            assertThat(checkpoints.load(KEY)).isEmpty();
            assertThat(counterOf(registry.getOrCreate(KEY, factory).snapshot().marking())).isEqualTo(0);

            // With no runner registered, discard still forgets the checkpoint.
            registry.close(KEY);
            assertThat(checkpoints.load(KEY)).isPresent();
            assertThat(registry.discard(KEY)).isFalse();
            assertThat(checkpoints.load(KEY)).isEmpty();
        }
    }

    private static final class StoreBroke extends Error {}

    /**
     * Waits until teardown has drained {@code runner}, which it does right
     * after taking the key's slot. A drained executor refuses snapshots.
     */
    private static void awaitDraining(PetriRunner runner) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (true) {
            try {
                runner.snapshot();
            } catch (IllegalStateException drained) {
                return;
            }
            if (System.nanoTime() > deadline) throw new AssertionError("close never began");
            Thread.onSpinWait();
        }
    }

    /**
     * {@code BUMP} increments {@code COUNTER}; {@code WORK} runs an action
     * that waits for {@code release} before depositing into {@code DONE}.
     */
    private static PetriNet counterNet(CountDownLatch release) {
        return PetriNet.builder("counter")
                .transition(Transition.builder("Counter_Bump")
                        .inputs(Arc.In.one(BUMP), Arc.In.one(COUNTER))
                        .outputs(Arc.Out.place(COUNTER))
                        .action(ctx -> {
                            ctx.input(BUMP);
                            ctx.output(COUNTER, ctx.input(COUNTER) + 1);
                            return CompletableFuture.completedFuture(null);
                        })
                        .build())
                .transition(Transition.builder("Counter_Work")
                        .inputs(Arc.In.one(WORK))
                        .outputs(Arc.Out.place(DONE))
                        .action(ctx -> {
                            String in = ctx.input(WORK);
                            return CompletableFuture.runAsync(() -> {
                                try {
                                    release.await();
                                } catch (InterruptedException e) {
                                    Thread.currentThread().interrupt();
                                }
                                ctx.output(DONE, in);
                            }, EXECUTOR);
                        })
                        .build())
                .build();
    }

    /** Seeded with a zero counter, resumed from {@code store} when it has a checkpoint. */
    private static PetriRunner.Builder counterRunner(PetriNet net, SessionCheckpointStore store,
                                                     SessionKey key) {
        return PetriRunner.builder(net)
                .environmentPlaces(BUMP, WORK)
                .initialMarking(Map.of(COUNTER, List.of(Token.of(0))))
                .resumeFrom(store, key)
                .orchestratorExecutor(EXECUTOR);
    }

    private static int counterOf(Map<String, List<Token<?>>> marking) {
        var tokens = new ArrayList<>(marking.get(COUNTER.name()));
        assertThat(tokens).hasSize(1);
        return (Integer) tokens.getFirst().value();
    }

    private static PetriRunner startResuming(PetriNet net, SessionCheckpointStore store, SessionKey key) {
        return PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .resumeFrom(store, key)
                .orchestratorExecutor(EXECUTOR)
                .start();
    }

    private static PetriNet agentNet() {
        var llm = new BaseLlm("echo") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.just(LlmResponse.builder()
                        .content(Content.builder().role("model")
                                .parts(List.of(Part.fromText("ok"))).build())
                        .build());
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .reaskBudget(3)
                .dispatchExecutor(EXECUTOR)
                .build();
        return PetriNet.builder("checkpointed")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));
    }

    private static Content userMessage(String text) {
        return Content.builder().role("user").parts(List.of(Part.fromText(text))).build();
    }
}
