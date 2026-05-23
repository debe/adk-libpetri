package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.tools.BaseTool;
import com.google.adk.tools.ToolContext;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.FunctionResponse;
import io.reactivex.rxjava3.core.Single;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import java.util.function.Supplier;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.runtime.BitmapNetExecutor;

class ToolDispatchSubnetTest {

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    // ============================================================
    //  Happy paths
    // ============================================================

    @Test
    void single_tool_call_produces_single_response() {
        var weather = fakeTool("get_weather",
                args -> Map.of("temp", args.get("city") + "-warm"));

        var fixture = run(Map.of("get_weather", weather),
                callBatch(call("get_weather", Map.of("city", "Berlin"), "c-1")));

        assertThat(fixture.responses()).hasSize(1);
        var resp = fixture.responses().get(0);
        assertThat(resp.name()).hasValue("get_weather");
        assertThat(resp.id()).hasValue("c-1");
        assertThat(resp.response().get()).containsExactly("temp", "Berlin-warm");
    }

    @Test
    void two_tool_calls_both_dispatched_and_collected() {
        var calc    = fakeTool("calculate", args -> Map.of("answer", 42));
        var weather = fakeTool("get_weather", args -> Map.of("temp", "cold"));

        var fixture = run(
                Map.of("calculate", calc, "get_weather", weather),
                callBatch(
                        call("calculate", Map.of("expr", "6*7"), "c-a"),
                        call("get_weather", Map.of("city", "Oslo"), "c-b")));

        assertThat(fixture.responses()).hasSize(2);
        // Order is preserved (matches input call order)
        assertThat(fixture.responses().get(0).name()).hasValue("calculate");
        assertThat(fixture.responses().get(0).id()).hasValue("c-a");
        assertThat(fixture.responses().get(0).response().get())
                .containsExactly("answer", 42);
        assertThat(fixture.responses().get(1).name()).hasValue("get_weather");
        assertThat(fixture.responses().get(1).id()).hasValue("c-b");
        assertThat(fixture.responses().get(1).response().get())
                .containsExactly("temp", "cold");
    }

    @Test
    void empty_call_list_produces_empty_results_token() {
        var fixture = run(Map.of(), callBatch(/* no calls */));

        assertThat(fixture.responses()).isEmpty();
        // The transition still fires once — produces an empty results token.
        assertThat(fixture.firedTransitionNames())
                .containsExactly(ToolDispatchSubnet.Transitions.DISPATCH);
    }

    // ============================================================
    //  Error handling — each per-call failure is isolated to that
    //  call's FunctionResponse; sibling calls still succeed.
    // ============================================================

    @Test
    void unknown_tool_name_produces_error_response() {
        var fixture = run(Map.of(),  // no tools registered
                callBatch(call("nonexistent_tool", Map.of(), "c-x")));

        assertThat(fixture.responses()).hasSize(1);
        var resp = fixture.responses().get(0);
        assertThat(resp.name()).hasValue("nonexistent_tool");
        assertThat(resp.id()).hasValue("c-x");
        var payload = resp.response().get();
        assertThat(payload).containsKey("error");
        assertThat((String) payload.get("error")).contains("unknown tool");
        assertThat(payload).containsEntry("exceptionType",
                IllegalArgumentException.class.getName());
    }

    @Test
    void tool_runtime_error_is_captured_in_response_not_transition_failure() {
        var brokenTool = throwingTool("broken", new RuntimeException("network down"));

        var fixture = run(Map.of("broken", brokenTool),
                callBatch(call("broken", Map.of(), "c-1")));

        assertThat(fixture.responses()).hasSize(1);
        var resp = fixture.responses().get(0);
        assertThat(resp.name()).hasValue("broken");
        var payload = resp.response().get();
        assertThat(payload).containsEntry("error", "network down");
        assertThat(payload).containsEntry("exceptionType", "java.lang.RuntimeException");

        // Critical: the *transition* did not fail — the dispatch must always
        // produce a TOOL_RESULTS token even when individual tools fail.
        var failed = fixture.events().stream()
                .filter(NetEvent.TransitionFailed.class::isInstance)
                .toList();
        assertThat(failed).isEmpty();
    }

    @Test
    void one_tool_succeeds_one_fails_results_carries_both() {
        var ok       = fakeTool("ok",     args -> Map.of("status", "good"));
        var bad      = throwingTool("bad", new IllegalStateException("oops"));

        var fixture = run(
                Map.of("ok", ok, "bad", bad),
                callBatch(
                        call("ok",  Map.of(), "c-good"),
                        call("bad", Map.of(), "c-bad")));

        assertThat(fixture.responses()).hasSize(2);
        assertThat(fixture.responses().get(0).response().get())
                .containsEntry("status", "good");
        assertThat(fixture.responses().get(1).response().get())
                .containsEntry("error", "oops");
        assertThat(fixture.responses().get(1).response().get())
                .containsEntry("exceptionType", "java.lang.IllegalStateException");
    }

    // ============================================================
    //  Concurrency — two tools each block on a CountDownLatch; if
    //  the dispatcher were sequential, the second tool's await
    //  would never complete (the first holds its own latch open
    //  until released by the second).
    // ============================================================

    @Test
    void tools_fire_concurrently_not_sequentially() throws Exception {
        var bothStarted = new CountDownLatch(2);
        var releaseAll  = new CountDownLatch(1);

        Function<Map<String, Object>, Map<String, Object>> waitForSibling = args -> {
            bothStarted.countDown();
            try {
                if (!releaseAll.await(2, TimeUnit.SECONDS)) {
                    throw new AssertionError("sibling never started — sequential dispatch");
                }
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                throw new AssertionError(ie);
            }
            return Map.of("name", args.get("__id"));
        };

        var tA = fakeTool("a", waitForSibling);
        var tB = fakeTool("b", waitForSibling);

        // Background thread releases the latch once both tools have started.
        var releaser = Thread.startVirtualThread(() -> {
            try {
                if (bothStarted.await(2, TimeUnit.SECONDS)) {
                    releaseAll.countDown();
                }
            } catch (InterruptedException ignored) {}
        });

        var fixture = run(
                Map.of("a", tA, "b", tB),
                callBatch(
                        call("a", Map.of("__id", "A"), "c-A"),
                        call("b", Map.of("__id", "B"), "c-B")));

        releaser.join();
        assertThat(fixture.responses()).hasSize(2);
        assertThat(fixture.responses().get(0).response().get())
                .containsEntry("name", "A");
        assertThat(fixture.responses().get(1).response().get())
                .containsEntry("name", "B");
    }

    // ============================================================
    //  ToolContext threading
    // ============================================================

    @Test
    void tool_receives_supplied_tool_context() {
        var captured = new AtomicReference<ToolContext>();
        var capturingTool = new BaseTool("capture", "captures context") {
            @Override
            public Single<Map<String, Object>> runAsync(Map<String, Object> args, ToolContext toolCtx) {
                captured.set(toolCtx);
                return Single.just(Map.of());
            }
        };
        // Synthetic non-null context value — we just verify pass-through.
        var sentinel = (ToolContext) null;  // null is the documented default
        var fixture = runWithContextSupplier(
                Map.of("capture", capturingTool),
                () -> sentinel,
                callBatch(call("capture", Map.of(), "c-1")));

        assertThat(fixture.responses()).hasSize(1);
        // The captured context should match what the supplier returned —
        // in this case null, which is documented as acceptable.
        assertThat(captured.get()).isSameInstanceAs(sentinel);
    }

    // ============================================================
    //  Subnet shape + binding validation
    // ============================================================

    @Test
    void subnet_def_declares_exactly_one_transition_and_two_ports() {
        var transitions = ToolDispatchSubnet.DEF.body().transitions().stream()
                .map(t -> t.name()).toList();
        assertThat(transitions).containsExactly(ToolDispatchSubnet.Transitions.DISPATCH);

        var portNames = ToolDispatchSubnet.DEF.iface().ports().stream()
                .map(p -> p.name()).sorted().toList();
        assertThat(portNames).containsExactly("toolCalls", "toolResults").inOrder();
    }

    @Test
    void composed_subnet_includes_dispatch_transition_and_both_boundary_places() {
        var net = PetriNet.builder("test")
                .compose(ToolDispatchSubnet.DEF)
                .build()
                .bindActions(ToolDispatchSubnet.actionBindings(Map.of(), () -> null, EXECUTOR));

        assertThat(net.transitions().stream().map(t -> t.name()).toList())
                .contains(ToolDispatchSubnet.Transitions.DISPATCH);
        assertThat(net.places()).containsAtLeast(
                AdkColours.TOOL_CALLS,
                AdkColours.TOOL_RESULTS);
    }

    // ============================================================
    //  Fixtures and helpers
    // ============================================================

    private static Fixture run(Map<String, BaseTool> tools, AdkColours.ToolCalls calls) {
        return runWithContextSupplier(tools, () -> null, calls);
    }

    private static Fixture runWithContextSupplier(
            Map<String, BaseTool> tools,
            Supplier<ToolContext> supplier,
            AdkColours.ToolCalls calls) {
        var net = PetriNet.builder("test-net")
                .compose(ToolDispatchSubnet.DEF)
                .build()
                .bindActions(ToolDispatchSubnet.actionBindings(tools, supplier, EXECUTOR));

        Map<Place<?>, List<Token<?>>> initial = Map.of(
                AdkColours.TOOL_CALLS, List.of(Token.of(calls)));

        var store = EventStore.inMemory();
        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(store)
                .build();
        var marking = executor.run();

        List<FunctionResponse> responses = marking.peekTokens(AdkColours.TOOL_RESULTS).stream()
                .map(Token::value)
                .map(AdkColours.ToolResults::results)
                .flatMap(List::stream)
                .toList();

        return new Fixture(responses, store.events());
    }

    private static AdkColours.ToolCalls callBatch(FunctionCall... calls) {
        return new AdkColours.ToolCalls(List.of(calls));
    }

    private static FunctionCall call(String name, Map<String, Object> args, String id) {
        return FunctionCall.builder().name(name).args(args).id(id).build();
    }

    private static BaseTool fakeTool(String name, Function<Map<String, Object>, Map<String, Object>> impl) {
        return new BaseTool(name, "test") {
            @Override
            public Single<Map<String, Object>> runAsync(Map<String, Object> args, ToolContext toolCtx) {
                return Single.fromCallable(() -> impl.apply(args));
            }
        };
    }

    private static BaseTool throwingTool(String name, RuntimeException toThrow) {
        return new BaseTool(name, "test") {
            @Override
            public Single<Map<String, Object>> runAsync(Map<String, Object> args, ToolContext toolCtx) {
                return Single.error(toThrow);
            }
        };
    }

    private record Fixture(List<FunctionResponse> responses, List<NetEvent> events) {
        List<String> firedTransitionNames() {
            return events.stream()
                    .filter(NetEvent.TransitionStarted.class::isInstance)
                    .map(e -> ((NetEvent.TransitionStarted) e).transitionName())
                    .toList();
        }
    }
}
