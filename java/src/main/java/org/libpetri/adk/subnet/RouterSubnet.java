package org.libpetri.adk.subnet;

import com.google.adk.events.Event;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.Part;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Optional;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.function.Supplier;
import org.libpetri.core.Arc;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;

/**
 * Stock subnet that inspects an {@link LlmResponse} and routes via
 * {@code Out.xor} to one of three boundary places:
 *
 * <pre>
 *   [LLM_RESPONSE] --T_Route--> Out.xor( [TOOL_CALLS], [TRANSFER], [EVENT_OUT] )
 * </pre>
 *
 * <p>Routing rules (first match wins):
 * <ol>
 *   <li>Response contains a {@link FunctionCall} named {@value #TRANSFER_TO_AGENT_FN}
 *       → produce a {@link AdkColours.TransferTarget} carrying the {@code agent_name}
 *       argument to {@link AdkColours#TRANSFER}.</li>
 *   <li>Response contains any other {@link FunctionCall}s
 *       → produce a {@link AdkColours.ToolCalls} bundle to {@link AdkColours#TOOL_CALLS}.</li>
 *   <li>Otherwise (text-only or empty content)
 *       → wrap the response's {@link com.google.genai.types.Content} in a final
 *       {@link Event} and produce to {@link AdkColours#EVENT_OUT}.</li>
 * </ol>
 *
 * <p>The transfer rule matches ADK's {@code AgentTransfer} convention.
 * When the model produces both a {@code transfer_to_agent} call <i>and</i>
 * other function calls, transfer takes precedence — the route is
 * structurally exclusive (the XOR validator enforces exactly one
 * production per fire).
 *
 * <p>Bind via
 * {@code petriNet.bindActions(RouterSubnet.actionBindings(RouterConfig.of("agent-name")))}.
 */
public final class RouterSubnet {

    public static final String NAME = "Router";

    /** Function-call name the model uses to request an agent hand-off. */
    public static final String TRANSFER_TO_AGENT_FN = "transfer_to_agent";

    /** Argument key carrying the target agent name on a transfer call. */
    public static final String TRANSFER_AGENT_NAME_ARG = "agent_name";

    public static final class Transitions {
        public static final String ROUTE = NAME + "_Route";
        private Transitions() {}
    }

    public record Config(String author, Supplier<String> invocationIdSupplier) {
        public Config {
            Objects.requireNonNull(author, "author");
            Objects.requireNonNull(invocationIdSupplier, "invocationIdSupplier");
        }

        /**
         * Default config: caller-supplied author, random UUID per
         * invocation id.
         */
        public static Config of(String author) {
            return new Config(author, () -> UUID.randomUUID().toString());
        }
    }

    public static final SubnetDef<Void> DEF = SubnetDef.builder(NAME)
            .place(AdkColours.LLM_RESPONSE)
            .place(AdkColours.TOOL_CALLS)
            .place(AdkColours.TRANSFER)
            .place(AdkColours.EVENT_OUT)
            .transition(Transition.builder(Transitions.ROUTE)
                    .inputs(Arc.In.one(AdkColours.LLM_RESPONSE))
                    .outputs(Arc.Out.xor(
                            AdkColours.TOOL_CALLS,
                            AdkColours.TRANSFER,
                            AdkColours.EVENT_OUT))
                    .build())
            .inputPort("llmResponse",  AdkColours.LLM_RESPONSE)
            .outputPort("toolCalls",   AdkColours.TOOL_CALLS)
            .outputPort("transfer",    AdkColours.TRANSFER)
            .outputPort("eventOut",    AdkColours.EVENT_OUT)
            .build();

    public static Map<String, TransitionAction> actionBindings(Config config) {
        Objects.requireNonNull(config, "config");
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.ROUTE, routeAction(config));
        return SubnetActions.bind(DEF, session);
    }

    private static TransitionAction routeAction(Config config) {
        return ctx -> {
            LlmResponse response = ctx.input(AdkColours.LLM_RESPONSE);
            List<FunctionCall> functionCalls = extractFunctionCalls(response);
            var transfer = findTransferCall(functionCalls);

            if (transfer.isPresent()) {
                String agentName = transferAgentName(transfer.get());
                ctx.output(AdkColours.TRANSFER, new AdkColours.TransferTarget(agentName));
            } else if (!functionCalls.isEmpty()) {
                ctx.output(AdkColours.TOOL_CALLS, new AdkColours.ToolCalls(functionCalls));
            } else {
                Event event = Event.builder()
                        .invocationId(config.invocationIdSupplier().get())
                        .author(config.author())
                        .content(response.content().orElse(null))
                        .build();
                ctx.output(AdkColours.EVENT_OUT, event);
            }
            return CompletableFuture.completedFuture(null);
        };
    }

    private static List<FunctionCall> extractFunctionCalls(LlmResponse response) {
        List<FunctionCall> calls = new ArrayList<>();
        response.content()
                .flatMap(c -> c.parts())
                .ifPresent(parts -> {
                    for (Part p : parts) {
                        p.functionCall().ifPresent(calls::add);
                    }
                });
        return calls;
    }

    /** The transfer call, if the model asked for one. */
    private static Optional<FunctionCall> findTransferCall(List<FunctionCall> calls) {
        for (var c : calls) {
            if (TRANSFER_TO_AGENT_FN.equals(c.name().orElse(null))) {
                return Optional.of(c);
            }
        }
        return Optional.empty();
    }

    private static String transferAgentName(FunctionCall transferCall) {
        return transferCall.args()
                .map(args -> args.get(TRANSFER_AGENT_NAME_ARG))
                .map(Object::toString)
                .orElse("");
    }

    private RouterSubnet() {}
}
