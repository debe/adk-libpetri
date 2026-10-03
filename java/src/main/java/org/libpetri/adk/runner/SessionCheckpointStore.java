package org.libpetri.adk.runner;

import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import org.libpetri.adk.Experimental;
import org.libpetri.core.Token;

/**
 * Where a session's marking goes when its runner is torn down, and where a
 * new runner for the same session resumes from.
 *
 * <p>The marking stays the state (design commitment 2). A checkpoint is a
 * write-only export at session end, read back only to seed a fresh executor
 * before it starts, never while a net is running. Wire it on both sides:
 *
 * <pre>{@code
 * var checkpoints = SessionCheckpointStore.inMemory();
 * var registry = SessionExecutorRegistry.strongOwned(checkpoints);   // saves on close
 * var agent = PetriAgent.builder("agent", registry,
 *         key -> PetriRunner.builder(net)
 *             .environmentPlace(AdkColours.USER_IN)
 *             .resumeFrom(checkpoints, key)                           // restores on create
 *             .orchestratorExecutor(exec)
 *             .start())
 *     .build();
 * }</pre>
 *
 * <p><b>What is saved.</b> The registry drains the runner (it refuses new
 * injects and lets actions in flight complete) and saves the marking the
 * run ends in, so a checkpoint is always a restore point and holds
 * everything the session accepted. It leaves out egress places whose tokens
 * nothing in the net consumes: {@link org.libpetri.adk.colours.AdkColours#EVENT_OUT}
 * always, and whatever else the runner names with
 * {@link PetriRunner.Builder#excludeFromCheckpoint}. Restored, those tokens
 * would be stale and pile up across resumes.
 *
 * <p><b>When there is nothing to save,</b> the registry {@linkplain #remove
 * removes} the key's checkpoint rather than leave an older one to be
 * restored: when the run does not drain to quiescence within the registry's
 * checkpoint timeout, when it ended any other way (a terminal place, an
 * orchestrator failure), when {@link #save} throws, and on
 * {@link SessionExecutorRegistry#discard(SessionKey)}.
 *
 * <p>The marking maps place names to tokens, as libpetri's
 * {@code SnapshotResult.marking()} does. Token values are your own types
 * ({@code Content}, {@code LlmRequest}, ...), so a durable store owns their
 * encoding; {@link #inMemory()} keeps the objects as they are. Restoring
 * restarts every timer: a {@code delayed} transition waits its full delay
 * again.
 */
@Experimental
public interface SessionCheckpointStore {

    /** Records {@code marking} as the latest checkpoint for {@code key}. */
    void save(SessionKey key, Map<String, List<Token<?>>> marking);

    /** The latest checkpoint for {@code key}, if any. */
    Optional<Map<String, List<Token<?>>>> load(SessionKey key);

    /**
     * Forgets {@code key}'s checkpoint, so {@link #load} finds none and the
     * next runner starts fresh. A no-op when there is none.
     */
    void remove(SessionKey key);

    /** A process-local store holding the token objects themselves. */
    static SessionCheckpointStore inMemory() {
        var checkpoints = new ConcurrentHashMap<SessionKey, Map<String, List<Token<?>>>>();
        return new SessionCheckpointStore() {
            @Override
            public void save(SessionKey key, Map<String, List<Token<?>>> marking) {
                checkpoints.put(Objects.requireNonNull(key, "key"), Map.copyOf(marking));
            }

            @Override
            public Optional<Map<String, List<Token<?>>>> load(SessionKey key) {
                return Optional.ofNullable(checkpoints.get(key));
            }

            @Override
            public void remove(SessionKey key) {
                checkpoints.remove(Objects.requireNonNull(key, "key"));
            }
        };
    }
}
