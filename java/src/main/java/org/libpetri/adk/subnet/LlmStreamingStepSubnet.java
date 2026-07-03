package org.libpetri.adk.subnet;

import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
import org.libpetri.adk.Experimental;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.core.Arc;
import org.libpetri.core.EnvironmentPlace;
import org.libpetri.core.Place;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.runtime.PetriNetExecutor;

/**
 * Streaming variant of {@link LlmStepSubnet} — does true incremental
 * env-place injection per partial chunk and bounds the in-flight chunk
 * count via a marking-based budget (so the bound is SMT-verifiable).
 *
 * <h2>Why env-place injection (not batched ctx.output)</h2>
 * <p>A naive "collect the {@code Flowable} into a list then produce N
 * chunk tokens via {@code ctx.output(CHUNK, chunk)}" implementation
 * would bypass the env-place interaction model — subscribers wouldn't
 * see partials until the LLM call fully completes (because libpetri
 * produces a transition's outputs only when its action's
 * {@link CompletionStage} completes). That defeats the point of
 * streaming.
 *
 * <p>This subnet instead has the {@code T_LlmCallStream} action call
 * {@code executor.inject(chunkEnv, chunk)} once per partial chunk as
 * the underlying {@link io.reactivex.rxjava3.core.Flowable} emits, then
 * inject a terminal merged-response marker after every partial has been
 * accepted. {@code T_EmitChunk} consumes those markers in arrival order:
 * partial markers emit partial {@link Event}s, and the terminal marker
 * releases the merged {@link LlmResponse} to the downstream router.
 *
 * <h2>Budgeting — boundedness via marking</h2>
 * <p>{@code T_EmitChunk} requires <i>both</i> a {@code CHUNK} token
 * (env-injected) <i>and</i> a {@code CHUNK_BUDGET} permit from a
 * fixed pool seeded at K tokens by {@code T_SeedAndStart} on every
 * new request. The emit transition consumes one permit and produces
 * one back, so {@code CHUNK_BUDGET} is structurally
 * <b>K-invariant</b>: it never exceeds and never drops below K (modulo
 * the brief window while the emit action is in-flight). This is the
 * SMT-verifiable property — {@code PlaceBound(CHUNK_BUDGET, K)} via
 * {@code AdkNetInvariants.reaskBudgetIsBounded(...)} — that proves
 * the at-most-K-concurrent-emissions invariant holds across all
 * reachable markings.
 *
 * <p>The env-side {@code CHUNK} queue itself is not strictly
 * marking-bounded (env-place injections from outside the net's arc
 * semantics) — bounding it requires producer-side back-pressure
 * (e.g. an in-action {@link java.util.concurrent.Semaphore} that
 * {@code T_EmitChunk} releases via a side callback). The example
 * below shows the simpler version that bounds <i>emission concurrency</i>
 * (K-bounded permit pool); producer-side rate-matching is a load-
 * specific extension layered on top.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [LLM_REQUEST] --T_SeedAndStart--> Out.and([LLM_REQUEST_INTERNAL],
 *                                              [CHUNK_BUDGET])
 *                  reset(CHUNK_BUDGET); action seeds K budget tokens
 *
 *   [LLM_REQUEST_INTERNAL] --T_LlmCallStream--> (no direct output)
 *                            side effect: inject N partial chunks, then
 *                            one terminal merged response into CHUNK env place
 *
 *   [CHUNK]env + [CHUNK_BUDGET] --T_EmitChunk-->
 *       Out.xor(Out.and([EVENT_OUT], [CHUNK_BUDGET]),
 *               Out.and([LLM_RESPONSE], [CHUNK_BUDGET]))
 * </pre>
 *
 * <h2>Executor wiring</h2>
 * <p>{@code T_LlmCallStream}'s action needs an executor handle (to
 * call {@code executor.inject}). The {@link Config} holds a
 * {@link AtomicReference} typed against the
 * {@link PetriNetExecutor} interface. When wiring through the ADK
 * adapter, pass the same reference to both this subnet's config and
 * {@code PetriRunner.Builder.deferredExecutorRef(...)} so the runner
 * populates it before the orchestrator starts:
 *
 * <pre>{@code
 * var execRef = new AtomicReference<PetriNetExecutor>();
 * var config = LlmStreamingStepSubnet.Config.builder("agent").chunkBudget(8)
 *     .executorRef(execRef).build();
 *
 * var agent = PetriAgent.of(name, desc, registry,
 *     key -> PetriRunner.builder(net)
 *         .environmentPlace(LlmStreamingStepSubnet.Places.CHUNK)
 *         .deferredExecutorRef(execRef)
 *         .actionExecutor(EXEC).orchestratorExecutor(EXEC).start(),
 *     ownerProvider);
 * }</pre>
 */
@Experimental
public final class LlmStreamingStepSubnet {

    public static final String NAME = "LlmStreamingStep";

    public static final class Transitions {
        public static final String SEED_AND_START   = NAME + "_SeedAndStart";
        public static final String LLM_CALL_STREAM  = NAME + "_LlmCallStream";
        public static final String EMIT_CHUNK       = NAME + "_EmitChunk";
        private Transitions() {}
    }

    public static final class Places {
        /** Internal request handoff — keeps LLM_REQUEST as a clean boundary. */
        public static final Place<LlmRequest> LLM_REQUEST_INTERNAL =
                Place.of(NAME + "_llmRequestInternal", LlmRequest.class);

        /**
         * Per-chunk arrival queue — wrap as {@link EnvironmentPlace} at
         * executor build time. Each injection is a separate partial
         * {@link LlmResponse} produced by the streaming LLM.
         */
        public static final Place<LlmResponseChunk> CHUNK =
                Place.of(NAME + "_chunk", LlmResponseChunk.class);

        /**
         * Budget place — seeded with K {@link Void} permits per request.
         * Structurally K-invariant since {@code T_EmitChunk} consumes
         * and returns one permit per fire. The K bound is the
         * SMT-verifiable concurrent-emission constraint.
         */
        public static final Place<Void> CHUNK_BUDGET =
                Place.of(NAME + "_chunkBudget", Void.class);

        private Places() {}
    }

    /** Typed wrapper for the chunk env place — distinguishes from the merged LlmResponse colour. */
    public record LlmResponseChunk(LlmResponse partial, boolean terminal) {
        public LlmResponseChunk(LlmResponse partial) {
            this(partial, false);
        }
    }

    public record Config(
            String author,
            Supplier<String> invocationIdSupplier,
            int chunkBudget,
            AtomicReference<PetriNetExecutor> executorRef) {

        public Config {
            Objects.requireNonNull(author, "author");
            Objects.requireNonNull(invocationIdSupplier, "invocationIdSupplier");
            Objects.requireNonNull(executorRef, "executorRef");
            if (chunkBudget < 1) {
                throw new IllegalArgumentException("chunkBudget must be >= 1, got: " + chunkBudget);
            }
        }

        public static Builder builder(String author) { return new Builder(author); }

        public static final class Builder {
            private final String author;
            private Supplier<String> invocationIdSupplier = () -> UUID.randomUUID().toString();
            private int chunkBudget = 4;
            private AtomicReference<PetriNetExecutor> executorRef;
            private Builder(String author) { this.author = author; }
            public Builder invocationIdSupplier(Supplier<String> s) { this.invocationIdSupplier = s; return this; }
            public Builder chunkBudget(int n) { this.chunkBudget = n; return this; }
            public Builder executorRef(AtomicReference<PetriNetExecutor> ref) { this.executorRef = ref; return this; }
            public Config build() {
                return new Config(author, invocationIdSupplier, chunkBudget,
                        Objects.requireNonNull(executorRef, "executorRef must be set before build"));
            }
        }
    }

    public static final SubnetDef<Void> DEF = SubnetDef.builder(NAME)
            .place(AdkColours.LLM_REQUEST)
            .place(AdkColours.LLM_RESPONSE)
            .place(AdkColours.EVENT_OUT)
            .place(Places.LLM_REQUEST_INTERNAL)
            .place(Places.CHUNK)
            .place(Places.CHUNK_BUDGET)
            .transition(Transition.builder(Transitions.SEED_AND_START)
                    .inputs(Arc.In.one(AdkColours.LLM_REQUEST))
                    .reset(Places.CHUNK_BUDGET)
                    .outputs(Arc.Out.and(Places.LLM_REQUEST_INTERNAL, Places.CHUNK_BUDGET))
                    .build())
            .transition(Transition.builder(Transitions.LLM_CALL_STREAM)
                    .inputs(Arc.In.one(Places.LLM_REQUEST_INTERNAL))
                    .build())
            .transition(Transition.builder(Transitions.EMIT_CHUNK)
                    .inputs(Arc.In.one(Places.CHUNK), Arc.In.one(Places.CHUNK_BUDGET))
                    .outputs(Arc.Out.xor(
                            Arc.Out.and(AdkColours.EVENT_OUT, Places.CHUNK_BUDGET),
                            Arc.Out.and(AdkColours.LLM_RESPONSE, Places.CHUNK_BUDGET)))
                    .priority(20)
                    .build())
            .inputPort("llmRequest",   AdkColours.LLM_REQUEST)
            .outputPort("eventOut",    AdkColours.EVENT_OUT)
            .outputPort("llmResponse", AdkColours.LLM_RESPONSE)
            .build();

    public static Map<String, TransitionAction> actionBindings(BaseLlm baseLlm, Config config) {
        Objects.requireNonNull(baseLlm, "baseLlm");
        Objects.requireNonNull(config, "config");
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.SEED_AND_START,  seedAndStartAction(config));
        session.put(Transitions.LLM_CALL_STREAM, llmCallStreamAction(baseLlm, config));
        session.put(Transitions.EMIT_CHUNK,      emitChunkAction(config));
        return SubnetActions.bind(DEF, session);
    }

    // ============================================================
    //  Actions
    // ============================================================

    private static TransitionAction seedAndStartAction(Config config) {
        return ctx -> {
            var request = ctx.input(AdkColours.LLM_REQUEST);
            ctx.output(Places.LLM_REQUEST_INTERNAL, request);
            // Seed K permits into the budget place. Reset arc already wiped
            // any stale survivors from a previous request, so the count after
            // this fire is exactly K.
            for (int i = 0; i < config.chunkBudget(); i++) {
                ctx.output(Places.CHUNK_BUDGET, (Void) null);
            }
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction llmCallStreamAction(BaseLlm baseLlm, Config config) {
        return ctx -> {
            var request = ctx.input(Places.LLM_REQUEST_INTERNAL);
            CompletableFuture<Void> done = new CompletableFuture<>();
            var collected = new ArrayList<LlmResponse>();
            var injections = new ArrayList<CompletableFuture<Boolean>>();
            var chunkEnv = EnvironmentPlace.of(Places.CHUNK);

            var executor = config.executorRef().get();
            if (executor == null) {
                done.completeExceptionally(new IllegalStateException(
                        "Config.executorRef has not been populated. Set the"
                                + " AtomicReference after BitmapNetExecutor.build()"
                                + " and before runAsync()."));
                return done;
            }

            baseLlm.generateContent(request, /* stream */ true)
                    .subscribe(
                            chunk -> {
                                collected.add(chunk);
                                // True incremental injection: each partial chunk lands
                                // on CHUNK as the model stream produces it. Completion
                                // waits until all partial injections are accepted, then
                                // appends one terminal CHUNK marker carrying the merged
                                // response. Because T_EmitChunk has higher priority than
                                // Router, the terminal cannot overtake queued partials.
                                injections.add(executor.inject(chunkEnv, new LlmResponseChunk(chunk)));
                            },
                            err -> done.completeExceptionally(err),
                            () -> {
                                if (collected.isEmpty()) {
                                    done.completeExceptionally(new IllegalStateException(
                                            "BaseLlm streaming call yielded no chunks"));
                                    return;
                                }
                                CompletableFuture<?>[] accepted = injections.toArray(CompletableFuture<?>[]::new);
                                CompletableFuture.allOf(accepted).whenComplete((_, err) -> {
                                    if (err != null) {
                                        done.completeExceptionally(err);
                                        return;
                                    }
                                    for (var injection : injections) {
                                        if (!injection.join()) {
                                            done.completeExceptionally(new IllegalStateException(
                                                    "chunk injection was rejected before streaming completed"));
                                            return;
                                        }
                                    }
                                    var terminal = executor.inject(chunkEnv,
                                            new LlmResponseChunk(mergeChunks(collected), true));
                                    terminal.whenComplete((acceptedTerminal, terminalErr) -> {
                                        if (terminalErr != null) {
                                            done.completeExceptionally(terminalErr);
                                            return;
                                        }
                                        if (!acceptedTerminal) {
                                            done.completeExceptionally(new IllegalStateException(
                                                    "terminal chunk injection was rejected before streaming completed"));
                                            return;
                                        }
                                        done.complete(null);
                                    });
                                });
                            });
            return done;
        };
    }

    private static TransitionAction emitChunkAction(Config config) {
        return ctx -> {
            LlmResponseChunk chunk = ctx.input(Places.CHUNK);
            ctx.input(Places.CHUNK_BUDGET);   // consume permit
            if (chunk.terminal()) {
                ctx.output(AdkColours.LLM_RESPONSE, chunk.partial());
            } else {
                Event partial = Event.builder()
                        .invocationId(config.invocationIdSupplier().get())
                        .author(config.author())
                        .content(chunk.partial().content().orElse(null))
                        .partial(Boolean.TRUE)
                        .build();
                ctx.output(AdkColours.EVENT_OUT, partial);
            }
            ctx.output(Places.CHUNK_BUDGET, (Void) null);   // return permit
            return CompletableFuture.completedFuture(null);
        };
    }

    /**
     * Merge N partial {@link LlmResponse} chunks into a single
     * {@link LlmResponse} by concatenating their content parts in
     * stream order. Sets {@code turnComplete=true}. Used for the
     * single {@link AdkColours#LLM_RESPONSE} token that downstream
     * {@link RouterSubnet} consumes.
     */
    static LlmResponse mergeChunks(List<LlmResponse> chunks) {
        var mergedParts = new ArrayList<Part>();
        for (var c : chunks) {
            c.content().ifPresent(content -> content.parts().ifPresent(mergedParts::addAll));
        }
        return LlmResponse.builder()
                .content(Content.builder().role("model").parts(mergedParts).build())
                .turnComplete(Boolean.TRUE)
                .build();
    }

    private LlmStreamingStepSubnet() {}
}
