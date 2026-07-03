package org.libpetri.adk.runner;

import java.lang.ref.Cleaner;
import java.lang.ref.Reference;
import java.lang.ref.WeakReference;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.function.Function;

/**
 * Lazy {@code Map<SessionKey, PetriRunner>} — preserves the
 * <b>one-net-per-user</b> invariant when a single shared
 * {@link PetriAgent} serves multiple ADK sessions.
 *
 * <h2>Two ownership modes</h2>
 * <p><b>Default to {@link #strongOwned()}.</b> It is safe under any
 * framework: entries live until you {@link #close(SessionKey)} them, and
 * a forgotten close is a <i>visible</i> leak ({@link #size()} grows
 * monotonically, asserts and metrics catch it). Reach for
 * {@link #cleanerOwned()} only when you genuinely have a stable
 * strong-referenced lifetime owner: its wrong-owner failure mode is
 * <i>invisible</i> (the runner is torn down silently and {@code inject}
 * no-ops; see below). Pick the mode that matches the strong-reference
 * chain your framework actually provides:
 * <dl>
 *   <dt>{@link #cleanerOwned()}</dt>
 *   <dd>Each per-session runner is bound to a caller-supplied
 *       <i>lifetime owner</i> via a {@link java.lang.ref.Cleaner}.
 *       When the owner becomes unreachable, the runner is torn down
 *       automatically — "forgot to call {@code close()}" leaks are
 *       structurally impossible. Use this when you have a stable
 *       strong-referenced object whose GC genuinely tracks session
 *       end (e.g. an application-level session-scope holder pinned by
 *       a strongly-keyed registry until {@code @OnClose} runs).</dd>
 *   <dt>{@link #strongOwned()}</dt>
 *   <dd>Entries are held strongly in a {@code ConcurrentHashMap} — no
 *       {@code WeakReference}, no {@code Cleaner}. Teardown happens
 *       exclusively via {@link #close(SessionKey)} / {@link #closeAll()}.
 *       Use this when your framework does not give you a clean strong
 *       owner (a common production trap: weak-keyed Caffeine
 *       caches, framework-internal weak references, etc.). "Forgot to
 *       call {@code close()}" becomes possible again, but it is
 *       <b>visible</b> — {@link #size()} grows monotonically — whereas
 *       a wrong-owner cleaner eviction is invisible (the runner
 *       silently shuts down and {@code inject} no-ops).</dd>
 * </dl>
 *
 * <p>The owner identity contract ({@code ==}-stable across calls,
 * different owner for the same key throws) applies in both modes —
 * it's how concurrent first-calls converge on a single runner.
 *
 * <h2>Owner identity is load-bearing (both modes)</h2>
 * <p>The {@code owner} object's <b>reference identity</b> controls
 * lifetime in cleaner-owned mode and is the de-duplication key in both
 * modes. Calling {@code getOrCreate} for the same key with a
 * <i>different</i> owner is a lifetime bug and throws
 * {@link IllegalStateException} — one owner per key, ever.
 *
 * <h2>Cleaner-owned: choosing a safe owner</h2>
 * <p>The owner must (a) be strongly referenced by the application for
 * exactly as long as "the session is alive" and (b) preserve reference
 * identity across every invocation for the same session. Typical safe
 * choices:
 * <ul>
 *   <li>The websocket connection / handler object, but only when the
 *       framework holds it strongly until disconnect.</li>
 *   <li>A {@code Map<SessionKey, Object>} the application maintains
 *       and clears explicitly on session end.</li>
 *   <li><b>Not</b> ADK's {@code Session} when backed by
 *       {@code InMemorySessionService} — it returns defensive copies,
 *       so every {@code runAsync} call sees a fresh instance and the
 *       cleaner fires prematurely.</li>
 *   <li><b>Not</b> objects held only by weak-keyed caches (e.g.
 *       Caffeine with {@code .weakKeys()}) — the very pattern that
 *       triggered the {@code strongOwned()} addition.</li>
 * </ul>
 * If none of these are available, use {@link #strongOwned()} and
 * call {@link #close(SessionKey)} from your session-end hook.
 */
public final class SessionExecutorRegistry implements AutoCloseable {

    /**
     * One shared {@link Cleaner} instance for the whole library. A
     * Cleaner spins up one daemon thread; consolidating into a single
     * instance keeps that to one thread for the JVM regardless of how
     * many registries / sessions exist.
     */
    private static final Cleaner CLEANER = Cleaner.create();

    /** Ownership semantics. See class javadoc. */
    private enum Mode { CLEANER, STRONG }

    private final ConcurrentMap<SessionKey, Entry> entries = new ConcurrentHashMap<>();
    private final Mode mode;

    /**
     * Slot per key. The owner reference is either weak (CLEANER mode,
     * so the registry never pins the owner — pinning would defeat the
     * leak-prevention mechanism) or strong (STRONG mode, so identity
     * survives without an external strong-reference chain). Only ever
     * dereferenced for {@code ==}-identity comparison.
     */
    private record Entry(PetriRunner runner, OwnerRef ownerRef) {}

    /**
     * Sealed abstraction over weak/strong owner refs so the
     * mode-dispatch in {@code getOrCreate} is one branch, not two
     * separate code paths.
     */
    private sealed interface OwnerRef {
        Object get();

        record Weak(WeakReference<Object> ref) implements OwnerRef {
            @Override public Object get() { return ref.get(); }
        }
        record Strong(Object owner) implements OwnerRef {
            @Override public Object get() { return owner; }
        }
    }

    private SessionExecutorRegistry(Mode mode) {
        this.mode = mode;
    }

    /**
     * Cleaner-owned registry. Per-session runners are torn down
     * automatically when their lifetime owner becomes unreachable.
     * Use when you have a stable strong-referenced lifetime owner.
     */
    public static SessionExecutorRegistry cleanerOwned() {
        return new SessionExecutorRegistry(Mode.CLEANER);
    }

    /**
     * Strong-owned registry. Entries live until explicit
     * {@link #close(SessionKey)} / {@link #closeAll()} is called. Use
     * when your framework does not give you a clean strong owner.
     */
    public static SessionExecutorRegistry strongOwned() {
        return new SessionExecutorRegistry(Mode.STRONG);
    }

    /**
     * Returns the runner for {@code key}, lazily creating it via
     * {@code factory} on first call. The {@code owner} object is used
     * as a de-duplication key in both modes and, in cleaner-owned
     * mode, also controls runner lifetime: when {@code owner} becomes
     * unreachable, a {@link Cleaner} action removes the entry and
     * drains the runner asynchronously without blocking the shared Cleaner daemon.
     *
     * <p>On the first call for {@code key}, {@code owner} is captured.
     * Subsequent calls for the same {@code key} must pass the
     * <i>same object identity</i> ({@code ==}); a different owner
     * throws {@link IllegalStateException}.
     *
     * <p>Thread-safe: under concurrent first-calls for the same key,
     * exactly one {@code factory.apply} result is retained and bound
     * to the cleaner (cleaner mode); the loser is shut down.
     * Subsequent same-key callers with the same owner identity see
     * the surviving runner.
     */
    public PetriRunner getOrCreate(SessionKey key,
                                   Object owner,
                                   Function<SessionKey, PetriRunner> factory) {
        Objects.requireNonNull(key, "key");
        Objects.requireNonNull(owner, "owner");
        Objects.requireNonNull(factory, "factory");

        // CAS retry loop. Iterations terminate as soon as we either reuse
        // an entry (same owner) or install a fresh one; we only loop on
        // a stale-entry eviction or a lost-CAS-with-stale-winner — both
        // are rare and each iteration makes forward progress (entry
        // removed or factory re-attempted).
        while (true) {
            // Fast path: existing entry. Verify owner identity matches.
            Entry existing = entries.get(key);
            if (existing != null) {
                PetriRunner reused = reuseIfSameOwner(key, existing, owner);
                if (reused != null) return reused;
                // Original owner was GC'd (cleaner mode only);
                // reuseIfSameOwner evicted the stale entry — retry to
                // install our own.
                continue;
            }

            PetriRunner created = factory.apply(key);
            Entry candidate = new Entry(created, makeOwnerRef(owner));
            Entry winner = entries.putIfAbsent(key, candidate);
            if (winner == null) {
                if (mode == Mode.CLEANER) {
                    // CRITICAL: this lambda must NOT capture `owner`. It
                    // captures `this` (the registry) and `key` (a record
                    // of three strings) — both unrelated to owner's
                    // reachability. If we captured owner here, the cleaner
                    // action would hold a strong ref through itself to
                    // owner, preventing collection forever.
                    CLEANER.register(owner, () -> drain(key));
                    // Keep the lifetime owner strongly reachable until
                    // after the Cleaner registration is installed. Otherwise
                    // an aggressive GC/JIT is allowed to clear `owner`
                    // between makeOwnerRef(owner) and register(owner, ...),
                    // leaving a weak candidate entry with no cleaner action.
                    Reference.reachabilityFence(owner);
                }
                return created;
            }

            // Lost a CAS race with a concurrent first-call. Shut down
            // our loser and reuse-or-retry against the winner.
            created.shutdown();
            PetriRunner reused = reuseIfSameOwner(key, winner, owner);
            if (reused != null) return reused;
            // Winner's owner went stale between the race and our check;
            // reuseIfSameOwner evicted it — loop and try again.
        }
    }

    private OwnerRef makeOwnerRef(Object owner) {
        return switch (mode) {
            case CLEANER -> new OwnerRef.Weak(new WeakReference<>(owner));
            case STRONG -> new OwnerRef.Strong(owner);
        };
    }

    /**
     * Returns the entry's runner iff the entry's original owner is
     * still the same identity as {@code candidate}. Returns {@code null}
     * if the original owner was GC'd (entry is stale — caller should
     * replace it; cleaner mode only) and throws if a different owner
     * is presented.
     */
    private PetriRunner reuseIfSameOwner(SessionKey key, Entry existing, Object candidate) {
        Object original = existing.ownerRef().get();
        if (original == candidate) {
            return existing.runner();
        }
        if (original == null) {
            // Stale entry — original owner was collected, but the Cleaner
            // hasn't fired (or it raced with us). Evict deterministically
            // so the caller's first-time call can take effect. Only
            // reachable in CLEANER mode (STRONG never returns null).
            evictAndShutdown(key, existing);
            return null;
        }
        throw new IllegalStateException(
                "SessionKey " + key + " is already bound to a different "
                + "lifetime owner. One owner per key, ever — sharing a key "
                + "across owners is a lifetime bug. If you are passing a "
                + "freshly-built per-call wrapper as the owner, hold a "
                + "stable identity for the session instead, or use "
                + "strongOwned(), which needs no external owner at all.");
    }

    /** Returns the runner if present (no creation), or {@code null}. */
    public PetriRunner get(SessionKey key) {
        Entry e = entries.get(key);
        return e == null ? null : e.runner();
    }

    /** Number of currently-registered sessions. */
    public int size() {
        return entries.size();
    }

    /**
     * Remove one session and start an asynchronous drain. Used only by
     * Cleaner actions: blocking the single shared Cleaner daemon on an
     * unbounded runner shutdown would delay cleanup for unrelated sessions.
     */
    private void drain(SessionKey key) {
        Entry removed = entries.remove(key);
        if (removed != null) {
            removed.runner().drainAsync();
        }
    }

    /**
     * Shut down and remove one session's runner. Idempotent. Returns
     * {@code true} if a runner was removed. Explicit close keeps
     * synchronous teardown semantics; the Cleaner path uses
     * non-blocking drain instead.
     */
    public boolean close(SessionKey key) {
        Entry removed = entries.remove(key);
        if (removed == null) return false;
        removed.runner().shutdown();
        return true;
    }

    /**
     * Shut down and remove every session's runner. Each per-key removal
     * goes through {@link #close(SessionKey)} so the atomic
     * {@code remove(key)} guarantees at-most-one teardown request per
     * runner — safe to call concurrently with Cleaner-driven evictions.
     */
    public void closeAll() {
        for (SessionKey k : entries.keySet()) {
            close(k);
        }
    }

    @Override
    public void close() {
        closeAll();
    }

    private void evictAndShutdown(SessionKey key, Entry expected) {
        if (entries.remove(key, expected)) {
            expected.runner().shutdown();
        }
    }
}
