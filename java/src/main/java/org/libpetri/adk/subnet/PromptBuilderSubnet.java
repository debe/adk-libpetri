package org.libpetri.adk.subnet;

import com.google.adk.tools.BaseTool;
import com.google.genai.types.Content;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import org.libpetri.core.Arc;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;

/**
 * Stock subnet that turns a user {@link Content} into a fully-formed
 * {@link LlmRequest} ready for {@link LlmStepSubnet}.
 *
 * <p>This stays <b>stateless and single-turn</b>: the prepared
 * request contains exactly the user's incoming {@code Content} plus an
 * optional {@code systemInstruction} carried via
 * {@link GenerateContentConfig#systemInstruction()}. Multi-turn history
 * (reading prior {@link com.google.adk.events.Event}s from session
 * state) belongs to a future Session/State integration — the topology is
 * the same, with an added Read arc to a history place.
 *
 * <p>Topology:
 * <pre>
 *   [USER_IN] --T_BuildPrompt--> [LLM_REQUEST]
 * </pre>
 *
 * <p>Configuration is captured at action-binding time via
 * {@link Config} — model name, optional system instruction, optional
 * tool registry. Bind via
 * {@code petriNet.bindActions(PromptBuilderSubnet.actionBindings(config))}.
 */
public final class PromptBuilderSubnet {

    public static final String NAME = "PromptBuilder";

    public static final class Transitions {
        public static final String BUILD_PROMPT = NAME + "_BuildPrompt";
        private Transitions() {}
    }

    public record Config(
            String model,
            Optional<String> systemInstruction,
            Map<String, BaseTool> tools) {

        public Config {
            Objects.requireNonNull(model, "model");
            systemInstruction = systemInstruction == null ? Optional.empty() : systemInstruction;
            tools = tools == null ? Map.of() : Map.copyOf(tools);
        }

        public static Config of(String model) {
            return new Config(model, Optional.empty(), Map.of());
        }

        public static Builder builder(String model) {
            return new Builder(model);
        }

        public static final class Builder {
            private final String model;
            private String systemInstruction;
            private Map<String, BaseTool> tools = Map.of();
            private Builder(String model) { this.model = Objects.requireNonNull(model, "model"); }
            public Builder systemInstruction(String text) { this.systemInstruction = text; return this; }
            public Builder tools(Map<String, BaseTool> tools) { this.tools = tools; return this; }
            public Config build() {
                return new Config(model, Optional.ofNullable(systemInstruction), tools);
            }
        }
    }

    public static final SubnetDef<Void> DEF = SubnetDef.builder(NAME)
            .place(AdkColours.USER_IN)
            .place(AdkColours.LLM_REQUEST)
            .transition(Transition.builder(Transitions.BUILD_PROMPT)
                    .inputs(Arc.In.one(AdkColours.USER_IN))
                    .outputs(Arc.Out.place(AdkColours.LLM_REQUEST))
                    .build())
            .inputPort("userIn",     AdkColours.USER_IN)
            .outputPort("llmRequest", AdkColours.LLM_REQUEST)
            .build();

    public static Map<String, TransitionAction> actionBindings(Config config) {
        Objects.requireNonNull(config, "config");
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.BUILD_PROMPT, buildPromptAction(config));
        return SubnetActions.bind(DEF, session);
    }

    private static TransitionAction buildPromptAction(Config config) {
        return ctx -> {
            Content userContent = ctx.input(AdkColours.USER_IN);
            ctx.output(AdkColours.LLM_REQUEST, LlmRequests.build(
                    config.model(),
                    config.systemInstruction(),
                    config.tools(),
                    List.of(userContent)));
            return CompletableFuture.completedFuture(null);
        };
    }

    private PromptBuilderSubnet() {}
}
