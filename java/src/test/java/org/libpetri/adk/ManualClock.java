package org.libpetri.adk;

import java.time.Duration;
import java.time.Instant;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.ReentrantLock;
import java.util.function.BooleanSupplier;
import org.libpetri.runtime.ExecutionEnvironment;

/**
 * A virtual clock for libpetri executors (TIME-015) that a test thread
 * advances by hand, so timed transitions fire at exact logical instants
 * with no real waiting.
 *
 * <p>Unlike libpetri's own single-threaded test clock, this one serves an
 * executor running on another thread while the test injects and advances
 * from its own. {@link #awaitWork} blocks until the executor has work
 * ({@code ready}) or the test moves time, and returns on any advance so the
 * executor re-reads the clock and recomputes its next deadline.
 *
 * <p>{@link #settle(Runnable)} is what makes a test deterministic: it runs
 * an action (an inject or an advance), then waits until the executor has
 * woken, acted on it and parked again. Only then is the next step taken, so
 * a timer's start is always recorded before time moves past it.
 *
 * <p>Pair with {@code deadlineTolerance(Duration.ZERO)}: the default 5 ms
 * tolerance exists for real-clock jitter, which a virtual clock has none of.
 */
public final class ManualClock implements ExecutionEnvironment {

    private static final long POLL_NANOS = TimeUnit.MILLISECONDS.toNanos(1);
    private static final Duration SETTLE_TIMEOUT = Duration.ofSeconds(5);

    private final ReentrantLock lock = new ReentrantLock();
    private final Condition changed = lock.newCondition();

    private volatile long nanos;
    /**
     * The last {@link #nanoTime()} each thread read. The executor computes
     * {@code delayNanos} from its own last reading, so the timer it waits on
     * is due at that reading plus the delay, not at whatever the clock says
     * once {@link #awaitWork} takes the lock.
     */
    private final ThreadLocal<Long> lastRead = ThreadLocal.withInitial(() -> 0L);
    /** Times the executor has started blocking in {@link #awaitWork}. */
    private long parks;
    private boolean parked;

    @Override
    public long nanoTime() {
        long n = nanos;
        lastRead.set(n);
        return n;
    }

    @Override
    public Instant now() {
        return Instant.EPOCH.plusNanos(nanos);
    }

    @Override
    public void awaitWork(BooleanSupplier ready, long delayNanos) {
        if (delayNanos <= 0 || ready.getAsBoolean()) return;
        // Due at the reading the delay was computed from. An advance between
        // that reading and this point would otherwise be absorbed into
        // `entry` below, and the executor would park past a due timer until
        // the next advance. Long.MAX_VALUE means no timer: never due.
        long due = delayNanos == Long.MAX_VALUE
                ? Long.MAX_VALUE
                : saturatingAdd(lastRead.get(), delayNanos);
        lock.lock();
        try {
            if (nanos >= due) return;
            long entry = nanos;
            parks++;
            parked = true;
            changed.signalAll();
            // Injected work does not signal this condition, so poll ready on a
            // short real-time interval. Time itself only moves on advance(),
            // and any advance returns, so the executor re-reads the clock and
            // settle() sees it park again.
            while (!ready.getAsBoolean() && nanos == entry) {
                changed.awaitNanos(POLL_NANOS);
            }
        } catch (InterruptedException e) {
            // Contract: restore the flag and return; the executor reads it.
            Thread.currentThread().interrupt();
        } finally {
            parked = false;
            changed.signalAll();
            lock.unlock();
        }
    }

    private static long saturatingAdd(long a, long b) {
        long sum = a + b;
        return ((a ^ sum) & (b ^ sum)) < 0 ? Long.MAX_VALUE : sum;
    }

    /** Moves logical time forward by {@code d} and wakes the executor. */
    public void advance(Duration d) {
        lock.lock();
        try {
            nanos += d.toNanos();
            changed.signalAll();
        } finally {
            lock.unlock();
        }
    }

    /** {@link #advance} and wait for the executor to act on it. */
    public void advanceAndSettle(Duration d) {
        settle(() -> advance(d));
    }

    /**
     * Runs {@code action}, then waits until the executor has parked again
     * after it, i.e. has processed whatever the action caused and is idle,
     * waiting on its next timer, or waiting on an action still in flight.
     *
     * <p>That last case means "settled" is not "done": an executor with an
     * asynchronous action in flight parks too (with no timer, so
     * {@code Long.MAX_VALUE}), and this returns while the action is still
     * running. A test that needs the action's output waits for it by a
     * property of the marking or the egress, not by settling.
     */
    public void settle(Runnable action) {
        long before;
        lock.lock();
        try {
            before = parks;
        } finally {
            lock.unlock();
        }
        action.run();
        lock.lock();
        try {
            long remaining = SETTLE_TIMEOUT.toNanos();
            while (!(parked && parks > before)) {
                if (remaining <= 0) {
                    throw new AssertionError("executor did not settle within " + SETTLE_TIMEOUT);
                }
                remaining = changed.awaitNanos(remaining);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new AssertionError("interrupted while settling", e);
        } finally {
            lock.unlock();
        }
    }
}
