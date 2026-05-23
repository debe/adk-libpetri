package org.libpetri.adk.subnet;

import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.tools.BaseTool;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.function.Supplier;
import org.libpetri.core.Arc;
import org.libpetri.core.Interface;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;

/**
 * Stock LLM-agent subnet — composes {@link LlmStepSubnet},
 * {@link RouterSubnet}, {@link ToolDispatchSubnet} into a complete
 * LLM↔tool feedback loop with a structural reask budget.
 *
 * <h2>Boundary (interface ports)</h2>
 * <ul>
 *   <li>{@code userIn}      — input,  {@link AdkColours#USER_IN}</li>
 *   <li>{@code eventOut}    — output, {@link AdkColours#EVENT_OUT}</li>
 *   <li>{@code legacySessionWrite} — output, {@link AdkColours#LEGACY_SESSION_WRITE}
 *       (currently unused by the subnet's own transitions; exposed so a
 *       parent net composing {@code PersistStateSubnet} can drain
 *       ADK-Session-bound legacy writes that a user-replaced transition
 *       might emit. Most agents won't need this — keep in-net state in
 *       typed Places via the in-net conversation-place pattern.)</li>
 *   <li>{@code transfer}    — output, {@link AdkColours#TRANSFER}
 *       (parent net demuxes via {@code Out.xor} over per-agent places)</li>
 * </ul>
 *
 * <h2>Topology</h2>
 * <pre>
 *   [USER_IN] --T_BuildPrompt--> Out.and([LLM_REQUEST], [REASK_BUDGET]*N), reset(REASK_BUDGET)
 *
 *   [LLM_REQUEST]  --LlmStepSubnet--> [LLM_RESPONSE]
 *   [LLM_RESPONSE] --RouterSubnet---> Out.xor([TOOL_CALLS], [TRANSFER], [EVENT_OUT])
 *   [TOOL_CALLS]   --ToolDispatchSubnet--> [TOOL_RESULTS]
 *
 *   [TOOL_RESULTS] + [REASK_BUDGET]  --T_ReAsk (prio 10)--> [LLM_REQUEST]
 *   [TOOL_RESULTS] + inhibitor(REASK_BUDGET)
 *                  --T_ReAskExhaustedFallback (prio -10)--> [EVENT_OUT] (canned)
 * </pre>
 *
 * <h2>Reask-budget invariant</h2>
 * <p>Each new user input <b>resets</b> {@link #REASK_BUDGET} (wiping any
 * stale tokens from a prior turn) and seeds {@code N} unit tokens (the
 * configured budget). Each LLM↔tool re-ask consumes one. When the place
 * is empty, the inhibitor-guarded fallback transition fires the
 * configured {@code fallbackContent} as the final event — terminating
 * the loop. The decision lives in the marking and priority, never in
 * action-level branching.
 *
 * <h2>Composition pattern</h2>
 * <p>{@code SubnetDef.Builder} doesn't expose {@code .compose()}; the
 * body net is constructed via {@link PetriNet.Builder#compose} for the
 * three sub-subnets plus {@link PetriNet.Builder#transition} for the
 * agent's own transitions, then wrapped via
 * {@link SubnetDef#fromNet(PetriNet, Interface)}.
 *
 * <h2>This is a convenience template, not the framework</h2>
 * <p>{@code LlmAgentSubnet} is one possible LLM-agent shape — it
 * bundles the most common pattern (prompt-build + LLM call + router +
 * tool dispatch + reask-budget feedback loop) into a ready-to-use
 * {@link SubnetDef}. Users are free (and expected) to compose their own
 * subnets directly via {@link PetriNet.Builder#compose} +
 * {@link SubnetDef#fromNet(PetriNet, Interface)}, mixing the stock
 * P1–P3 building blocks with their own transitions to express any
 * agent topology they want — different callback shapes, custom tool
 * dependency graphs, parallel LLM branches, etc. The framework is the
 * composition primitives; the stock subnets are just examples that
 * happen to work for common cases.
 */
public final class LlmAgentSubnet {

    public static final String NAME = "LlmAgent";

    public static final class Transitions {
        public static final String BUILD_PROMPT              = NAME + "_BuildPrompt";
        public static final String RE_ASK                    = NAME + "_ReAsk";
        public static final String RE_ASK_EXHAUSTED_FALLBACK = NAME + "_ReAskExhaustedFallback";
        private Transitions() {}
    }

    /** Internal place — reask-budget counter (cardinality = remaining attempts). */
    public static final Place<Void> REASK_BUDGET = Place.of(NAME + "_reaskBudget", Void.class);

    /**
     * Configuration for one LLM-agent instance — bound at action-binding time.
     *
     * <p>{@code dispatchExecutor} is required: it is the executor passed
     * through to {@link ToolDispatchSubnet#actionBindings} for running the
     * agent's {@link BaseTool}s concurrently. Callers own its lifecycle.
     */
    public record Config(
            String name,
            String model,
            Optional<String> systemInstruction,
            Map<String, BaseTool> tools,
            int reaskBudget,
            Content fallbackContent,
            Supplier<String> invocationIdSupplier,
            ExecutorService dispatchExecutor) {

        public Config {
            Objects.requireNonNull(name, "name");
            Objects.requireNonNull(model, "model");
            systemInstruction = systemInstruction == null ? Optional.empty() : systemInstruction;
            tools = tools == null ? Map.of() : Map.copyOf(tools);
            if (reaskBudget < 1) {
                throw new IllegalArgumentException(
                        "reaskBudget must be >= 1, got: " + reaskBudget);
            }
            Objects.requireNonNull(fallbackContent, "fallbackContent");
            Objects.requireNonNull(invocationIdSupplier, "invocationIdSupplier");
            Objects.requireNonNull(dispatchExecutor, "dispatchExecutor");
        }

        public static Builder builder(String name, String model) {
            return new Builder(name, model);
        }

        public static final class Builder {
            private final String name;
            private final String model;
            private String systemInstruction;
            private Map<String, BaseTool> tools = Map.of();
            private int reaskBudget = 3;
            private Content fallbackContent = Content.fromParts(Part.fromText(
                    "I couldn't complete all the requested steps. Please rephrase your question."));
            private Supplier<String> invocationIdSupplier = () -> UUID.randomUUID().toString();
            private ExecutorService dispatchExecutor;

            private Builder(String name, String model) {
                this.name = name;
                this.model = model;
            }

            public Builder systemInstruction(String text)          { this.systemInstruction = text; return this; }
            public Builder tools(Map<String, BaseTool> tools)      { this.tools = tools; return this; }
            public Builder reaskBudget(int n)                      { this.reaskBudget = n; return this; }
            public Builder fallbackContent(Content c)              { this.fallbackContent = c; return this; }
            public Builder invocationIdSupplier(Supplier<String> s){ this.invocationIdSupplier = s; return this; }
            public Builder dispatchExecutor(ExecutorService e)     { this.dispatchExecutor = e; return this; }

            public Config build() {
                return new Config(name, model, Optional.ofNullable(systemInstruction),
                        tools, reaskBudget, fallbackContent, invocationIdSupplier,
                        dispatchExecutor);
            }
        }
    }

    /** Stateless subnet definition (composed body + N-port interface). */
    public static final SubnetDef<Void> DEF = buildDef();

    private static SubnetDef<Void> buildDef() {
        var body = PetriNet.builder(NAME)
                .place(REASK_BUDGET)

                // Agent-owned transitions (logic lives in action binding).
                .transition(Transition.builder(Transitions.BUILD_PROMPT)
                        .inputs(Arc.In.one(AdkColours.USER_IN))
                        .reset(REASK_BUDGET)
                        .outputs(Arc.Out.and(AdkColours.LLM_REQUEST, REASK_BUDGET))
                        .build())

                .transition(Transition.builder(Transitions.RE_ASK)
                        .inputs(
                                Arc.In.one(AdkColours.TOOL_RESULTS),
                                Arc.In.one(REASK_BUDGET))
                        .outputs(Arc.Out.place(AdkColours.LLM_REQUEST))
                        .priority(10)
                        .build())

                .transition(Transition.builder(Transitions.RE_ASK_EXHAUSTED_FALLBACK)
                        .inputs(Arc.In.one(AdkColours.TOOL_RESULTS))
                        .inhibitor(REASK_BUDGET)
                        .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                        .priority(-10)
                        .build())

                // Compose the three pipeline subnets (direct, by-name merge).
                .compose(LlmStepSubnet.DEF)
                .compose(RouterSubnet.DEF)
                .compose(ToolDispatchSubnet.DEF)

                // LEGACY_SESSION_WRITE boundary place declared so the interface port can
                // reference it even though no built-in transition writes there yet. A
                // user-replaced transition (or a future opt-in version of this subnet)
                // could emit envelopes here for a parent's PersistStateSubnet to drain.
                // For in-net state, use typed Places per concept instead.
                .place(AdkColours.LEGACY_SESSION_WRITE)
                .build();

        var iface = Interface.builder()
                .inputPort("userIn",            AdkColours.USER_IN)
                .outputPort("eventOut",         AdkColours.EVENT_OUT)
                .outputPort("legacySessionWrite", AdkColours.LEGACY_SESSION_WRITE)
                .outputPort("transfer",         AdkColours.TRANSFER)
                .build();

        return SubnetDef.fromNet(body, iface);
    }

    /**
     * Full binding map — merges the contributing subnets' action bindings
     * with the agent's own. The merged map is validated against
     * {@link #DEF} by {@link SubnetActions#bind}.
     */
    public static Map<String, TransitionAction> actionBindings(BaseLlm baseLlm, Config config) {
        Objects.requireNonNull(baseLlm, "baseLlm");
        Objects.requireNonNull(config, "config");

        var all = new LinkedHashMap<String, TransitionAction>();
        all.putAll(LlmStepSubnet.actionBindings(baseLlm));
        all.putAll(RouterSubnet.actionBindings(routerConfig(config)));
        all.putAll(ToolDispatchSubnet.actionBindings(
                config.tools(), () -> null, config.dispatchExecutor()));
        all.put(Transitions.BUILD_PROMPT,              buildPromptAction(config));
        all.put(Transitions.RE_ASK,                    reAskAction(config));
        all.put(Transitions.RE_ASK_EXHAUSTED_FALLBACK, reAskExhaustedFallbackAction(config));

        return SubnetActions.bind(DEF, all);
    }

    private static RouterSubnet.Config routerConfig(Config c) {
        return new RouterSubnet.Config(c.name(), c.invocationIdSupplier());
    }

    // ======================== Agent-owned actions ========================

    private static TransitionAction buildPromptAction(Config config) {
        return ctx -> {
            Content userContent = ctx.input(AdkColours.USER_IN);
            ctx.output(AdkColours.LLM_REQUEST, LlmRequests.build(
                    config.model(),
                    config.systemInstruction(),
                    config.tools(),
                    List.of(userContent)));

            // Seed N reask-budget unit tokens. The Reset arc on this transition
            // wiped any survivors from a previous turn before this action runs.
            for (int i = 0; i < config.reaskBudget(); i++) {
                ctx.output(REASK_BUDGET, (Void) null);
            }
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction reAskAction(Config config) {
        return ctx -> {
            // Consume the budget token (validated by transition input arc).
            ctx.input(REASK_BUDGET);
            AdkColours.ToolResults results = ctx.input(AdkColours.TOOL_RESULTS);

            // Build a continuation LlmRequest carrying the function responses as
            // a "tool"-role Content. For P4 this is a degenerate single-turn
            // continuation — P5 will thread the original-request + previous-LLM-response
            // history through a session-state Read arc.
            var responseParts = new ArrayList<Part>();
            for (var fr : results.results()) {
                responseParts.add(Part.builder().functionResponse(fr).build());
            }
            Content toolTurn = Content.builder().role("tool").parts(responseParts).build();
            ctx.output(AdkColours.LLM_REQUEST, LlmRequests.build(
                    config.model(),
                    config.systemInstruction(),
                    config.tools(),
                    List.of(toolTurn)));
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction reAskExhaustedFallbackAction(Config config) {
        return ctx -> {
            ctx.input(AdkColours.TOOL_RESULTS);  // drained — no further action
            Event event = Event.builder()
                    .invocationId(config.invocationIdSupplier().get())
                    .author(config.name())
                    .content(config.fallbackContent())
                    .build();
            ctx.output(AdkColours.EVENT_OUT, event);
            return CompletableFuture.completedFuture(null);
        };
    }

    private LlmAgentSubnet() {}
}
