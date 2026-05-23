package org.libpetri.adk.subnet;

import com.google.adk.models.BaseLlm;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import org.libpetri.core.Arc;
import org.libpetri.core.Place;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;

/**
 * Stock subnet for a single LLM call with Before / After / Error
 * callback transitions.
 *
 * <p>Interface (input/output ports auto-bound by name+type inference):
 * <ul>
 *   <li>{@code llmRequest} — input port, {@link AdkColours#LLM_REQUEST}</li>
 *   <li>{@code llmResponse} — output port, {@link AdkColours#LLM_RESPONSE}</li>
 * </ul>
 *
 * <p>Internal topology (places live on the {@link LlmStepSubnet.Places}
 * holder, transition names live on {@link LlmStepSubnet.Transitions}):
 *
 * <pre>
 *   [LLM_REQUEST] --T_BeforeModel--> [READY_TO_CALL]    (continue)
 *                          \-------> [LLM_RESPONSE]     (short-circuit override)
 *
 *   [READY_TO_CALL] --T_LlmCall--> [RAW_RESPONSE]       (success)
 *                          \-----> [LLM_ERROR]          (BaseLlm threw)
 *
 *   [RAW_RESPONSE] --T_AfterModel--> [LLM_RESPONSE]     (forward / mutate)
 *
 *   [LLM_ERROR] --T_OnModelError--> [LLM_RESPONSE]      (recovery)
 * </pre>
 *
 * <p>All four transitions have configurable callback actions. The
 * defaults are stateless pass-throughs that:
 * <ul>
 *   <li>{@code BeforeModel}: forwards the request to {@code READY_TO_CALL}
 *       unchanged.</li>
 *   <li>{@code LlmCall}: calls {@link BaseLlm#generateContent} (non-streaming)
 *       and routes the single yielded {@link LlmResponse} to
 *       {@code RAW_RESPONSE}, or to {@code LLM_ERROR} on failure.</li>
 *   <li>{@code AfterModel}: forwards the response to {@code LLM_RESPONSE}
 *       unchanged.</li>
 *   <li>{@code OnModelError}: rethrows by failing the transition — install
 *       a callback to recover with a fallback response.</li>
 * </ul>
 *
 * <p>The decision between continue/short-circuit (BeforeModel) and
 * success/error (LlmCall) lives in the {@code Out.xor} structure — the
 * action picks the target place; the executor's XOR validator enforces
 * exactly-one production per fire.
 *
 * <p>Users compose by {@code PetriNet.builder("…").compose(LlmStepSubnet.DEF)}
 * and bind actions via {@code petriNet.bindActions(LlmStepSubnet.actionBindings(baseLlm, callbacks))}.
 */
public final class LlmStepSubnet {

    public static final String NAME = "LlmStep";

    /** Stable transition names — used as keys in {@link #actionBindings}. */
    public static final class Transitions {
        public static final String BEFORE_MODEL   = NAME + "_BeforeModel";
        public static final String LLM_CALL       = NAME + "_LlmCall";
        public static final String AFTER_MODEL    = NAME + "_AfterModel";
        public static final String ON_MODEL_ERROR = NAME + "_OnModelError";
        private Transitions() {}
    }

    /** Internal places, prefixed by subnet name to avoid collisions on direct compose. */
    public static final class Places {
        public static final Place<LlmRequest>  READY_TO_CALL =
                Place.of(NAME + "_readyToCall", LlmRequest.class);
        public static final Place<LlmResponse> RAW_RESPONSE  =
                Place.of(NAME + "_rawResponse", LlmResponse.class);
        public static final Place<LlmError>    LLM_ERROR     =
                Place.of(NAME + "_llmError", LlmError.class);
        private Places() {}
    }

    /** Typed error colour for the LLM-error branch. */
    public record LlmError(String message, String exceptionType) {}

    /**
     * Optional user callbacks. Use {@link #none()} for the
     * pass-through-only configuration.
     */
    public record Callbacks(
            Optional<BeforeModelCallback> beforeModel,
            Optional<AfterModelCallback>  afterModel,
            Optional<OnModelErrorCallback> onModelError) {

        public Callbacks {
            beforeModel  = beforeModel  == null ? Optional.empty() : beforeModel;
            afterModel   = afterModel   == null ? Optional.empty() : afterModel;
            onModelError = onModelError == null ? Optional.empty() : onModelError;
        }

        public static Callbacks none() {
            return new Callbacks(Optional.empty(), Optional.empty(), Optional.empty());
        }

        public static Builder builder() { return new Builder(); }

        public static final class Builder {
            private BeforeModelCallback before;
            private AfterModelCallback  after;
            private OnModelErrorCallback onError;
            public Builder beforeModel(BeforeModelCallback cb)   { this.before  = cb; return this; }
            public Builder afterModel(AfterModelCallback cb)     { this.after   = cb; return this; }
            public Builder onModelError(OnModelErrorCallback cb) { this.onError = cb; return this; }
            public Callbacks build() {
                return new Callbacks(
                        Optional.ofNullable(before),
                        Optional.ofNullable(after),
                        Optional.ofNullable(onError));
            }
        }
    }

    /**
     * Returns {@code Optional.of(response)} to short-circuit the call
     * (skip LlmCall + AfterModel and emit this response directly), or
     * {@code Optional.empty()} to let the call proceed.
     */
    @FunctionalInterface
    public interface BeforeModelCallback {
        Optional<LlmResponse> apply(LlmRequest request);
    }

    /**
     * Receives the raw response and returns the (possibly mutated or
     * replaced) response that lands on the output port.
     */
    @FunctionalInterface
    public interface AfterModelCallback {
        LlmResponse apply(LlmResponse rawResponse);
    }

    /**
     * Receives the error and returns a recovery {@link LlmResponse}.
     * Throw from the callback to propagate the failure (the transition
     * will fire {@code TransitionFailed}).
     */
    @FunctionalInterface
    public interface OnModelErrorCallback {
        LlmResponse apply(LlmError error);
    }

    /** Stateless subnet definition — bind actions per session via {@link #actionBindings}. */
    public static final SubnetDef<Void> DEF = SubnetDef.builder(NAME)
            .place(AdkColours.LLM_REQUEST)
            .place(AdkColours.LLM_RESPONSE)
            .place(Places.READY_TO_CALL)
            .place(Places.RAW_RESPONSE)
            .place(Places.LLM_ERROR)
            .transition(Transition.builder(Transitions.BEFORE_MODEL)
                    .inputs(Arc.In.one(AdkColours.LLM_REQUEST))
                    .outputs(Arc.Out.xor(Places.READY_TO_CALL, AdkColours.LLM_RESPONSE))
                    .build())
            .transition(Transition.builder(Transitions.LLM_CALL)
                    .inputs(Arc.In.one(Places.READY_TO_CALL))
                    .outputs(Arc.Out.xor(Places.RAW_RESPONSE, Places.LLM_ERROR))
                    .build())
            .transition(Transition.builder(Transitions.AFTER_MODEL)
                    .inputs(Arc.In.one(Places.RAW_RESPONSE))
                    .outputs(Arc.Out.place(AdkColours.LLM_RESPONSE))
                    .build())
            .transition(Transition.builder(Transitions.ON_MODEL_ERROR)
                    .inputs(Arc.In.one(Places.LLM_ERROR))
                    .outputs(Arc.Out.place(AdkColours.LLM_RESPONSE))
                    .build())
            .inputPort("llmRequest",  AdkColours.LLM_REQUEST)
            .outputPort("llmResponse", AdkColours.LLM_RESPONSE)
            .build();

    /** Convenience: pass-through callbacks, just the BaseLlm wired. */
    public static Map<String, TransitionAction> actionBindings(BaseLlm baseLlm) {
        return actionBindings(baseLlm, Callbacks.none());
    }

    /**
     * Full binding map for this subnet — keyed by the {@link Transitions}
     * constants, ready to merge into {@link org.libpetri.core.PetriNet#bindActions(Map)}.
     * Validated via {@link SubnetActions#bind}.
     */
    public static Map<String, TransitionAction> actionBindings(BaseLlm baseLlm, Callbacks callbacks) {
        Objects.requireNonNull(baseLlm, "baseLlm");
        Objects.requireNonNull(callbacks, "callbacks");
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.BEFORE_MODEL,   beforeModelAction(callbacks.beforeModel()));
        session.put(Transitions.LLM_CALL,       llmCallAction(baseLlm));
        session.put(Transitions.AFTER_MODEL,    afterModelAction(callbacks.afterModel()));
        session.put(Transitions.ON_MODEL_ERROR, onModelErrorAction(callbacks.onModelError()));
        return SubnetActions.bind(DEF, session);
    }

    // ======================== Action implementations ========================

    private static TransitionAction beforeModelAction(Optional<BeforeModelCallback> cb) {
        return ctx -> {
            var request = ctx.input(AdkColours.LLM_REQUEST);
            var shortCircuit = cb.flatMap(c -> c.apply(request));
            if (shortCircuit.isPresent()) {
                ctx.output(AdkColours.LLM_RESPONSE, shortCircuit.get());
            } else {
                ctx.output(Places.READY_TO_CALL, request);
            }
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction llmCallAction(BaseLlm baseLlm) {
        return ctx -> {
            var request = ctx.input(Places.READY_TO_CALL);
            CompletableFuture<Void> done = new CompletableFuture<>();
            baseLlm.generateContent(request, /* stream */ false)
                    .firstOrError()
                    .subscribe(
                            response -> {
                                ctx.output(Places.RAW_RESPONSE, response);
                                done.complete(null);
                            },
                            err -> {
                                ctx.output(Places.LLM_ERROR,
                                        new LlmError(
                                                err.getMessage(),
                                                err.getClass().getName()));
                                done.complete(null);
                            });
            return done;
        };
    }

    private static TransitionAction afterModelAction(Optional<AfterModelCallback> cb) {
        return ctx -> {
            var raw = ctx.input(Places.RAW_RESPONSE);
            var out = cb.map(c -> c.apply(raw)).orElse(raw);
            ctx.output(AdkColours.LLM_RESPONSE, out);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction onModelErrorAction(Optional<OnModelErrorCallback> cb) {
        return ctx -> {
            var error = ctx.input(Places.LLM_ERROR);
            if (cb.isEmpty()) {
                CompletionStage<Void> failed = CompletableFuture.failedFuture(
                        new IllegalStateException(
                                "LlmStep_OnModelError fired with no recovery callback bound: "
                                        + error.message() + " (" + error.exceptionType() + ")"));
                return failed;
            }
            var recovery = cb.get().apply(error);
            ctx.output(AdkColours.LLM_RESPONSE, recovery);
            return CompletableFuture.completedFuture(null);
        };
    }

    private LlmStepSubnet() {}
}
