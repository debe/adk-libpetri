package org.libpetri.adk.subnet;

import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.tools.BaseTool;
import com.google.adk.tools.ToolContext;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import java.util.ArrayList;
import java.util.LinkedHashSet;
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
 * {@link RouterSubnet}'s route transition and {@link ToolDispatchSubnet} into
 * a complete LLM↔tool feedback loop with a structural reask budget, run one
 * turn at a time under a turn permit.
 *
 * <h2>Boundary (interface ports)</h2>
 * <ul>
 *   <li>{@code userIn}      — input,  {@link AdkColours#USER_IN}</li>
 *   <li>{@code turnAbort}   — input,  {@link AdkColours#TURN_ABORT}: abandon
 *       the turn in flight (signalled by {@code PetriAgent} on a failure)</li>
 *   <li>{@code eventOut}    — output, {@link AdkColours#EVENT_OUT}</li>
 *   <li>{@code transfer}    — output, {@link AdkColours#TRANSFER}
 *       (parent net demuxes via {@code Out.xor} over per-agent places)</li>
 * </ul>
 * <p>The net also holds {@link AdkColours#TURN_PERMIT}, which must carry one
 * token when the net starts. {@code PetriRunner} seeds it; a bare libpetri
 * executor needs it in its initial marking. {@code DEF.instantiate(prefix)}
 * renames it to {@code prefix/turnPermit}, which {@code PetriRunner} does not
 * seed: name it in {@code initialMarking}, or the start fails. The instance's
 * {@code turnAbort} port must be bound to {@link AdkColours#TURN_ABORT} for
 * {@code PetriAgent}'s abort to reach it.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [USER_IN] + [TURN_PERMIT] --T_StartTurn--> Out.and([TURN_ACTIVE], [TURN_INPUT])
 *   [TURN_INPUT] --T_BuildPrompt--> Out.and([LLM_REQUEST], [REASK_BUDGET]*N, [CONVERSATION])
 *
 *   [LLM_REQUEST]  --LlmStepSubnet--> [LLM_RESPONSE]
 *   [LLM_RESPONSE] --Router_Route---> Out.xor([TOOL_CALLS], [HANDOFF], [ANSWER])
 *   [TOOL_CALLS]   --ToolDispatchSubnet--> [TOOL_RESULTS]
 *
 *   [TOOL_RESULTS] + [REASK_BUDGET] + [CONVERSATION]
 *                  --T_ReAsk (prio 10)--> Out.and([LLM_REQUEST], [CONVERSATION])
 *   [TOOL_RESULTS] + inhibitor(REASK_BUDGET)
 *                  --T_ReAskExhaustedFallback (prio -10)--> [ANSWER] (canned)
 *
 *   [ANSWER]  + [TURN_ACTIVE] + [CONVERSATION], reset(REASK_BUDGET)
 *                  --T_EmitAnswer--> Out.and([EVENT_OUT], [TURN_PERMIT])
 *   [HANDOFF] + [TURN_ACTIVE] + [CONVERSATION], reset(REASK_BUDGET)
 *                  --T_EmitTransfer--> Out.and([TRANSFER], [TURN_PERMIT])
 *
 *   [TURN_ABORT] + [TURN_ACTIVE], reset(every place the turn holds)
 *                  --T_AbortTurn (prio 30)--> [TURN_PERMIT]
 *   [TURN_ABORT] + read(TURN_PERMIT) --T_DropAbort (prio 30)--> (nothing)
 * </pre>
 *
 * <h2>One turn at a time</h2>
 * <p>{@code StartTurn} consumes the session's single {@link AdkColours#TURN_PERMIT}
 * and only a turn's end returns it, so a second {@code USER_IN} that arrives
 * while a turn is still in its tool loop waits on {@code USER_IN} until that
 * turn has ended. Turns used to be told apart by position alone: an
 * overlapping input reset the conversation and the budget of the turn still
 * in flight, which then answered with the newcomer's turns, or left both
 * conversations on the place. Every way a turn ends goes through one of
 * three transitions, and each returns the permit: {@code EmitAnswer} (the
 * router's answer, a model-error recovery, a {@code BeforeModel}
 * short-circuit, or the reask-exhausted fallback), {@code EmitTransfer}, and
 * {@code AbortTurn}. The permit-moving transitions only move tokens, so
 * none of them can fail and lose it; {@code BuildPrompt}, which can, runs
 * after {@code StartTurn} has handed the turn its {@code TURN_ACTIVE} token.
 *
 * <p>A failed transition consumes its inputs and produces nothing, which
 * leaves the turn holding the permit with nothing left to end it.
 * {@link AdkColours#TURN_ABORT} is the way out: {@code AbortTurn} clears the
 * places the turn holds and returns the permit, and {@code DropAbort} drops
 * an abort that arrives while no turn is in flight. {@code DropAbort} ranks
 * above {@code StartTurn}, so an abort and an input that land in one pass
 * with the permit at rest drop the abort before the input's turn starts. An
 * abort clears what is at rest; an action of the turn still running when it
 * lands deposits into the next turn. This agent runs one action at a time,
 * so a failure of one of its own transitions leaves nothing running. The
 * streaming agent does not: its stream stays in flight while it emits
 * chunks, so a failed chunk emission falls under the same late-output
 * limit. See ADR 0005.
 *
 * <h2>Reask-budget invariant</h2>
 * <p>{@code BuildPrompt} seeds {@code N} unit tokens on {@link #REASK_BUDGET}
 * (the configured budget). Each LLM↔tool re-ask consumes one. When the place
 * is empty, the inhibitor-guarded fallback transition answers with the
 * configured {@code fallbackContent} — terminating the loop. Whichever
 * transition ends the turn resets the place, so no allowance outlives its
 * turn, and the permit keeps a second turn from seeding while one is
 * running. The decision lives in the marking and priority, never in
 * action-level branching.
 *
 * <h2>Composition pattern</h2>
 * <p>{@code SubnetDef.Builder} doesn't expose {@code .compose()}; the
 * body net is constructed via {@link PetriNet.Builder#compose} for the
 * step and dispatch subnets plus {@link PetriNet.Builder#transition} for
 * the router's transition (aimed at {@link #ANSWER} and {@link #HANDOFF})
 * and the agent's own transitions, then wrapped via
 * {@link SubnetDef#fromNet(PetriNet, Interface)}.
 *
 * <h2>This is a convenience template, not the framework</h2>
 * <p>{@code LlmAgentSubnet} is one possible LLM-agent shape — it
 * bundles the most common pattern (prompt-build + LLM call + router +
 * tool dispatch + reask-budget feedback loop) into a ready-to-use
 * {@link SubnetDef}. Users are free (and expected) to compose their own
 * subnets directly via {@link PetriNet.Builder#compose} +
 * {@link SubnetDef#fromNet(PetriNet, Interface)}, mixing the stock
 * building blocks with their own transitions to express any
 * agent topology they want — different callback shapes, custom tool
 * dependency graphs, parallel LLM branches, etc. The framework is the
 * composition primitives; the stock subnets are just examples that
 * happen to work for common cases.
 */
public final class LlmAgentSubnet {

    public static final String NAME = "LlmAgent";

    public static final class Transitions {
        public static final String START_TURN                = NAME + "_StartTurn";
        public static final String BUILD_PROMPT              = NAME + "_BuildPrompt";
        public static final String RE_ASK                    = NAME + "_ReAsk";
        public static final String RE_ASK_EXHAUSTED_FALLBACK = NAME + "_ReAskExhaustedFallback";
        public static final String EMIT_ANSWER               = NAME + "_EmitAnswer";
        public static final String EMIT_TRANSFER             = NAME + "_EmitTransfer";
        public static final String ABORT_TURN                = NAME + "_AbortTurn";
        public static final String DROP_ABORT                = NAME + "_DropAbort";
        private Transitions() {}
    }

    /** Internal place — reask-budget counter (cardinality = remaining attempts). */
    public static final Place<Void> REASK_BUDGET = Place.of(NAME + "_reaskBudget", Void.class);

    /**
     * The turns of the current invocation, oldest first: the user turn, then
     * each model function-call turn followed by its function-response turn.
     * {@code BuildPrompt} seeds it; {@code ReAsk} consumes it and writes it
     * back extended, so every continuation request carries the whole exchange
     * rather than the tool responses alone. The transition that ends the turn
     * consumes it.
     *
     * <p>Scope is one invocation. History across invocations is the caller's
     * own typed place, read by their prompt-building transition.
     */
    public static final Place<Conversation> CONVERSATION =
            Place.of(NAME + "_conversation", Conversation.class);

    /**
     * Internal place — marked while a turn is in flight: {@code StartTurn}
     * trades the {@link AdkColours#TURN_PERMIT} for it, and the transition
     * that ends the turn trades it back.
     */
    public static final Place<Void> TURN_ACTIVE = Place.of(NAME + "_turnActive", Void.class);

    /** Internal place — the admitted user input, waiting for {@code BuildPrompt}. */
    public static final Place<Content> TURN_INPUT = Place.of(NAME + "_turnInput", Content.class);

    /**
     * Internal place — the turn's final answer: the router's text answer or
     * the reask-exhausted fallback. {@code EmitAnswer} puts it on
     * {@link AdkColours#EVENT_OUT} and returns the permit.
     */
    public static final Place<Event> ANSWER = Place.of(NAME + "_answer", Event.class);

    /**
     * Internal place — the model's hand-off. {@code EmitTransfer} puts it on
     * {@link AdkColours#TRANSFER} and returns the permit.
     */
    public static final Place<AdkColours.TransferTarget> HANDOFF =
            Place.of(NAME + "_handoff", AdkColours.TransferTarget.class);

    /** Colour of {@link #CONVERSATION}: the invocation's turns, oldest first. */
    public record Conversation(List<Content> turns) {
        public Conversation {
            turns = List.copyOf(turns);
        }

        Conversation append(Content... more) {
            var next = new ArrayList<>(turns);
            next.addAll(List.of(more));
            return new Conversation(next);
        }
    }

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
            ExecutorService dispatchExecutor,
            LlmStepSubnet.Callbacks callbacks,
            Supplier<ToolContext> toolContextSupplier) {

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
            callbacks = callbacks == null ? LlmStepSubnet.Callbacks.none() : callbacks;
            toolContextSupplier = toolContextSupplier == null ? () -> null : toolContextSupplier;
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
            private LlmStepSubnet.Callbacks callbacks = LlmStepSubnet.Callbacks.none();
            private Supplier<ToolContext> toolContextSupplier = () -> null;

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

            /**
             * Model-call callbacks, forwarded to the composed
             * {@link LlmStepSubnet}. Without this the composite could not reach
             * them at all, so an LLM error inside a composed agent was
             * unrecoverable even though the step subnet supports handling it.
             */
            public Builder callbacks(LlmStepSubnet.Callbacks c)    { this.callbacks = c; return this; }

            /**
             * Supplies the {@link ToolContext} handed to each tool. Defaults to
             * {@code () -> null}, which was previously hard-coded with no way to
             * override it, so tools needing state, artifacts or auth could not
             * be used through this composite.
             */
            public Builder toolContextSupplier(Supplier<ToolContext> s) { this.toolContextSupplier = s; return this; }

            public Config build() {
                return new Config(name, model, Optional.ofNullable(systemInstruction),
                        tools, reaskBudget, fallbackContent, invocationIdSupplier,
                        dispatchExecutor, callbacks, toolContextSupplier);
            }
        }
    }

    /** Stateless subnet definition (composed body + N-port interface). */
    public static final SubnetDef<Void> DEF = buildComposedDef(NAME, LlmStepSubnet.DEF);

    static SubnetDef<Void> buildComposedDef(String netName, SubnetDef<Void> stepDef) {
        var body = PetriNet.builder(netName)
                .place(AdkColours.TURN_PERMIT)
                .place(TURN_ACTIVE)
                .place(TURN_INPUT)
                .place(REASK_BUDGET)
                .place(CONVERSATION)
                .place(ANSWER)
                .place(HANDOFF)
                .transition(Transition.builder(Transitions.START_TURN)
                        .inputs(Arc.In.one(AdkColours.USER_IN), Arc.In.one(AdkColours.TURN_PERMIT))
                        .outputs(Arc.Out.and(TURN_ACTIVE, TURN_INPUT))
                        .build())
                .transition(Transition.builder(Transitions.BUILD_PROMPT)
                        .inputs(Arc.In.one(TURN_INPUT))
                        .outputs(Arc.Out.and(AdkColours.LLM_REQUEST, REASK_BUDGET, CONVERSATION))
                        .build())
                .transition(Transition.builder(Transitions.RE_ASK)
                        .inputs(Arc.In.one(AdkColours.TOOL_RESULTS), Arc.In.one(REASK_BUDGET),
                                Arc.In.one(CONVERSATION))
                        .outputs(Arc.Out.and(AdkColours.LLM_REQUEST, CONVERSATION))
                        .priority(10)
                        .build())
                .transition(Transition.builder(Transitions.RE_ASK_EXHAUSTED_FALLBACK)
                        .inputs(Arc.In.one(AdkColours.TOOL_RESULTS))
                        .inhibitor(REASK_BUDGET)
                        .outputs(Arc.Out.place(ANSWER))
                        .priority(-10)
                        .build())
                .transition(RouterSubnet.routeTransition(HANDOFF, ANSWER))
                .transition(Transition.builder(Transitions.EMIT_ANSWER)
                        .inputs(Arc.In.one(ANSWER), Arc.In.one(TURN_ACTIVE), Arc.In.one(CONVERSATION))
                        .reset(REASK_BUDGET)
                        .outputs(Arc.Out.and(AdkColours.EVENT_OUT, AdkColours.TURN_PERMIT))
                        .build())
                .transition(Transition.builder(Transitions.EMIT_TRANSFER)
                        .inputs(Arc.In.one(HANDOFF), Arc.In.one(TURN_ACTIVE), Arc.In.one(CONVERSATION))
                        .reset(REASK_BUDGET)
                        .outputs(Arc.Out.and(AdkColours.TRANSFER, AdkColours.TURN_PERMIT))
                        .build())
                .transition(abortTurn(stepDef))
                // Above StartTurn: an abort and an input that land in one
                // pass with the permit at rest drop the abort first. At equal
                // priority StartTurn would take the permit and AbortTurn would
                // then wipe the fresh turn. The read leaves StartTurn enabled.
                .transition(Transition.builder(Transitions.DROP_ABORT)
                        .inputs(Arc.In.one(AdkColours.TURN_ABORT))
                        .read(AdkColours.TURN_PERMIT)
                        .priority(30)
                        .build())
                .compose(stepDef)
                .compose(ToolDispatchSubnet.DEF)
                .build();
        var iface = Interface.builder()
                .inputPort("userIn", AdkColours.USER_IN)
                .inputPort("turnAbort", AdkColours.TURN_ABORT)
                .outputPort("eventOut", AdkColours.EVENT_OUT)
                .outputPort("transfer", AdkColours.TRANSFER)
                .build();
        return SubnetDef.fromNet(body, iface);
    }

    /**
     * {@code AbortTurn}: takes the turn's {@link #TURN_ACTIVE} token back for
     * the permit and resets every place a turn holds, the step subnet's own
     * places included (all of {@code stepDef}'s places but its egress).
     */
    private static Transition abortTurn(SubnetDef<Void> stepDef) {
        var held = new LinkedHashSet<Place<?>>(List.of(
                TURN_INPUT, REASK_BUDGET, CONVERSATION, ANSWER, HANDOFF,
                AdkColours.LLM_REQUEST, AdkColours.LLM_RESPONSE,
                AdkColours.TOOL_CALLS, AdkColours.TOOL_RESULTS));
        for (var place : stepDef.body().places()) {
            if (!place.equals(AdkColours.EVENT_OUT)) held.add(place);
        }
        // Above every step of the turn, the streaming step's chunk emission
        // (20) included: a chunk admitted in the same pass as the abort is
        // reset with the turn instead of being emitted as a partial of it.
        var builder = Transition.builder(Transitions.ABORT_TURN)
                .inputs(Arc.In.one(AdkColours.TURN_ABORT), Arc.In.one(TURN_ACTIVE))
                .outputs(Arc.Out.place(AdkColours.TURN_PERMIT))
                .priority(30);
        for (var place : held) builder.reset(place);
        return builder.build();
    }

    /**
     * Full binding map — merges the contributing subnets' action bindings
     * with the agent's own. The merged map is validated against
     * {@link #DEF} by {@link SubnetActions#bind}.
     */
    public static Map<String, TransitionAction> actionBindings(BaseLlm baseLlm, Config config) {
        Objects.requireNonNull(baseLlm, "baseLlm");
        Objects.requireNonNull(config, "config");

        return SubnetActions.bind(DEF, SubnetActions.merge(
                LlmStepSubnet.actionBindings(baseLlm, config.callbacks()),
                ToolDispatchSubnet.actionBindings(
                        config.tools(), config.toolContextSupplier(), config.dispatchExecutor()),
                ownActions(config)));
    }

    /**
     * The agent's own transitions, router included, shared with
     * {@link StreamingLlmAgentSubnet}.
     */
    static Map<String, TransitionAction> ownActions(Config config) {
        return Map.of(
                Transitions.START_TURN,                startTurnAction(),
                Transitions.BUILD_PROMPT,              buildPromptAction(config),
                Transitions.RE_ASK,                    reAskAction(config),
                Transitions.RE_ASK_EXHAUSTED_FALLBACK, reAskExhaustedFallbackAction(config),
                RouterSubnet.Transitions.ROUTE,
                        RouterSubnet.routeAction(routerConfig(config), HANDOFF, ANSWER),
                Transitions.EMIT_ANSWER,               emitAction(ANSWER, AdkColours.EVENT_OUT),
                Transitions.EMIT_TRANSFER,             emitAction(HANDOFF, AdkColours.TRANSFER),
                Transitions.ABORT_TURN,                abortTurnAction(),
                Transitions.DROP_ABORT,                ctx -> {
                    ctx.input(AdkColours.TURN_ABORT);   // no turn in flight: nothing to abort
                    return CompletableFuture.completedFuture(null);
                });
    }

    static RouterSubnet.Config routerConfig(Config c) {
        return new RouterSubnet.Config(c.name(), c.invocationIdSupplier());
    }

    // ======================== Agent-owned actions ========================

    // The permit-moving actions (StartTurn, the emits, AbortTurn) only move
    // tokens. A failure there would lose the permit with no turn left to
    // abort, so nothing that can throw belongs in them.

    static TransitionAction startTurnAction() {
        return ctx -> {
            ctx.input(AdkColours.TURN_PERMIT);
            ctx.output(TURN_INPUT, ctx.input(AdkColours.USER_IN));
            ctx.output(TURN_ACTIVE, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    static TransitionAction buildPromptAction(Config config) {
        return ctx -> {
            Content userContent = ctx.input(TURN_INPUT);
            var request = LlmRequests.build(
                    config.model(),
                    config.systemInstruction(),
                    config.tools(),
                    List.of(userContent));
            ctx.output(AdkColours.LLM_REQUEST, request);
            ctx.output(CONVERSATION, new Conversation(List.of(userContent)));

            // Seed N reask-budget unit tokens. The place is empty: the
            // transition that ended the previous turn reset it.
            for (int i = 0; i < config.reaskBudget(); i++) {
                ctx.output(REASK_BUDGET, (Void) null);
            }
            return CompletableFuture.completedFuture(null);
        };
    }

    static TransitionAction reAskAction(Config config) {
        return ctx -> {
            // Consume the budget token (validated by transition input arc).
            ctx.input(REASK_BUDGET);
            AdkColours.ToolResults results = ctx.input(AdkColours.TOOL_RESULTS);
            Conversation conversation = ctx.input(CONVERSATION);

            // The continuation carries the whole invocation so far: the model's
            // function-call turn goes back verbatim (thought signatures included)
            // ahead of the responses, which Gemini pairs call-by-call. Function
            // responses travel in a "user"-role turn, as ADK's own flow sends them.
            var responseParts = new ArrayList<Part>();
            for (var fr : results.results()) {
                responseParts.add(Part.builder().functionResponse(fr).build());
            }
            Content responseTurn = Content.builder().role("user").parts(responseParts).build();
            Conversation next = conversation.append(results.modelTurn(), responseTurn);
            ctx.output(AdkColours.LLM_REQUEST, LlmRequests.build(
                    config.model(),
                    config.systemInstruction(),
                    config.tools(),
                    next.turns()));
            ctx.output(CONVERSATION, next);
            return CompletableFuture.completedFuture(null);
        };
    }

    static TransitionAction reAskExhaustedFallbackAction(Config config) {
        return ctx -> {
            ctx.input(AdkColours.TOOL_RESULTS);  // drained — no further action
            Event event = Event.builder()
                    .invocationId(config.invocationIdSupplier().get())
                    .author(config.name())
                    .content(config.fallbackContent())
                    .build();
            ctx.output(ANSWER, event);
            return CompletableFuture.completedFuture(null);
        };
    }

    /** {@code EmitAnswer} / {@code EmitTransfer}: forward the outcome, end the turn. */
    private static <T> TransitionAction emitAction(Place<T> outcome, Place<T> boundary) {
        return ctx -> {
            ctx.input(TURN_ACTIVE);
            ctx.input(CONVERSATION);
            ctx.output(boundary, ctx.input(outcome));
            ctx.output(AdkColours.TURN_PERMIT, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction abortTurnAction() {
        return ctx -> {
            ctx.input(AdkColours.TURN_ABORT);
            ctx.input(TURN_ACTIVE);
            ctx.output(AdkColours.TURN_PERMIT, (Void) null);
            return CompletableFuture.completedFuture(null);
        };
    }

    private LlmAgentSubnet() {}
}
