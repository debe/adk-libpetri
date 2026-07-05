package org.libpetri.adk.subnet;

import com.google.adk.agents.RunConfig;
import com.google.adk.models.BaseLlm;
import com.google.adk.tools.BaseTool;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
import org.libpetri.adk.Experimental;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.TransitionAction;
import org.libpetri.runtime.PetriNetExecutor;

/**
 * SSE counterpart of {@link LlmAgentSubnet}: the same stock LLM↔tool
 * feedback loop, but composed with {@link LlmStreamingStepSubnet} so LLM
 * chunks can surface as partial ADK {@link com.google.adk.events.Event}s.
 *
 * <p>Callers must declare {@link LlmStreamingStepSubnet.Places#CHUNK} as an
 * environment place and pass the same executor reference to both
 * {@link Config#executorRef()} and {@link PetriRunner.Builder#deferredExecutorRef(AtomicReference)}.
 * Run this subnet under {@link RunConfig.StreamingMode#SSE}; normal mode keeps
 * the legacy one-event completion policy and will consume only the first egress
 * event for a turn.
 */
@Experimental
public final class StreamingLlmAgentSubnet {

    public static final String NAME = "StreamingLlmAgent";

    /**
     * Configuration for one streaming LLM-agent instance — bound at action-binding time.
     *
     * <p>{@code dispatchExecutor} is required: it is the executor passed
     * through to {@link ToolDispatchSubnet#actionBindings} for running the
     * agent's {@link BaseTool}s concurrently. Callers own its lifecycle.
     * {@code executorRef} is required for {@link LlmStreamingStepSubnet}'s
     * per-chunk env-place injection and must also be registered through
     * {@link PetriRunner.Builder#deferredExecutorRef(AtomicReference)}.
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
            int chunkBudget,
            AtomicReference<PetriNetExecutor> executorRef) {

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
            if (chunkBudget < 1) {
                throw new IllegalArgumentException("chunkBudget must be >= 1, got: " + chunkBudget);
            }
            Objects.requireNonNull(executorRef, "executorRef");
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
            private int chunkBudget = 4;
            private AtomicReference<PetriNetExecutor> executorRef;

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
            public Builder chunkBudget(int n)                      { this.chunkBudget = n; return this; }
            public Builder executorRef(AtomicReference<PetriNetExecutor> ref) { this.executorRef = ref; return this; }

            public Config build() {
                return new Config(name, model, Optional.ofNullable(systemInstruction),
                        tools, reaskBudget, fallbackContent, invocationIdSupplier,
                        dispatchExecutor, chunkBudget,
                        Objects.requireNonNull(executorRef, "executorRef must be set before build"));
            }
        }
    }

    public static final SubnetDef<Void> DEF = LlmAgentSubnet.buildComposedDef(NAME, LlmStreamingStepSubnet.DEF);

    /**
     * Full binding map — merges the contributing subnets' action bindings
     * with the agent's own. The merged map is validated against
     * {@link #DEF} by {@link SubnetActions#bind}.
     */
    public static Map<String, TransitionAction> actionBindings(BaseLlm baseLlm, Config config) {
        Objects.requireNonNull(baseLlm, "baseLlm");
        Objects.requireNonNull(config, "config");

        var agentConfig = agentConfig(config);
        var all = new LinkedHashMap<String, TransitionAction>();
        all.putAll(LlmStreamingStepSubnet.actionBindings(baseLlm, streamingCfg(config)));
        all.putAll(RouterSubnet.actionBindings(LlmAgentSubnet.routerConfig(agentConfig)));
        all.putAll(ToolDispatchSubnet.actionBindings(
                config.tools(), () -> null, config.dispatchExecutor()));
        all.put(LlmAgentSubnet.Transitions.BUILD_PROMPT,
                LlmAgentSubnet.buildPromptAction(agentConfig));
        all.put(LlmAgentSubnet.Transitions.RE_ASK,
                LlmAgentSubnet.reAskAction(agentConfig));
        all.put(LlmAgentSubnet.Transitions.RE_ASK_EXHAUSTED_FALLBACK,
                LlmAgentSubnet.reAskExhaustedFallbackAction(agentConfig));

        return SubnetActions.bind(DEF, all);
    }

    private static LlmStreamingStepSubnet.Config streamingCfg(Config config) {
        return new LlmStreamingStepSubnet.Config(
                config.name(),
                config.invocationIdSupplier(),
                config.chunkBudget(),
                config.executorRef());
    }

    private static LlmAgentSubnet.Config agentConfig(Config config) {
        return new LlmAgentSubnet.Config(
                config.name(),
                config.model(),
                config.systemInstruction(),
                config.tools(),
                config.reaskBudget(),
                config.fallbackContent(),
                config.invocationIdSupplier(),
                config.dispatchExecutor());
    }

    private StreamingLlmAgentSubnet() {}
}
