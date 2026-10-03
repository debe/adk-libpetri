package org.libpetri.adk.runner;

import java.lang.ref.Cleaner;
import java.lang.ref.Reference;
import java.lang.ref.WeakReference;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ExecutionException;
import java.util.function.Function;
import java.util.logging.Level;
import java.util.logging.Logger;
import org.libpetri.adk.Experimental;
import org.libpetri.core.Token;
import org.libpetri.runtime.TerminationReason;

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
 * <p>In strong-owned mode the owner is only that identity check, so it can
 * be left out: {@link #getOrCreate(SessionKey, Function)} takes no owner,
 * and is what a {@code PetriAgent} built without an owner extractor calls.
 * Pick one form per key. Mixing the ownerless call with an explicit owner
 * for the same key throws, like any other owner mismatch.
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
 *
 * <h2>Checkpointing registries (experimental)</h2>
 * <p>A registry built with a {@link SessionCheckpointStore} saves a
 * session's final marking when its runner is torn down. Teardown drains
 * first: the runner refuses new injects, actions in flight complete, and
 * only the marking the run ends in is saved, so nothing the session
 * accepted is left out. Until that save is done the key stays taken:
 * {@link #getOrCreate(SessionKey, Object, Function) getOrCreate} for it
 * waits, so a replacement runner always resumes from what the old one
 * left, and a key never has two serving runners. A run that does not
 * drain to quiescence within the checkpoint timeout (or that ends any
 * other way) has its checkpoint removed rather than left stale, and the
 * next runner starts fresh. {@link #discard(SessionKey)} ends a session
 * without saving it.
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

    /**
     * The owner {@link #getOrCreate(SessionKey, Function)} records in
     * strong-owned mode, where an owner is only an identity and never
     * controls lifetime. One shared instance, so every ownerless call for a
     * key agrees with every other.
     */
    private static final Object NO_OWNER = new Object() {
        @Override public String toString() { return "<no owner>"; }
    };

    private static final Logger LOG = Logger.getLogger(SessionExecutorRegistry.class.getName());

    /**
     * How long a checkpointing teardown waits, by default, for a session to
     * drain before it gives up on saving it. Long enough for an in-flight
     * model call to land; a replacement runner for the key waits as long.
     */
    @Experimental
    public static final Duration DEFAULT_CHECKPOINT_TIMEOUT = Duration.ofSeconds(30);

    private final ConcurrentMap<SessionKey, Slot> slots = new ConcurrentHashMap<>();
    private final Mode mode;
    private final SessionCheckpointStore checkpoints;   // nullable
    private final Duration checkpointTimeout;

    /**
     * What the map holds for one key. {@link Live} is a serving runner.
     * {@link Closing} takes its place, atomically, the moment teardown
     * starts, and stays until the runner is drained and its final marking
     * saved (or its checkpoint removed). A caller that finds one waits on
     * it, so nothing can create a replacement that resumes from a store the
     * old runner has yet to write.
     */
    private sealed interface Slot {

        /**
         * The owner reference is either weak (CLEANER mode, so the registry
         * never pins the owner — pinning would defeat the leak-prevention
         * mechanism) or strong (STRONG mode, so identity survives without an
         * external strong-reference chain). Only ever dereferenced for
         * {@code ==}-identity comparison.
         */
        record Live(PetriRunner runner, OwnerRef ownerRef) implements Slot {}

        /** Completes, normally and always, once the key is free again. */
        record Closing(CompletableFuture<Void> done) implements Slot {
            Closing() {
                this(new CompletableFuture<>());
            }
        }
    }

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

    private SessionExecutorRegistry(Mode mode, SessionCheckpointStore checkpoints,
                                    Duration checkpointTimeout) {
        this.mode = mode;
        this.checkpoints = checkpoints;
        this.checkpointTimeout = checkpointTimeout;
    }

    /**
     * Cleaner-owned registry. Per-session runners are torn down
     * automatically when their lifetime owner becomes unreachable.
     * Use when you have a stable strong-referenced lifetime owner.
     */
    public static SessionExecutorRegistry cleanerOwned() {
        return new SessionExecutorRegistry(Mode.CLEANER, null, DEFAULT_CHECKPOINT_TIMEOUT);
    }

    /**
     * {@link #cleanerOwned()} that also saves each session's final marking
     * to {@code checkpoints} when its runner is torn down. See
     * {@link SessionCheckpointStore} and the class javadoc.
     */
    @Experimental
    public static SessionExecutorRegistry cleanerOwned(SessionCheckpointStore checkpoints) {
        return cleanerOwned(checkpoints, DEFAULT_CHECKPOINT_TIMEOUT);
    }

    /**
     * {@link #cleanerOwned(SessionCheckpointStore)} with a bound other than
     * {@link #DEFAULT_CHECKPOINT_TIMEOUT} on how long teardown waits for a
     * session to drain before it gives up on saving it.
     */
    @Experimental
    public static SessionExecutorRegistry cleanerOwned(SessionCheckpointStore checkpoints,
                                                       Duration checkpointTimeout) {
        return new SessionExecutorRegistry(Mode.CLEANER,
                Objects.requireNonNull(checkpoints, "checkpoints"),
                Objects.requireNonNull(checkpointTimeout, "checkpointTimeout"));
    }

    /**
     * Strong-owned registry. Entries live until explicit
     * {@link #close(SessionKey)} / {@link #closeAll()} is called. Use
     * when your framework does not give you a clean strong owner.
     */
    public static SessionExecutorRegistry strongOwned() {
        return new SessionExecutorRegistry(Mode.STRONG, null, DEFAULT_CHECKPOINT_TIMEOUT);
    }

    /**
     * {@link #strongOwned()} that also saves each session's final marking
     * to {@code checkpoints} on {@link #close(SessionKey)} and
     * {@link #closeAll()}. See {@link SessionCheckpointStore} and the class
     * javadoc.
     */
    @Experimental
    public static SessionExecutorRegistry strongOwned(SessionCheckpointStore checkpoints) {
        return strongOwned(checkpoints, DEFAULT_CHECKPOINT_TIMEOUT);
    }

    /**
     * {@link #strongOwned(SessionCheckpointStore)} with a bound other than
     * {@link #DEFAULT_CHECKPOINT_TIMEOUT} on how long teardown waits for a
     * session to drain before it gives up on saving it.
     */
    @Experimental
    public static SessionExecutorRegistry strongOwned(SessionCheckpointStore checkpoints,
                                                      Duration checkpointTimeout) {
        return new SessionExecutorRegistry(Mode.STRONG,
                Objects.requireNonNull(checkpoints, "checkpoints"),
                Objects.requireNonNull(checkpointTimeout, "checkpointTimeout"));
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
     *
     * <p>While the key's previous runner is being torn down, this waits
     * for that teardown to finish (bounded by the checkpoint timeout plus
     * the store's own latency) before calling {@code factory}, so a factory
     * that resumes from a checkpoint store reads what the previous runner
     * saved.
     *
     * @throws IllegalStateException for a different owner, or if the
     *                               thread is interrupted while waiting for
     *                               the key's previous runner to close (the
     *                               interrupt status is set again)
     */
    public PetriRunner getOrCreate(SessionKey key,
                                   Object owner,
                                   Function<SessionKey, PetriRunner> factory) {
        Objects.requireNonNull(key, "key");
        Objects.requireNonNull(owner, "owner");
        Objects.requireNonNull(factory, "factory");

        // CAS retry loop. Iterations terminate as soon as we either reuse
        // an entry (same owner) or install a fresh one; we only loop on
        // a stale-entry eviction, a closing slot, or a lost CAS whose winner
        // is stale or closing. Each iteration makes forward progress (entry
        // removed, teardown awaited, or factory re-attempted).
        while (true) {
            Slot existing = slots.get(key);
            if (existing instanceof Slot.Closing closing) {
                awaitClosedOrThrow(key, closing);
                continue;
            }
            if (existing instanceof Slot.Live live) {
                // Fast path: existing entry. Verify owner identity matches.
                PetriRunner reused = reuseIfSameOwner(key, live, owner);
                if (reused != null) return reused;
                // Original owner was GC'd (cleaner mode only);
                // reuseIfSameOwner evicted the stale entry — retry to
                // install our own.
                continue;
            }

            PetriRunner created = factory.apply(key);
            var candidate = new Slot.Live(created, makeOwnerRef(owner));
            Slot winner = slots.putIfAbsent(key, candidate);
            if (winner == null) {
                if (mode == Mode.CLEANER) {
                    // CRITICAL: this lambda must NOT capture `owner`. It
                    // captures `this` (the registry), `key` (a record of
                    // three strings) and a weak reference to `candidate` —
                    // none of them keeps owner reachable. If we captured
                    // owner here, the cleaner action would hold a strong
                    // ref through itself to owner, preventing collection
                    // forever. `candidate` (not just `key`) is what lets the
                    // action tell its own slot from a successor's installed
                    // after a stale eviction. It is held weakly because it
                    // holds the runner: an explicitly closed runner must not
                    // stay reachable until its owner is collected. While the
                    // slot is in the map, the map keeps it alive for the
                    // action; once it is gone, there is nothing to drain.
                    var slotRef = new WeakReference<>(candidate);
                    CLEANER.register(owner, () -> {
                        var live = slotRef.get();
                        if (live != null) drain(key, live);
                    });
                    // Keep the lifetime owner strongly reachable until
                    // after the Cleaner registration is installed. Otherwise
                    // an aggressive GC/JIT is allowed to clear `owner`
                    // between makeOwnerRef(owner) and register(owner, ...),
                    // leaving a weak candidate entry with no cleaner action.
                    Reference.reachabilityFence(owner);
                }
                return created;
            }

            // Lost a CAS race: with a concurrent first-call, or with a
            // teardown that began after our check (then `created` may have
            // resumed from a checkpoint the closing runner is about to
            // replace). Shut down our loser and reuse-or-retry.
            created.shutdown();
            if (winner instanceof Slot.Live live) {
                PetriRunner reused = reuseIfSameOwner(key, live, owner);
                if (reused != null) return reused;
            }
            // Winner's owner went stale between the race and our check
            // (reuseIfSameOwner evicted it), or the winner is closing —
            // loop and try again.
        }
    }

    /**
     * Strong-owned shorthand for {@link #getOrCreate(SessionKey, Object, Function)}
     * without a lifetime owner. In strong-owned mode the owner is only an
     * identity check (teardown is {@link #close(SessionKey)}), so a caller
     * with nothing meaningful to pass can omit it.
     *
     * @throws IllegalStateException in cleaner-owned mode, where the owner
     *                               is what tears the runner down and must
     *                               be supplied
     */
    public PetriRunner getOrCreate(SessionKey key, Function<SessionKey, PetriRunner> factory) {
        if (mode == Mode.CLEANER) {
            throw new IllegalStateException(
                    "A cleanerOwned() registry needs a lifetime owner: its runner is torn "
                    + "down when the owner becomes unreachable. Pass one, or use "
                    + "strongOwned() and close(SessionKey) from your session-end hook.");
        }
        return getOrCreate(key, NO_OWNER, factory);
    }

    /** Whether this registry ties runner lifetime to an owner (see {@link #cleanerOwned()}). */
    boolean isCleanerOwned() {
        return mode == Mode.CLEANER;
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
    private PetriRunner reuseIfSameOwner(SessionKey key, Slot.Live existing, Object candidate) {
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
        if (original == NO_OWNER || candidate == NO_OWNER) {
            throw new IllegalStateException(
                    "SessionKey " + key + " was first requested "
                    + (original == NO_OWNER ? "without an owner" : "with an owner")
                    + " and is now requested "
                    + (candidate == NO_OWNER ? "without one" : "with one")
                    + ". Use one form per key: the ownerless getOrCreate(key, factory) "
                    + "everywhere, or the same owner everywhere. A PetriAgent built "
                    + "without an owner extractor uses the ownerless form.");
        }
        throw new IllegalStateException(
                "SessionKey " + key + " is already bound to a different "
                + "lifetime owner. One owner per key, ever — sharing a key "
                + "across owners is a lifetime bug. If you are passing a "
                + "freshly-built per-call wrapper as the owner, hold a "
                + "stable identity for the session instead: a websocket "
                + "session, or a value from your own per-session map. "
                + "Note ctx.session() is NOT stable under "
                + "InMemorySessionService, which returns defensive copies.");
    }

    /**
     * Returns the runner if present (no creation), or {@code null}. A
     * runner being torn down is no longer present.
     */
    public PetriRunner get(SessionKey key) {
        return slots.get(key) instanceof Slot.Live live ? live.runner() : null;
    }

    /** Number of sessions with a serving runner; one being torn down no longer counts. */
    public int size() {
        return (int) slots.values().stream().filter(Slot.Live.class::isInstance).count();
    }

    /**
     * Tear down {@code live} for a Cleaner action. A checkpointing teardown
     * waits for the session to drain, so it runs on its own virtual thread:
     * blocking the single shared Cleaner daemon on it would delay cleanup
     * for unrelated sessions. A plain one only starts the drain and is done.
     */
    private void drain(SessionKey key, Slot.Live live) {
        var closing = new Slot.Closing();
        if (!slots.replace(key, live, closing)) return;   // closed or evicted already
        if (checkpoints == null) {
            settle(key, live.runner(), closing, true);
            return;
        }
        Thread.ofVirtual().name("petri-checkpoint-" + key)
                .start(() -> settle(key, live.runner(), closing, true));
    }

    /**
     * Shut down and remove one session's runner, saving its final marking
     * first if this registry checkpoints. Idempotent. Returns {@code true}
     * if this call tore a runner down; {@code false} if there was none, or
     * if another teardown of the key was already under way (this call then
     * waits for it to finish). Explicit close keeps synchronous teardown
     * semantics: it returns once the runner has terminated, or the thread
     * is interrupted. The Cleaner path uses a non-blocking drain instead.
     */
    public boolean close(SessionKey key) {
        return end(key, true);
    }

    /**
     * End a session for good: like {@link #close(SessionKey)}, but the
     * final marking is not saved and any checkpoint the store holds for
     * {@code key} is removed, so the next runner for the key starts fresh.
     * Removes the checkpoint even when no runner is registered. Returns
     * {@code true} if this call tore a runner down.
     */
    @Experimental
    public boolean discard(SessionKey key) {
        return end(key, false);
    }

    /**
     * Shut down and remove every session's runner. Each per-key removal
     * goes through {@link #close(SessionKey)} so the atomic
     * {@code Live -> Closing} replacement guarantees at-most-one teardown
     * per runner — safe to call concurrently with Cleaner-driven evictions.
     */
    public void closeAll() {
        for (SessionKey k : slots.keySet()) {
            close(k);
        }
    }

    @Override
    public void close() {
        closeAll();
    }

    private boolean end(SessionKey key, boolean save) {
        Objects.requireNonNull(key, "key");
        while (true) {
            Slot slot = slots.get(key);
            if (slot == null) {
                if (!save) removeCheckpoint(key);
                return false;
            }
            if (slot instanceof Slot.Closing closing) {
                // Someone else's teardown: wait for it. A close is then
                // done, and must not tear down a successor a getOrCreate
                // installed meanwhile. A discard looks again: it still has
                // the checkpoint that teardown saved to remove.
                if (!awaitClosed(closing) || save) return false;
                continue;
            }
            var live = (Slot.Live) slot;
            var closing = new Slot.Closing();
            if (!slots.replace(key, live, closing)) continue;
            try {
                settle(key, live.runner(), closing, save);
            } finally {
                live.runner().shutdown();
            }
            return true;
        }
    }

    private void evictAndShutdown(SessionKey key, Slot.Live stale) {
        var closing = new Slot.Closing();
        if (!slots.replace(key, stale, closing)) return;
        try {
            // Saved before the caller creates the replacement runner, so a
            // factory that resumes from the store picks this marking up.
            settle(key, stale.runner(), closing, true);
        } finally {
            stale.runner().shutdown();
        }
    }

    /**
     * Drain {@code runner}, then save its final marking ({@code save}) or
     * remove its checkpoint, then free the key, which {@code closing} holds
     * until then. The key is freed whatever happens, an {@link Error} from
     * the store included; teardown past this point is the caller's.
     */
    private void settle(SessionKey key, PetriRunner runner, Slot.Closing closing, boolean save) {
        try {
            // Refuse new injects first, so the marking saved below is final:
            // nothing the session accepts afterwards can be left out of it.
            runner.drainAsync();
            if (checkpoints != null) {
                if (save) {
                    saveFinalMarking(key, runner);
                } else {
                    removeCheckpoint(key);
                }
            }
        } finally {
            slots.remove(key, closing);
            closing.done().complete(null);
        }
    }

    /**
     * Waits up to the checkpoint timeout for the drained {@code runner} to
     * terminate, then saves the marking its run ended in. A run that did
     * not end at quiescence in time has no marking to resume from, and its
     * previous checkpoint is removed rather than left for the next runner
     * to restore.
     */
    private void saveFinalMarking(SessionKey key, PetriRunner runner) {
        if (!runner.awaitTermination(checkpointTimeout)) {
            if (Thread.currentThread().isInterrupted()) {
                LOG.warning(() -> "Interrupted while session " + key + " drained; not "
                        + "checkpointed, and its earlier checkpoint is removed.");
            } else {
                LOG.warning(() -> "Session " + key + " did not drain within "
                        + checkpointTimeout + "; not checkpointed, and its earlier "
                        + "checkpoint is removed.");
            }
            removeCheckpoint(key);
            return;
        }
        Optional<Map<String, List<Token<?>>>> marking;
        try {
            marking = runner.checkpointMarking();
        } catch (RuntimeException e) {
            LOG.log(Level.WARNING, e, () -> "Session " + key + " cannot be checkpointed; "
                    + "its earlier checkpoint is removed.");
            removeCheckpoint(key);
            return;
        }
        if (marking.isEmpty()) {
            TerminationReason reason = runner.executor().terminationReason();
            // A terminal place is a designed end: nothing to resume, nothing to warn about.
            LOG.log(reason == TerminationReason.TERMINAL ? Level.FINE : Level.WARNING,
                    () -> "Session " + key + " ended " + reason + ", not quiescent after "
                            + "its drain, so there is no marking to resume from; its "
                            + "earlier checkpoint is removed.");
            removeCheckpoint(key);
            return;
        }
        try {
            checkpoints.save(key, marking.get());
        } catch (RuntimeException e) {
            LOG.log(Level.WARNING, e, () -> "Checkpointing session " + key + " failed; "
                    + "its earlier checkpoint is removed.");
            removeCheckpoint(key);
        } catch (Error e) {
            removeCheckpoint(key);
            throw e;
        }
    }

    /** Removes {@code key}'s checkpoint, if this registry has a store; never throws a RuntimeException. */
    private void removeCheckpoint(SessionKey key) {
        if (checkpoints == null) return;
        try {
            checkpoints.remove(key);
        } catch (RuntimeException e) {
            LOG.log(Level.WARNING, e, () -> "Removing the checkpoint of session " + key
                    + " failed; a later resume may restore an older marking.");
        }
    }

    /** Waits for a teardown to free its key. {@code false} if interrupted (status set again). */
    private static boolean awaitClosed(Slot.Closing closing) {
        try {
            closing.done().get();
            return true;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        } catch (ExecutionException e) {
            return true;   // unreachable: done only ever completes normally
        }
    }

    private static void awaitClosedOrThrow(SessionKey key, Slot.Closing closing) {
        if (!awaitClosed(closing)) {
            throw new IllegalStateException(
                    "Interrupted while waiting for session " + key + " to finish closing");
        }
    }
}
