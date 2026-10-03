package org.libpetri.adk;

import static com.google.common.truth.Truth.assertThat;
import static org.junit.jupiter.api.Assertions.assertTimeoutPreemptively;

import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;

class ManualClockTest {

    private static final long SECOND = TimeUnit.SECONDS.toNanos(1);

    /**
     * The executor reads the clock, computes its delay from that reading,
     * and only then calls {@code awaitWork}. An advance in between used to be
     * absorbed: the wait took the advanced time as its starting point and
     * parked past a timer that was already due.
     */
    @Test
    void an_advance_between_the_reading_and_the_wait_is_not_lost() {
        var clock = new ManualClock();
        long read = clock.nanoTime();
        clock.advance(Duration.ofSeconds(1));   // the test thread, in the window

        assertThat(read).isEqualTo(0);
        assertTimeoutPreemptively(Duration.ofSeconds(2),
                () -> clock.awaitWork(() -> false, SECOND));
    }

    /** settle() returns only once the wait has parked; it throws if the wait never does. */
    @Test
    void a_timer_not_yet_due_parks_until_an_advance() throws Exception {
        var clock = new ManualClock();
        clock.advance(Duration.ofSeconds(5));
        var waiter = new AtomicReference<CompletableFuture<Void>>();
        clock.settle(() -> waiter.set(CompletableFuture.runAsync(() -> {
            clock.nanoTime();
            clock.awaitWork(() -> false, SECOND);
        })));

        assertThat(waiter.get().isDone()).isFalse();
        clock.advance(Duration.ofMillis(1));
        waiter.get().get(2, TimeUnit.SECONDS);
    }

    /** No timer ({@code Long.MAX_VALUE}) is never due, but any advance still wakes the wait. */
    @Test
    void a_wait_without_a_timer_returns_on_any_advance() throws Exception {
        var clock = new ManualClock();
        var waiter = new AtomicReference<CompletableFuture<Void>>();
        clock.settle(() -> waiter.set(CompletableFuture.runAsync(
                () -> clock.awaitWork(() -> false, Long.MAX_VALUE))));

        assertThat(waiter.get().isDone()).isFalse();
        clock.advance(Duration.ofMillis(1));
        waiter.get().get(2, TimeUnit.SECONDS);
    }
}
