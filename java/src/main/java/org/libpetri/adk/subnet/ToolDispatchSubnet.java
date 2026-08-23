package org.libpetri.adk.subnet;

import com.google.adk.tools.BaseTool;
import com.google.adk.tools.ToolContext;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.FunctionResponse;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.function.Supplier;
import org.libpetri.core.Arc;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;

/**
 * Stock subnet for the <b>trivial</b> tool-dispatch case: a single
 * {@code Dispatch} transition consumes a {@code TOOL_CALLS} token,
 * fans the contained {@link FunctionCall}s out to their matching
 * {@link BaseTool}s concurrently via the action executor (virtual
 * threads by default), AND-joins the results, and produces one
 * {@code TOOL_RESULTS} token.
 *
 * <p>Use this when each {@link BaseTool} is independent — no
 * inter-tool dependencies, no rate-limit pools, no mutex, no
 * time-window restrictions, no per-tool callbacks. Anything richer
 * belongs to the {@code ToolSubnet} / {@code ResourcePool} primitives
 * (not yet implemented) where each tool becomes its own subnet
 * with typed interface ports and shared-resource arcs.
 *
 * <p>Topology:
 * <pre>
 *   [TOOL_CALLS] --T_Dispatch--> [TOOL_RESULTS]
 * </pre>
 *
 * <p>Per-call failures (tool threw, tool name not found) are
 * <i>not</i> a transition failure — the dispatcher catches them and
 * encodes them in the corresponding {@link FunctionResponse}'s
 * {@code response} map under the keys {@code error} (the throwable
 * message or "unknown tool") and {@code exceptionType} (the
 * throwable's class name, or {@code "java.lang.IllegalArgumentException"}
 * for unknown-tool). This matches ADK's {@code Functions.java} pattern
 * for tool errors — the LLM sees the failure as a normal response part
 * and decides how to recover.
 *
 * <p>Bind via
 * {@code petriNet.bindActions(ToolDispatchSubnet.actionBindings(toolsByName, contextSupplier, dispatchExecutor))}.
 */
public final class ToolDispatchSubnet {

    public static final String NAME = "ToolDispatch";

    public static final class Transitions {
        public static final String DISPATCH = NAME + "_Dispatch";
        private Transitions() {}
    }

    public static final SubnetDef<Void> DEF = SubnetDef.builder(NAME)
            .place(AdkColours.TOOL_CALLS)
            .place(AdkColours.TOOL_RESULTS)
            .transition(Transition.builder(Transitions.DISPATCH)
                    .inputs(Arc.In.one(AdkColours.TOOL_CALLS))
                    .outputs(Arc.Out.place(AdkColours.TOOL_RESULTS))
                    .build())
            .inputPort("toolCalls",   AdkColours.TOOL_CALLS)
            .outputPort("toolResults", AdkColours.TOOL_RESULTS)
            .build();

    /**
     * Full binding map for this subnet.
     *
     * @param toolsByName       lookup keyed by {@link BaseTool#name()}. Must
     *                          be non-null; an empty map is legal (every call
     *                          routes to an unknown-tool error response).
     * @param contextSupplier   supplies the {@link ToolContext} passed to
     *                          every {@link BaseTool#runAsync} invocation.
     *                          May return {@code null} if the tools don't
     *                          consult the context. Called once per call
     *                          within a dispatch fire.
     * @param dispatchExecutor  executor used to run individual tool
     *                          invocations concurrently. Each call gets its
     *                          own task; the action's returned future
     *                          completes when all tasks finish. A
     *                          virtual-thread-per-task executor is a typical
     *                          choice so tools doing blocking I/O don't pin
     *                          any platform thread.
     */
    public static Map<String, TransitionAction> actionBindings(
            Map<String, BaseTool> toolsByName,
            Supplier<ToolContext> contextSupplier,
            ExecutorService dispatchExecutor) {
        Objects.requireNonNull(toolsByName, "toolsByName");
        Objects.requireNonNull(contextSupplier, "contextSupplier");
        Objects.requireNonNull(dispatchExecutor, "dispatchExecutor");
        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.DISPATCH,
                dispatchAction(toolsByName, contextSupplier, dispatchExecutor));
        return SubnetActions.bind(DEF, session);
    }

    private static TransitionAction dispatchAction(
            Map<String, BaseTool> tools,
            Supplier<ToolContext> contextSupplier,
            ExecutorService dispatchExecutor) {
        return ctx -> {
            var batch = ctx.input(AdkColours.TOOL_CALLS);
            var calls = batch.calls();

            if (calls.isEmpty()) {
                ctx.output(AdkColours.TOOL_RESULTS,
                        new AdkColours.ToolResults(List.of()));
                return CompletableFuture.completedFuture(null);
            }

            // Each call runs as its own task on the dispatch executor so
            // libpetri's single action thread doesn't serialize them. With
            // the default virtual-thread-per-task executor, blocking tools
            // don't pin platform threads.
            List<CompletableFuture<FunctionResponse>> futures = new ArrayList<>(calls.size());
            for (var call : calls) {
                var toolCtx = contextSupplier.get();
                futures.add(CompletableFuture.supplyAsync(
                        () -> dispatchOneSync(tools, call, toolCtx),
                        dispatchExecutor));
            }

            return CompletableFuture
                    .allOf(futures.toArray(new CompletableFuture<?>[0]))
                    .thenAccept(v -> {
                        List<FunctionResponse> responses = new ArrayList<>(futures.size());
                        for (var f : futures) responses.add(f.join());
                        ctx.output(AdkColours.TOOL_RESULTS,
                                new AdkColours.ToolResults(responses));
                    });
        };
    }

    /**
     * Runs one call synchronously on the dispatch task's thread (a virtual
     * thread by default). Errors become structured error responses; the
     * transition never fails for a per-call failure.
     */
    private static FunctionResponse dispatchOneSync(
            Map<String, BaseTool> tools, FunctionCall call, ToolContext toolContext) {
        var name = call.name().orElse("");
        var args = call.args().orElse(Map.of());
        var tool = tools.get(name);
        if (tool == null) {
            return errorResponse(call,
                    "unknown tool: '" + name + "'",
                    IllegalArgumentException.class.getName());
        }
        try {
            // blockingGet on a virtual thread is cheap and lets us treat
            // RxJava Single as a normal value-returning call here.
            var result = tool.runAsync(args, toolContext).blockingGet();
            return successResponse(call, result);
        } catch (Exception err) {
            // Exception, not Throwable. A per-call failure becoming a structured
            // error response is the point; an Error is not a per-call failure.
            // Catching it would tell the model its weather tool had a problem
            // while the JVM is going down, and would swallow the one class of
            // failure that must reach the orchestrator. libpetri rethrows Error
            // for the same reason.
            return errorResponse(call, err.getMessage(), err.getClass().getName());
        }
    }

    private static FunctionResponse successResponse(FunctionCall call, Map<String, Object> result) {
        var builder = FunctionResponse.builder()
                .name(call.name().orElse(""))
                .response(result);
        call.id().ifPresent(builder::id);
        return builder.build();
    }

    private static FunctionResponse errorResponse(FunctionCall call, String message, String exceptionType) {
        var payload = new LinkedHashMap<String, Object>();
        payload.put("error", message == null ? "" : message);
        payload.put("exceptionType", exceptionType);
        var builder = FunctionResponse.builder()
                .name(call.name().orElse(""))
                .response(payload);
        call.id().ifPresent(builder::id);
        return builder.build();
    }

    private ToolDispatchSubnet() {}
}
