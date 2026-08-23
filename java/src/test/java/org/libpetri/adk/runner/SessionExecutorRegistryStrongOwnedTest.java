package org.libpetri.adk.runner;

import static com.google.common.truth.Truth.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import io.reactivex.rxjava3.core.Flowable;
import java.lang.ref.WeakReference;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;

/**
 * Covers the {@link SessionExecutorRegistry#strongOwned()} mode added in
 * response to {@code ADK_FINDINGS.md} #2 (the cleaner-only-by-construction
 * owner-lifetime trap). The cleaner-owned path is covered by
 * {@link SessionExecutorRegistryTest}; here we verify the strong-owned
 * mode's distinguishing behaviours:
 * <ul>
 *   <li>Owner GC does <i>not</i> evict the entry — only explicit
 *       {@code close()} does.</li>
 *   <li>Same key + same owner identity reuses the runner.</li>
 *   <li>Same key + different owner still throws (de-dup contract).</li>
 * </ul>
 */
class SessionExecutorRegistryStrongOwnedTest {

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

    @Test
    void factories_return_distinct_mode_instances() {
        try (var strong = SessionExecutorRegistry.strongOwned();
             var cleaner = SessionExecutorRegistry.cleanerOwned()) {
            assertThat(strong).isNotSameInstanceAs(cleaner);
            assertThat(strong.size()).isEqualTo(0);
            assertThat(cleaner.size()).isEqualTo(0);
        }
    }

    @Test
    void strong_mode_pins_the_owner_so_gc_can_never_evict() throws Exception {
        // The headline behaviour of strongOwned(). This test used to be called
        // "does_not_evict_when_owner_is_collected" and waited for the owner to
        // be collected, which cannot happen: STRONG mode stores
        // OwnerRef.Strong(owner), a strong reference, so the registry itself
        // keeps the owner alive. The old wait silently timed out and the test
        // passed having exercised nothing.
        //
        // The real guarantee is the stronger one: under GC pressure the owner
        // is NOT collected, so no GC-driven eviction is even reachable, and
        // only an explicit close removes the entry. Flip OwnerRef.Strong to
        // Weak and the first assertion below fails.
        try (var registry = SessionExecutorRegistry.strongOwned()) {
            WeakReference<Object> ownerRef = registerAndForget(registry);

            applyGcPressure(500);

            assertThat(ownerRef.refersTo(null)).isFalse();
            assertThat(registry.size()).isEqualTo(1);

            // Explicit close is required and sufficient.
            assertThat(registry.close(K1)).isTrue();
            assertThat(registry.size()).isEqualTo(0);
        }
    }

    @Test
    void strong_mode_reuses_runner_for_same_owner() {
        Object owner = new Object();
        try (var registry = SessionExecutorRegistry.strongOwned()) {
            var r1 = registry.getOrCreate(K1, owner, k -> testRunner());
            var r2 = registry.getOrCreate(K1, owner, k -> testRunner());
            assertThat(r1).isSameInstanceAs(r2);
        }
    }

    @Test
    void strong_mode_rejects_different_owner_for_same_key() {
        try (var registry = SessionExecutorRegistry.strongOwned()) {
            Object owner1 = new Object();
            Object owner2 = new Object();
            registry.getOrCreate(K1, owner1, k -> testRunner());
            assertThrows(IllegalStateException.class,
                    () -> registry.getOrCreate(K1, owner2, k -> testRunner()));
            // Pin both refs past the assertion so neither is opportunistically GC'd.
            assertThat(owner1).isNotSameInstanceAs(owner2);
        }
    }

    /**
     * Registers an entry under {@link #K1} with a fresh owner and returns
     * a weak reference to that owner. The owner is unreachable from the
     * caller as soon as this returns.
     */
    private static WeakReference<Object> registerAndForget(SessionExecutorRegistry registry) {
        Object owner = new Object();
        registry.getOrCreate(K1, owner, k -> testRunner());
        return new WeakReference<>(owner);
    }

    /**
     * Runs GC repeatedly for a fixed window without asserting anything.
     *
     * <p>Used where the expected outcome is that an object is <i>not</i>
     * collected, so there is no condition to await; the point is to give a
     * collector every chance and then check the reference is still live.
     */
    @SuppressWarnings("BusyWait")
    private static void applyGcPressure(long millis) throws InterruptedException {
        long deadline = System.currentTimeMillis() + millis;
        while (System.currentTimeMillis() < deadline) {
            System.gc();
            Thread.sleep(50);
        }
    }

    private static PetriRunner testRunner() {
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
