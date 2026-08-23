package org.libpetri.adk.runner;

import com.google.adk.events.Event;
import io.reactivex.rxjava3.core.Flowable;
import java.time.Duration;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicReference;
import org.libpetri.core.EnvironmentPlace;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.adk.bridge.EventStoreToFlowableBridge;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.runtime.BitmapNetExecutor;
import org.libpetri.runtime.ActionFailureHandler;
import org.libpetri.runtime.ExecutionContextProvider;
import org.libpetri.runtime.PetriNetExecutor;
import org.libpetri.runtime.PrecompiledNetExecutor;

/**
 * Per-session handle around a long-lived {@link PetriNetExecutor}.
 *
 * <p>One {@code PetriRunner} = one user's net = one orchestrator thread.
 * Built once at session start, kept alive across many user messages, and
 * shut down when the session ends.
 *
 * <h2>Where actions run</h2>
 * <p>libpetri calls {@code action.execute(ctx)} <i>inline</i>: it never
 * submits actions anywhere. Every transition action therefore runs on the
 * single thread hosting the orchestrator loop, the one taken from
 * {@link Builder#orchestratorExecutor(ExecutorService)}. Make that a
 * virtual-thread executor when actions block, since a blocking action holds
 * that thread and serialises the net for its duration. Fan-out needs an
 * explicit worker pool inside the action itself, the way
 * {@code ToolDispatchSubnet} takes a {@code dispatchExecutor}.
 * {@link Builder#actionExecutor(ExecutorService)}, despite its name, never
 * dispatched actions and is deprecated.
 *
 * <h2>Interaction model</h2>
 * <p>Two surfaces, deliberately asymmetric:
 * <ul>
 *   <li><b>Ingress (generic):</b> declare any number of typed env places
 *       via {@link Builder#environmentPlace(Place)} and inject tokens
 *       from any thread via {@link #inject(Place, Object)}. This is the
 *       <i>only</i> way external producers hand tokens to the net —
 *       chat content, scroll events, sensor readings, webhook payloads
 *       all go through this surface.</li>
 *   <li><b>Egress (narrow, ADK-specific):</b> {@link #adkEvents()} is a
 *       hot {@link Flowable} hard-wired to {@link AdkColours#EVENT_OUT}
 *       — it is the ADK {@code Runner} contract bridge, <i>not</i> a
 *       general observation API. For other egress paths, chain an
 *       {@link EventStore} decorator via {@link Builder#eventStore}
 *       (observability) or model the egress as an in-net transition
 *       action consuming a typed place (side effects).</li>
 * </ul>
 *
 * <h2>Executor impl is the caller's choice</h2>
 * <p>The underlying executor is typed against the
 * {@link PetriNetExecutor} interface, not a concrete impl. Choose
 * between {@link BitmapNetExecutor} (default, bitmap enablement
 * tracking) and {@link PrecompiledNetExecutor} (precompiles to
 * bytecode, typically faster for hot per-session nets) via
 * {@link Builder#executorFactory(ExecutorFactory)}, or supply your own
 * factory for debug-wrapped or other impls.
 *
 * <h2>Lifecycle</h2>
 * <ol>
 *   <li>{@link Builder#start()} builds the executor with the configured
 *       env places and event-store chain, then submits
 *       {@code executor.run()} to the caller-supplied orchestrator pool.</li>
 *   <li>{@link #inject(Place, Object)} (or {@link #envPlace(Place)} +
 *       {@code executor.inject(...)}) injects from any thread; returns
 *       a future that completes when the orchestrator has accepted
 *       the token.</li>
 *   <li>{@link #adkEvents()} returns the same hot Flowable on every
 *       call — late subscribers miss earlier events (PublishProcessor
 *       semantics).</li>
 *   <li>Teardown: choose by your caller-thread budget.
 *       {@link #drainAsync()} stops accepting new injects and returns
 *       immediately (fire-and-forget — right for {@code @OnClose} and
 *       other low-latency hooks). {@link #awaitTermination(Duration)}
 *       bounded-waits for the orchestrator to finish. {@link #shutdown()}
 *       composes them as drain-then-unbounded-wait — synchronous, blocks
 *       the calling thread.</li>
 * </ol>
 *
 * <h2>Skippable</h2>
 * <p>{@code PetriRunner} is convenience sugar over the
 * {@link PetriNetExecutor} interface for the ADK-integrated case.
 * Users who don't need the ADK egress bridge (libpetri-only demos,
 * tests) can construct a {@link PetriNetExecutor} directly via the
 * concrete builders — the interaction model (env-place inject +
 * EventStore observation) is the same.
 */
public final class PetriRunner implements AutoCloseable {

    private final PetriNetExecutor executor;
    private final Map<Place<?>, EnvironmentPlace<?>> envPlaces;
    private final EventStoreToFlowableBridge bridge;
    private final Future<?> orchestratorTask;

    private PetriRunner(PetriNetExecutor executor,
                        Map<Place<?>, EnvironmentPlace<?>> envPlaces,
                        EventStoreToFlowableBridge bridge,
                        Future<?> orchestratorTask) {
        this.executor = executor;
        this.envPlaces = envPlaces;
        this.bridge = bridge;
        this.orchestratorTask = orchestratorTask;
    }

    /**
     * Type-safe lookup of a registered env-place handle. Throws
     * {@link IllegalArgumentException} if {@code place} was not
     * declared via {@link Builder#environmentPlace(Place)}.
     */
    @SuppressWarnings("unchecked")
    public <T> EnvironmentPlace<T> envPlace(Place<T> place) {
        Objects.requireNonNull(place, "place");
        EnvironmentPlace<?> env = envPlaces.get(place);
        if (env == null) {
            throw new IllegalArgumentException(
                    "Place " + place + " was not declared as an env place on this PetriRunner. "
                    + "Add .environmentPlace(" + place + ") to the builder.");
        }
        return (EnvironmentPlace<T>) env;
    }

    /**
     * Inject a token onto a registered env place. The returned future
     * completes when the orchestrator has accepted the token (not when
     * downstream actions are done — observe {@link #adkEvents()} or
     * an EventStore decorator for execution progress).
     */
    public <T> CompletableFuture<Boolean> inject(Place<T> place, T token) {
        Objects.requireNonNull(token, "token");
        return executor.inject(envPlace(place), token);
    }

    /**
     * Inject a pre-built {@link Token} onto a registered env place. Use this
     * overload for the unit token ({@link Token#unit()}) on a
     * {@link Place}{@code <Void>} signal place, which the value overload cannot
     * express (it rejects {@code null}). Prefer {@link #signal(Place)} for the
     * common void-signal case.
     */
    public <T> CompletableFuture<Boolean> inject(Place<T> place, Token<T> token) {
        Objects.requireNonNull(token, "token");
        return executor.inject(envPlace(place), token);
    }

    /**
     * Inject the unit token onto a {@link Place}{@code <Void>} signal place:
     * voice-activity edges, barge-in interrupts, {@link AdkColours#END_INVOCATION},
     * and every other control signal whose presence (not value) carries the
     * information. This is the void-shaped counterpart to
     * {@link #inject(Place, Object)}; both go through the same env-place surface,
     * so there is no reason to drop to {@link #executor()} for signals.
     */
    public CompletableFuture<Boolean> signal(Place<Void> place) {
        return executor.inject(envPlace(place), Token.unit());
    }

    /**
     * Hot {@link Flowable} of {@link Event} tokens produced into
     * {@link AdkColours#EVENT_OUT}. This is the ADK {@code Runner}
     * contract bridge — <b>not</b> a generic observation API. Multiple
     * subscribers share the upstream; late subscribers do not see past
     * events.
     */
    public Flowable<Event> adkEvents() {
        return bridge.asFlowable();
    }

    /**
     * Hot {@link Flowable} of transition failures on this net, one
     * {@code onNext} each, terminating only when the net does.
     *
     * <p>A failure does <b>not</b> terminate {@link #adkEvents()}. libpetri
     * contains an action failure to its own transition and keeps the
     * orchestrator running (EXEC-031); this runner keeps its egress alive to
     * match, so one failed tool call cannot end a session that is still
     * perfectly able to serve the next turn.
     *
     * <p>The cost is that a caller waiting for a turn's terminal event has to
     * decide for itself when a failure means that turn is over: merge this for
     * the life of the turn and drop it afterwards. {@code PetriAgent} does
     * exactly that. Without it, a transition that was going to produce the
     * turn's only {@code EVENT_OUT} would fail and the caller would wait
     * forever.
     *
     * <p>For observability rather than control flow, use the
     * {@link EventStore} decorator chain, which sees every failure whether or
     * not anyone subscribes here.
     */
    public Flowable<Throwable> failureSignal() {
        return bridge.failureSignal();
    }

    /**
     * Fire-and-forget drain — stops accepting new injects and lets
     * in-flight actions finish on the orchestrator thread. Returns
     * immediately; the orchestrator terminates and
     * {@link #adkEvents()} {@code onCompletes} on its own time. Use
     * this from latency-sensitive hooks ({@code @OnClose} etc.) where
     * blocking the caller is not acceptable.
     */
    public void drainAsync() {
        executor.drain();
    }

    /**
     * Wait up to {@code timeout} for the orchestrator to terminate.
     * Returns {@code true} if the orchestrator finished within the
     * budget, {@code false} on timeout. Call after {@link #drainAsync()}
     * (or any other path that triggers drain) when you need to confirm
     * teardown before proceeding. Clears the interrupt status if the
     * wait is interrupted; the caller can re-check
     * {@code Thread.currentThread().isInterrupted()} afterwards.
     */
    public boolean awaitTermination(Duration timeout) {
        Objects.requireNonNull(timeout, "timeout");
        try {
            orchestratorTask.get(timeout.toNanos(), TimeUnit.NANOSECONDS);
            return true;
        } catch (TimeoutException e) {
            return false;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        } catch (ExecutionException e) {
            // Orchestrator threw — task is done, just not happy. Counts
            // as terminated.
            return true;
        }
    }

    /**
     * Graceful synchronous shutdown — composes {@link #drainAsync()}
     * with an unbounded {@link #awaitTermination(Duration)}. Blocks
     * the calling thread until the orchestrator terminates and
     * {@link #adkEvents()} {@code onCompletes}. Use
     * {@link #drainAsync()} + {@link #awaitTermination(Duration)}
     * separately when caller-thread latency matters.
     */
    public void shutdown() {
        drainAsync();
        try {
            orchestratorTask.get();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } catch (ExecutionException ignored) {
            // Orchestrator threw — task done.
        }
    }

    @Override
    public void close() {
        shutdown();
    }

    /** Direct access to the underlying executor for advanced uses. */
    public PetriNetExecutor executor() {
        return executor;
    }

    public static Builder builder(PetriNet net) {
        return new Builder(net);
    }

    /**
     * Action-failure policy installed by the built-in {@link ExecutorFactory}
     * implementations.
     *
     * <p>Since libpetri 2.13 an unchecked throw from a transition action fails
     * <i>that transition only</i> and the tokens it consumed are lost
     * (EXEC-031); the orchestrator survives. libpetri's own default logs a
     * WARNING, but only when no {@code EventStore} recorded a
     * {@code TransitionFailed} for the failure. This builder defaults to
     * {@link EventStore#noop()}, whose append succeeds while recording
     * nothing, so on the default wiring libpetri considers the failure
     * observed and stays quiet, and a throwing action becomes a silent hole
     * in the marking. That is precisely the failure mode design commitment #2
     * ("the marking IS the state") exists to prevent, so the built-in
     * factories always install a handler.
     *
     * <p>Callers wanting different handling supply their own
     * {@link ExecutorFactory} (the documented extension point) rather than
     * a setter here, which would have to widen the factory's signature.
     * A custom factory that wants the standard behaviour should install
     * {@link ExecutorFactory#loudActionFailure()} rather than omit a handler,
     * since omitting one restores the silent hole described above.
     */
    private static final ActionFailureHandler LOUD_ACTION_FAILURE = (transition, cause) ->
            System.getLogger("org.libpetri.adk.runner").log(
                    System.Logger.Level.WARNING,
                    "Transition '" + transition.name() + "' action failed; its consumed tokens "
                            + "are lost (libpetri EXEC-031). The orchestrator continues.",
                    cause);

    /**
     * Strategy for constructing a {@link PetriNetExecutor}. Hides the minor
     * builder differences between {@link BitmapNetExecutor} and
     * {@link PrecompiledNetExecutor}. The library ships {@link #bitmap()} and
     * {@link #precompiled()}; callers can supply their own, for example a
     * debug-wrapped executor decorator.
     *
     * <p>A custom factory takes on two obligations the built-ins already meet,
     * and getting either wrong fails quietly rather than loudly:
     *
     * <ul>
     *   <li>Propagate {@code contextProvider} to the executor builder (e.g.
     *       {@code BitmapNetExecutor.builder(...).executionContextProvider(contextProvider)}).
     *       It carries the ambient {@link ExecutionContextProvider} configured
     *       on the builder, or {@link ExecutionContextProvider#NOOP} when none
     *       was set; drop it and transition actions lose ambient-context
     *       propagation such as tracing and baggage.</li>
     *   <li>Install an {@link ActionFailureHandler}, normally
     *       {@link #loudActionFailure()}. Without one, a throwing action is
     *       silent on this runner's default {@code EventStore.noop()} wiring
     *       and its consumed tokens vanish from the marking unreported.</li>
     * </ul>
     *
     * <p>{@code orchestratorExecutor} hosts libpetri's own loop under
     * {@code run(Duration)}. Note that actions are invoked inline rather than
     * dispatched, so it does not dispatch anything on this path; see
     * {@link Builder#orchestratorExecutor(ExecutorService)}.
     */
    @FunctionalInterface
    public interface ExecutorFactory {
        PetriNetExecutor build(PetriNet net,
                               Map<Place<?>, List<Token<?>>> initialMarking,
                               Collection<EnvironmentPlace<?>> envPlaces,
                               EventStore eventStore,
                               ExecutorService orchestratorExecutor,
                               ExecutionContextProvider contextProvider);

        /**
         * The action-failure policy the built-in factories install: log a
         * WARNING naming the transition and the token loss. Exposed so a custom
         * factory can reuse it instead of silently going without.
         */
        static ActionFailureHandler loudActionFailure() {
            return LOUD_ACTION_FAILURE;
        }

        /** Default: {@link BitmapNetExecutor}. */
        static ExecutorFactory bitmap() {
            return (net, initial, envs, store, exec, ctx) -> BitmapNetExecutor.builder(net, initial)
                    .environmentPlaces(envs.toArray(EnvironmentPlace[]::new))
                    .eventStore(store)
                    .orchestratorExecutor(exec)
                    .executionContextProvider(ctx)
                    .uncaughtActionHandler(LOUD_ACTION_FAILURE)
                    .build();
        }

        /** Precompiled: {@link PrecompiledNetExecutor}. Faster for hot per-session nets. */
        static ExecutorFactory precompiled() {
            return (net, initial, envs, store, exec, ctx) -> PrecompiledNetExecutor.builder(net, initial)
                    .environmentPlaces(envs.toArray(EnvironmentPlace[]::new))
                    .eventStore(store)
                    .orchestratorExecutor(exec)
                    .executionContextProvider(ctx)
                    .uncaughtActionHandler(LOUD_ACTION_FAILURE)
                    .build();
        }
    }

    public static final class Builder {
        private final PetriNet net;
        private final LinkedHashMap<Place<?>, EnvironmentPlace<?>> envPlaces = new LinkedHashMap<>();
        private EventStore primaryEventStore = EventStore.noop();
        private ExecutorService actionExecutor;
        private ExecutorService orchestratorExecutor;
        private Map<Place<?>, List<Token<?>>> initialMarking = Map.of();
        private ExecutorFactory executorFactory = ExecutorFactory.bitmap();
        private ExecutionContextProvider contextProvider = ExecutionContextProvider.NOOP;
        private AtomicReference<PetriNetExecutor> deferredExecutorRef;

        private Builder(PetriNet net) {
            this.net = Objects.requireNonNull(net, "net");
        }

        /**
         * Declare {@code place} as an env place — external producers can
         * inject tokens onto it via {@link PetriRunner#inject(Place, Object)}.
         * Each call wraps {@code place} in a fresh
         * {@link EnvironmentPlace}. Use
         * {@link #environmentPlace(EnvironmentPlace)} instead if you
         * already hold a pre-built {@code EnvironmentPlace} instance
         * (e.g. shared constants the rest of your app injects against).
         */
        public <T> Builder environmentPlace(Place<T> place) {
            Objects.requireNonNull(place, "place");
            return environmentPlace(EnvironmentPlace.of(place));
        }

        /**
         * Register a pre-built {@link EnvironmentPlace}. Use this when
         * your application maintains its own {@code EnvironmentPlace}
         * constants (e.g. a {@code Set<EnvironmentPlace<?>>} consumed by
         * both this runner and direct executor callers) so the runner
         * stores the caller's exact instance instead of wrapping the
         * underlying {@link Place} a second time.
         */
        public <T> Builder environmentPlace(EnvironmentPlace<T> env) {
            Objects.requireNonNull(env, "env");
            Place<T> place = env.place();
            if (envPlaces.containsKey(place)) {
                throw new IllegalStateException(
                        "Place " + place + " already declared as an env place");
            }
            envPlaces.put(place, env);
            return this;
        }

        /** Bulk overload of {@link #environmentPlace(Place)}. */
        public Builder environmentPlaces(Place<?>... places) {
            Objects.requireNonNull(places, "places");
            for (Place<?> p : places) environmentPlace(p);
            return this;
        }

        /** Bulk overload of {@link #environmentPlace(EnvironmentPlace)}. */
        public Builder environmentPlaces(EnvironmentPlace<?>... envs) {
            Objects.requireNonNull(envs, "envs");
            for (EnvironmentPlace<?> e : envs) environmentPlace(e);
            return this;
        }

        /**
         * Bulk overload taking a {@link java.util.Set} of pre-built
         * {@link EnvironmentPlace} constants — matches the upstream
         * {@code BitmapNetExecutor.Builder.environmentPlaces(Set)}
         * convention and the {@code Set<EnvironmentPlace<?>>} pattern
         * applications typically maintain for cross-call injection.
         */
        public Builder environmentPlaces(java.util.Set<EnvironmentPlace<?>> envs) {
            Objects.requireNonNull(envs, "envs");
            for (EnvironmentPlace<?> e : envs) environmentPlace(e);
            return this;
        }

        /**
         * Primary {@link EventStore} chained inside the runner. The
         * runner always wraps it with an
         * {@link EventStoreToFlowableBridge} watching
         * {@link AdkColours#EVENT_OUT} for the ADK egress, so this is
         * the place for observability decorators
         * ({@code EventStore.logging()}, {@code OtelEventStore},
         * structured-logging stores, etc.).
         */
        public Builder eventStore(EventStore primary) {
            this.primaryEventStore = Objects.requireNonNull(primary, "eventStore");
            return this;
        }

        /**
         * Optional, and misnamed: this executor never ran subnet actions.
         *
         * <p>It is handed to libpetri's executor builder, which uses it to
         * host the orchestrator loop under {@code run(Duration)} and nothing
         * else ("the configured ExecutorService hosts exactly one task").
         * This runner drives the no-arg {@code run()} on
         * {@link #orchestratorExecutor(ExecutorService)} instead, so on this
         * path the pool passed here is never used at all. Actions are invoked
         * inline, on whichever thread runs the orchestrator loop. Pinned by
         * {@code PetriRunnerTest.actions_run_on_the_orchestrator_executor_not_the_action_executor}.
         *
         * <p>Put your virtual-thread executor on
         * {@link #orchestratorExecutor(ExecutorService)}, which is the pool
         * that actually runs actions. Tool fan-out has its own executor
         * ({@code ToolDispatchSubnet}'s {@code dispatchExecutor}), which is a
         * genuine worker pool because that action submits to it explicitly.
         *
         * <p>Retained and still accepted so existing callers keep compiling;
         * no longer required. Scheduled for removal in 1.0.
         *
         * @deprecated never dispatched actions and is unused on this path.
         *     Configure {@link #orchestratorExecutor(ExecutorService)}.
         */
        @Deprecated(since = "0.4", forRemoval = true)
        public Builder actionExecutor(ExecutorService exec) {
            this.actionExecutor = Objects.requireNonNull(exec, "actionExecutor");
            return this;
        }

        /**
         * Required: executor hosting the orchestrator loop. One task per
         * runner is submitted to it.
         *
         * <p>Because libpetri invokes {@code action.execute(ctx)} inline
         * rather than submitting it anywhere, this is also the pool on which
         * every transition action runs. A blocking action (a synchronous
         * model call, say) therefore occupies this thread for its duration
         * and serialises the net, which is why
         * {@code Executors.newVirtualThreadPerTaskExecutor()} is the right
         * choice when actions block.
         */
        public Builder orchestratorExecutor(ExecutorService exec) {
            this.orchestratorExecutor = Objects.requireNonNull(exec, "orchestratorExecutor");
            return this;
        }

        /** Seed the initial marking (default: empty). */
        public Builder initialMarking(Map<Place<?>, List<Token<?>>> marking) {
            this.initialMarking = Objects.requireNonNull(marking, "initialMarking");
            return this;
        }

        /**
         * Choose the libpetri executor implementation. Defaults to
         * {@link ExecutorFactory#bitmap()}; switch to
         * {@link ExecutorFactory#precompiled()} for hot per-session
         * nets where compile-once pays off.
         */
        public Builder executorFactory(ExecutorFactory factory) {
            this.executorFactory = Objects.requireNonNull(factory, "executorFactory");
            return this;
        }

        /**
         * Ambient {@link ExecutionContextProvider} forwarded to the
         * executor builder. The provider is invoked once per transition
         * firing and its result is exposed via
         * {@code TransitionContext.executionContext(Class)} to action
         * bodies — wire a tracing provider here to give every action a
         * span scoped under the workflow root. Defaults to
         * {@link ExecutionContextProvider#NOOP}.
         *
         * <p>Note: this is the only sanctioned use of
         * {@code ExecutionContextProvider}. For observability that does
         * <i>not</i> involve action-side ambient context (counting
         * transition fires, structured logging, OT spans per fire),
         * chain an {@link EventStore} decorator via
         * {@link #eventStore(EventStore)} instead.
         */
        public Builder executionContextProvider(ExecutionContextProvider provider) {
            this.contextProvider = Objects.requireNonNull(provider, "executionContextProvider");
            return this;
        }

        /**
         * Register an {@link AtomicReference} that {@link #start()} populates
         * with the built {@link PetriNetExecutor} before the orchestrator is
         * submitted. Streaming subnets ({@code LlmStreamingStepSubnet}) whose
         * actions self-inject onto an env place need the executor handle, but
         * the handle only exists after build. Pass the same reference you gave
         * to the subnet's {@code Config.executorRef(...)}; this removes the
         * manual post-build {@code ref.set(...)} step and its ordering trap.
         */
        public Builder deferredExecutorRef(AtomicReference<PetriNetExecutor> ref) {
            this.deferredExecutorRef = Objects.requireNonNull(ref, "deferredExecutorRef");
            return this;
        }

        /** Build the executor, submit it to the orchestrator pool, and return the running runner. */
        public PetriRunner start() {
            if (orchestratorExecutor == null) {
                throw new IllegalStateException("orchestratorExecutor must be set");
            }
            var bridge = new EventStoreToFlowableBridge(AdkColours.EVENT_OUT, primaryEventStore);

            // actionExecutor is optional and inert (see its setter). When a
            // caller supplies one we still hand it over, since libpetri would
            // otherwise create and own a pool it never uses. When absent, fall
            // back to the orchestrator pool so the factory contract, which
            // does not accept null, stays satisfied.
            var executor = executorFactory.build(
                    net,
                    initialMarking,
                    envPlaces.values(),
                    bridge,
                    actionExecutor != null ? actionExecutor : orchestratorExecutor,
                    contextProvider);

            if (deferredExecutorRef != null) {
                deferredExecutorRef.set(executor);
            }

            var task = orchestratorExecutor.submit((Runnable) executor::run);
            return new PetriRunner(executor, Map.copyOf(envPlaces), bridge, task);
        }
    }
}
