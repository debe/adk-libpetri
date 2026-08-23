package org.libpetri.adk.runner;

import static com.google.common.truth.Truth.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import io.reactivex.rxjava3.core.Flowable;
import java.lang.ref.WeakReference;
import java.time.Duration;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;

class SessionExecutorRegistryTest {

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    private static final SessionKey K1 = new SessionKey("app", "u1", "s1");
    private static final SessionKey K2 = new SessionKey("app", "u1", "s2");
    private static final SessionKey K3 = new SessionKey("app", "u2", "s1");

    @Test
    void get_or_create_lazily_builds_runner_on_first_call_only() {
        var calls = new AtomicInteger();
        Object owner = new Object();
        try (var registry = SessionExecutorRegistry.cleanerOwned()) {
            var r1 = registry.getOrCreate(K1, owner, k -> { calls.incrementAndGet(); return testRunner(); });
            var r2 = registry.getOrCreate(K1, owner, k -> { calls.incrementAndGet(); return testRunner(); });
            var r3 = registry.getOrCreate(K1, owner, k -> { calls.incrementAndGet(); return testRunner(); });

            assertThat(calls.get()).isEqualTo(1);
            assertThat(r1).isSameInstanceAs(r2);
            assertThat(r2).isSameInstanceAs(r3);
        }
    }

    @Test
    void different_session_keys_get_different_runners() {
        Object owner = new Object();
        try (var registry = SessionExecutorRegistry.cleanerOwned()) {
            var r1 = registry.getOrCreate(K1, owner, k -> testRunner());
            var r2 = registry.getOrCreate(K2, owner, k -> testRunner());
            var r3 = registry.getOrCreate(K3, owner, k -> testRunner());

            assertThat(r1).isNotSameInstanceAs(r2);
            assertThat(r2).isNotSameInstanceAs(r3);
            assertThat(registry.size()).isEqualTo(3);
        }
    }

    @Test
    void same_key_with_different_owner_throws() {
        Object owner1 = new Object();
        Object owner2 = new Object();
        try (var registry = SessionExecutorRegistry.cleanerOwned()) {
            registry.getOrCreate(K1, owner1, k -> testRunner());
            assertThrows(IllegalStateException.class,
                    () -> registry.getOrCreate(K1, owner2, k -> testRunner()));
            // Keep refs alive past the assertion so neither owner is GC'd mid-test.
            assertThat(owner1).isNotSameInstanceAs(owner2);
        }
    }

    @Test
    void close_removes_one_runner_and_returns_true() {
        Object owner = new Object();
        try (var registry = SessionExecutorRegistry.cleanerOwned()) {
            registry.getOrCreate(K1, owner, k -> testRunner());
            registry.getOrCreate(K2, owner, k -> testRunner());

            assertThat(registry.close(K1)).isTrue();
            assertThat(registry.size()).isEqualTo(1);
            assertThat(registry.get(K1)).isNull();
            assertThat(registry.get(K2)).isNotNull();
        }
    }

    @Test
    void close_missing_key_is_noop_returning_false() {
        try (var registry = SessionExecutorRegistry.cleanerOwned()) {
            assertThat(registry.close(K1)).isFalse();
        }
    }

    @Test
    void close_all_removes_every_runner() {
        Object owner = new Object();
        var registry = SessionExecutorRegistry.cleanerOwned();
        registry.getOrCreate(K1, owner, k -> testRunner());
        registry.getOrCreate(K2, owner, k -> testRunner());
        registry.getOrCreate(K3, owner, k -> testRunner());
        assertThat(registry.size()).isEqualTo(3);

        registry.closeAll();
        assertThat(registry.size()).isEqualTo(0);
    }

    @Test
    void cleaner_owned_factory_yields_a_working_cleaner_registry() {
        Object owner = new Object();
        try (var registry = SessionExecutorRegistry.cleanerOwned()) {
            var r1 = registry.getOrCreate(K1, owner, k -> testRunner());
            var r2 = registry.getOrCreate(K1, owner, k -> testRunner());
            assertThat(r1).isSameInstanceAs(r2);
            assertThat(registry.size()).isEqualTo(1);
        }
    }

    @Test
    void owner_gc_triggers_cleaner_shutdown() throws Exception {
        // The whole point of the breaking-change redesign: when the
        // caller's lifetime-owner object becomes unreachable, the
        // Cleaner attached on first registration tears the runner down
        // — no possibility of leaking the orchestrator thread, the hot
        // PublishProcessor, or the executor's marking state. This is a
        // load-bearing test for the leak-prevention contract.
        var registry = SessionExecutorRegistry.cleanerOwned();
        var ownerRef = createAndForget(registry);
        // Sanity: the runner is registered while the owner is reachable.
        assertThat(registry.size()).isEqualTo(1);

        // Force collection of the owner and wait for the Cleaner to fire.
        awaitGc(() -> ownerRef.refersTo(null), 5_000);
        awaitGc(() -> registry.size() == 0, 5_000);

        assertThat(registry.size()).isEqualTo(0);
    }

    /**
     * Helper that registers a session with a fresh owner and returns a
     * weak reference to that owner. The owner is unreachable from this
     * method's caller as soon as this method returns — exactly what the
     * Cleaner needs to fire.
     */
    private static WeakReference<Object> createAndForget(SessionExecutorRegistry registry) {
        Object owner = new Object();
        registry.getOrCreate(K1, owner, k -> testRunner());
        return new WeakReference<>(owner);
    }

    @Test
    void explicit_close_then_owner_gc_is_safe() throws Exception {
        var registry = SessionExecutorRegistry.cleanerOwned();
        var ownerRef = createAndForget(registry);
        // Explicitly close before GC.
        assertThat(registry.close(K1)).isTrue();
        assertThat(registry.size()).isEqualTo(0);
        // Now let the Cleaner fire — it should find no entry, do nothing.
        awaitGc(() -> ownerRef.refersTo(null), 5_000);
        // Give the Cleaner thread a moment to invoke close(K1) — which is
        // a safe no-op. Just verify the registry is still healthy.
        Thread.sleep(100);
        assertThat(registry.size()).isEqualTo(0);
        registry.closeAll();
    }

    @Test
    void concurrent_first_call_installs_exactly_one_runner_and_closes_loser() throws Exception {
        Object owner = new Object();
        var registry = SessionExecutorRegistry.cleanerOwned();
        var calls = new AtomicInteger();
        var factoriesEntered = new CountDownLatch(2);
        var releaseFactories = new CountDownLatch(1);
        var created = new CopyOnWriteArrayList<PetriRunner>();

        PetriRunner[] runners;
        try (var pool = Executors.newFixedThreadPool(2)) {
            var f1 = pool.submit(() -> registry.getOrCreate(K1, owner, key -> {
                calls.incrementAndGet();
                factoriesEntered.countDown();
                awaitGate(releaseFactories);
                var runner = testRunner();
                created.add(runner);
                return runner;
            }));
            var f2 = pool.submit(() -> registry.getOrCreate(K1, owner, key -> {
                calls.incrementAndGet();
                factoriesEntered.countDown();
                awaitGate(releaseFactories);
                var runner = testRunner();
                created.add(runner);
                return runner;
            }));

            try {
                assertThat(factoriesEntered.await(2, TimeUnit.SECONDS)).isTrue();
                assertThat(calls.get()).isEqualTo(2);
            } finally {
                releaseFactories.countDown();
            }
            runners = new PetriRunner[] {
                    f1.get(5, TimeUnit.SECONDS),
                    f2.get(5, TimeUnit.SECONDS)
            };
        }

        assertThat(runners[0]).isSameInstanceAs(runners[1]);
        assertThat(created).hasSize(2);
        PetriRunner loser = created.get(0) == runners[0] ? created.get(1) : created.get(0);
        assertThat(loser.awaitTermination(Duration.ofSeconds(2))).isTrue();
        assertThat(registry.size()).isEqualTo(1);
        registry.closeAll();
    }

    private static void awaitGate(CountDownLatch gate) {
        try {
            gate.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new AssertionError("interrupted while waiting for concurrent factory gate", e);
        }
    }

    /** Polls until {@code condition} is true, forcing GC each iteration. */
    @SuppressWarnings("BusyWait")
    private static void awaitGc(java.util.function.BooleanSupplier condition,
                                long timeoutMillis) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (System.currentTimeMillis() < deadline) {
            if (condition.getAsBoolean()) return;
            System.gc();
            Thread.sleep(50);
        }
        // Throw rather than return. Returning on timeout made the callers'
        // headline assertion unfalsifiable: if the owner is never collected,
        // nothing is evicted, the registry size is whatever it already was,
        // and the test passed without ever exercising the GC path it names.
        throw new AssertionError(
                "owner was not collected within " + timeoutMillis + "ms; "
                        + "the GC-dependent property under test never ran");
    }

    private static PetriRunner testRunner() {
        // A minimal long-lived runner — we don't drive it, just verify identity.
        var llm = new BaseLlm("none") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.never();
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
        var config = LlmAgentSubnet.Config.builder("test-agent", "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build();
        var net = PetriNet.builder("test")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));
        return PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .orchestratorExecutor(EXECUTOR)
                .start();
    }
}
