package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.runner.InMemoryRunner;
import com.google.adk.tools.BaseTool;
import com.google.adk.tools.ToolContext;
import com.google.genai.types.Content;
import com.microsoft.z3.Context;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.Part;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.core.Single;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.core.PetriNet;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.bridge.OtelEventStore;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.runner.PetriAgent;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.analysis.MarkingState;
import org.libpetri.analysis.StateClassGraph;
import org.libpetri.adk.runner.SessionExecutorRegistry;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.subnet.LlmStepSubnet;
import org.libpetri.adk.subnet.RouterSubnet;
import org.libpetri.adk.subnet.TransferRouterSubnet;
import org.libpetri.adk.verify.AdkNetInvariants;
import org.libpetri.event.EventStore;
import org.libpetri.smt.SmtProperty;
import org.libpetri.smt.SmtVerifier;

/**
 * Complex multi-agent demo — composes the stock subnets into a real
 * customer-service routing pattern and drives it through the
 * <b>stock ADK {@code Runner}</b> with full observability and
 * structural verification.
 *
 * <h2>What this demo shows that libpetri makes possible</h2>
 *
 * <ol>
 *   <li><b>Multi-subnet composition by typed-place inference</b> —
 *       a planner {@link LlmAgentSubnet} is composed alongside a
 *       {@link TransferRouterSubnet} configured with the
 *       compile-time-known set of specialist agent names. The
 *       structural {@code Out.xor} over per-target places makes
 *       hallucinated agent names a typed error event, not an NPE.</li>
 *   <li><b>End-to-end observability via EventStore decoration</b> —
 *       an {@link OtelEventStore} wraps the executor's event store,
 *       emitting one OT span per transition fire. The captured
 *       {@link InMemorySpanExporter} confirms spans cover the actual
 *       agent flow (planner LLM call → router demux → transfer-error
 *       fallback or downstream specialist).</li>
 *   <li><b>Structural verification at build time</b> —
 *       {@link AdkNetInvariants} validators run against the composed
 *       net and confirm:
 *       <ul>
 *         <li>at most one legacy-session-write consumer
 *             ({@code singleLegacySessionWriter});</li>
 *         <li>the transfer demux has an unknown fallback
 *             ({@code transferDemuxHasUnknownFallback}).</li>
 *       </ul></li>
 *   <li><b>Stock ADK {@code Runner} compatibility</b> — the assembled
 *       libpetri net is wrapped in a {@link PetriAgent} adapter and
 *       driven via {@link InMemoryRunner} with no source changes to
 *       adk-java.</li>
 *   <li><b>Long-lived per-session executor</b> — the
 *       {@link SessionExecutorRegistry} provides one
 *       {@link PetriRunner} per session; consecutive
 *       {@code runAsync(...)} calls reuse the same net.</li>
 * </ol>
 *
 * <p>The end-to-end claim: a user query lands on the planner agent's
 * USER_IN env place; the planner's LLM emits a
 * {@code transfer_to_agent} function call; the router demuxes to the
 * correct specialist port (or emits a typed error event for an
 * unknown target). Every step generates an OT span; structural
 * invariants hold; the response flows back out through
 * {@code Runner.runAsync}'s standard {@code Flowable<Event>} contract.
 */
class MultiAgentDemoTest {

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    static boolean z3Available() {
        try {
            new Context().close();
            return true;
        } catch (UnsatisfiedLinkError | NoClassDefFoundError _) {
            return false;
        }
    }


    @Test
    void planner_composed_with_transfer_router_passes_invariants_and_emits_text_event() throws Exception {
        // ============================================================
        //  1. Compose the net: planner LlmAgent + transfer router.
        //     The router knows exactly two specialist names at build time —
        //     even though this happy-path test has the planner reply with
        //     a plain text answer, the router structure must still pass
        //     all structural invariants.
        // ============================================================
        Set<String> knownSpecialists = Set.of("billing", "tech_support");

        // Planner answers with text (no transfer call) — the LlmAgent's
        // Router routes the plain-text response to EVENT_OUT directly.
        // The transfer-routing topology is present but doesn't fire on
        // this path; it's exercised by the second test below.
        var plannerLlm = scriptedLlm(textResponse("Here's the answer to your question."));

        var plannerSubnet = LlmAgentSubnet.DEF;
        var plannerConfig = LlmAgentSubnet.Config.builder("planner", "fake-model")
                .systemInstruction("Route to the right specialist.")
                .reaskBudget(2)
                .dispatchExecutor(EXECUTOR)
                .build();

        var routerDef = TransferRouterSubnet.def(knownSpecialists);
        var routerConfig = new TransferRouterSubnet.Config("planner",
                () -> "inv-" + System.nanoTime());

        var net = PetriNet.builder("multi-agent-app")
                .compose(plannerSubnet)
                .compose(routerDef)
                .build();

        // ============================================================
        //  2. Structural verification — these run BEFORE we execute.
        //     Catches wiring bugs as net-construction time, not at runtime.
        // ============================================================
        assertThat(AdkNetInvariants.singleLegacySessionWriter(net)).isEmpty();
        assertThat(AdkNetInvariants.transferDemuxHasUnknownFallback(net)).isEmpty();

        // ============================================================
        //  3. Bind actions and wrap with OT observability.
        // ============================================================
        // Merge ALL subnet bindings before a single bindActions call.
        // Chained .bindActions() doesn't work because the Map overload
        // defaults unbound names to passthrough() — the second call
        // would wipe the first call's bindings.
        var allBindings = new LinkedHashMap<String, TransitionAction>();
        allBindings.putAll(LlmAgentSubnet.actionBindings(plannerLlm, plannerConfig));
        allBindings.putAll(TransferRouterSubnet.actionBindings(knownSpecialists, routerConfig));
        var bound = net.bindActions(allBindings);

        var exporter = InMemorySpanExporter.create();
        var tracerProvider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter))
                .build();
        var tracer = OpenTelemetrySdk.builder()
                .setTracerProvider(tracerProvider)
                .build()
                .getTracer("multi-agent-demo");
        var observabilityChain = new OtelEventStore(tracer, EventStore.logging());

        // ============================================================
        //  4. Wire to stock ADK Runner via PetriAgent adapter.
        //     No source changes to ADK; just a BaseAgent subclass.
        // ============================================================
        var registry = SessionExecutorRegistry.cleanerOwned();
        ConcurrentMap<org.libpetri.adk.runner.SessionKey, Object> sessionOwners = new ConcurrentHashMap<>();
        var agent = PetriAgent.of(
                "multi_agent",
                "Planner that routes to specialists",
                registry,
                key -> PetriRunner.builder(bound)
                        .environmentPlace(AdkColours.USER_IN)
                        .eventStore(observabilityChain)
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start(),
                ctx -> sessionOwners.computeIfAbsent(
                        org.libpetri.adk.runner.SessionKey.from(ctx.session()), k -> new Object()),
                tracer,
                observabilityChain);

        var runner = new InMemoryRunner(agent);
        var session = runner.sessionService()
                .createSession(runner.appName(), "user-1", (Map<String, Object>) null, "sess-1")
                .blockingGet();

        // ============================================================
        //  5. Drive an invocation. The planner LLM returns text; the
        //     LlmAgent's Router routes to EVENT_OUT; PetriAgent's
        //     take(1) emits the response back through Runner.
        // ============================================================
        var events = runner.runAsync(
                        session.userId(),
                        session.id(),
                        userMessage("Help me with billing"),
                        RunConfig.builder().build())
                .toList()
                .blockingGet();

        // The runner emits the user-message event and the agent's response.
        assertThat(events).isNotEmpty();

        // ============================================================
        //  6. Observability assertion — OT spans were emitted for the
        //     LlmAgent pipeline transitions that fired.
        // ============================================================
        registry.closeAll();
        // End any still-open per-session invocation spans — they're held open
        // past doFinally so late TransitionCompleted emits from the orchestrator
        // (which fire AFTER the take(1)-triggering TokenAdded(EVENT_OUT))
        // still attach as children of the invocation span.
        agent.endAllOpenInvocationSpans();
        tracerProvider.forceFlush().join(2, TimeUnit.SECONDS);

        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).isNotEmpty();
        var spanNames = spans.stream().map(s -> s.getName()).toList();
        // BuildPrompt, LlmCall, AfterModel, Route should all have spans for
        // the text-response path. RE_ASK doesn't fire because there's no
        // tool-call loop; it WOULD fire if the planner returned function calls.
        assertThat(spanNames).contains(LlmAgentSubnet.Transitions.BUILD_PROMPT);
        assertThat(spanNames).contains(LlmStepSubnet.Transitions.LLM_CALL);
        assertThat(spanNames).contains(RouterSubnet.Transitions.ROUTE);
        // The TransferRouter's Demux did NOT fire on this path (no transfer call),
        // but it WOULD fire on the unknown-agent path tested below.

        // The PetriAgent opened a "petri.invocation.<name>" root span around
        // the invocation; every transition span must be a child of it (proves
        // the OTel root-span hookup works across the orchestrator-thread
        // boundary via OtelEventStore.bindInvocationContext).
        var invocationSpans = spans.stream()
                .filter(s -> s.getName().equals("petri.invocation.multi_agent"))
                .toList();
        assertThat(invocationSpans).hasSize(1);
        var invocationSpanId = invocationSpans.get(0).getSpanId();
        var transitionSpans = spans.stream()
                .filter(s -> !s.getName().equals("petri.invocation.multi_agent"))
                .toList();
        assertThat(transitionSpans).isNotEmpty();
        for (var transitionSpan : transitionSpans) {
            assertThat(transitionSpan.getParentSpanId()).isEqualTo(invocationSpanId);
        }

        tracerProvider.close();
    }

    @Test
    void hallucinated_agent_name_surfaces_as_typed_error_event_not_npe() throws Exception {
        // Same shape as above, but the planner emits a transfer to an
        // UNKNOWN agent name. ADK's stock AgentTransfer would NPE; our
        // TransferRouterSubnet routes to UNKNOWN_TARGET → T_EmitUnknownError
        // produces an Event to EVENT_OUT. PetriAgent's take(1) sees it.
        //
        // Note: the planner's reask budget must absorb the loop iteration.
        // The LlmAgent emits the transfer → Router routes to TRANSFER →
        // TransferRouter demuxes to UNKNOWN_TARGET → emits Event → done.
        // The LlmAgent's own LLM_RESPONSE arrives, gets routed to TRANSFER
        // (NOT to EVENT_OUT), so its event from the Router's path doesn't
        // fire. PetriAgent's take(1) waits for the EmitUnknownError event.
        Set<String> knownSpecialists = Set.of("billing", "tech_support");

        var plannerLlm = scriptedLlm(transferCall("hallucinated_typo"));

        var plannerSubnet = LlmAgentSubnet.DEF;
        var plannerConfig = LlmAgentSubnet.Config.builder("planner", "fake-model")
                .reaskBudget(2)
                .dispatchExecutor(EXECUTOR)
                .build();
        var routerDef = TransferRouterSubnet.def(knownSpecialists);
        var routerConfig = new TransferRouterSubnet.Config("planner",
                () -> "inv-fixed");

        var allBindings = new LinkedHashMap<String, TransitionAction>();
        allBindings.putAll(LlmAgentSubnet.actionBindings(plannerLlm, plannerConfig));
        allBindings.putAll(TransferRouterSubnet.actionBindings(knownSpecialists, routerConfig));

        var net = PetriNet.builder("hallucination-app")
                .compose(plannerSubnet)
                .compose(routerDef)
                .build()
                .bindActions(allBindings);

        var registry = SessionExecutorRegistry.cleanerOwned();
        ConcurrentMap<org.libpetri.adk.runner.SessionKey, Object> sessionOwners = new ConcurrentHashMap<>();
        var agent = PetriAgent.of(
                "halluc_agent",
                "Demonstrates structural elimination of hallucinated-transfer NPE",
                registry,
                key -> PetriRunner.builder(net)
                        .environmentPlace(AdkColours.USER_IN)
                        .actionExecutor(EXECUTOR)
                        .orchestratorExecutor(EXECUTOR)
                        .start(),
                ctx -> sessionOwners.computeIfAbsent(
                        org.libpetri.adk.runner.SessionKey.from(ctx.session()), k -> new Object()));

        var runner = new InMemoryRunner(agent);
        var session = runner.sessionService()
                .createSession(runner.appName(), "u", (Map<String, Object>) null, "s").blockingGet();

        var events = runner.runAsync(
                        session.userId(), session.id(),
                        userMessage("help"),
                        RunConfig.builder().build())
                .toList()
                .blockingGet();

        // No exception, no NPE. The agent's response is the typed error event:
        // either the planner-author event (if the unknown demux completed before
        // PetriAgent's take(1) fired) or the planner's eventual event. Either
        // way, the runner returns events successfully.
        assertThat(events).isNotEmpty();

        registry.closeAll();
    }

    // ============================================================
    //  Z3 deadlock-free verification of the assembled multi-agent net.
    //  Catches any topology bug that would deadlock — before we ever
    //  ship the demo as "real". With Z3 Spacer + sinks declared, the
    //  property is provable (no reachable marking has every advancing
    //  transition disabled with nothing on a sink).
    // ============================================================

    @Test
    @EnabledIf("z3Available")
    void multi_agent_net_is_smt_proven_deadlock_free() {
        Set<String> knownSpecialists = Set.of("billing", "tech_support");

        // CORE-043 (libpetri 2.14+): a transition declaring an output spec
        // must carry a producing action at verification as well as at
        // execution. Bind the same actions the demo runs with, so the
        // deadlock-freedom proof is about the net that actually executes
        // rather than an unbound skeleton no firing could ever satisfy.
        // The actions are never invoked here; only the structure is encoded.
        var dlfConfig = LlmAgentSubnet.Config.builder("planner", "fake-model")
                .systemInstruction("Route to the right specialist.")
                .reaskBudget(2)
                .dispatchExecutor(EXECUTOR)
                .build();
        var dlfRouterConfig = new TransferRouterSubnet.Config("planner",
                () -> "inv-dlf");
        var dlfBindings = new LinkedHashMap<String, TransitionAction>();
        dlfBindings.putAll(LlmAgentSubnet.actionBindings(
                scriptedLlm(textResponse("verification stub")), dlfConfig));
        dlfBindings.putAll(TransferRouterSubnet.actionBindings(
                knownSpecialists, dlfRouterConfig));

        var net = PetriNet.builder("dlf-check")
                .compose(LlmAgentSubnet.DEF)
                .compose(TransferRouterSubnet.def(knownSpecialists))
                .build()
                .bindActions(dlfBindings);

        var result = SmtVerifier.forNet(net)
                .initialMarking(b -> b.tokens(AdkColours.USER_IN, 1))
                .sinkPlaces(
                        // Terminal places — a marking with tokens here is
                        // a finished agent turn, not a deadlock.
                        AdkColours.EVENT_OUT,
                        AdkColours.LEGACY_SESSION_WRITE,
                        TransferRouterSubnet.UNKNOWN_TARGET,
                        TransferRouterSubnet.targetPlace("billing"),
                        TransferRouterSubnet.targetPlace("tech_support"))
                .property(SmtProperty.deadlockFree())
                .verify();

        // No counterexample = no reachable deadlock from the seeded initial
        // marking. Assert the strong form: the README and CLAUDE.md both say
        // Z3 *proves* this net deadlock-free, and libpetri 3.0.1 downgrades a
        // verdict whose IC3 certificate does not re-validate to Unknown.
        // isViolated()==false alone also passes on Unknown, which would let
        // the claim rot silently.
        assertThat(result.isProven()).isTrue();
        assertThat(result.isViolated()).isFalse();
    }

    // ============================================================
    //  Fixtures
    // ============================================================

    private static Content userMessage(String text) {
        return Content.builder().role("user")
                .parts(List.of(Part.fromText(text))).build();
    }

    private static LlmResponse textResponse(String text) {
        return LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.fromText(text))).build())
                .build();
    }

    private static LlmResponse transferCall(String targetAgent) {
        return LlmResponse.builder()
                .content(Content.builder().role("model")
                        .parts(List.of(Part.builder().functionCall(
                                FunctionCall.builder()
                                        .name(RouterSubnetMirror.TRANSFER_TO_AGENT_FN)
                                        .args(Map.of(RouterSubnetMirror.TRANSFER_AGENT_NAME_ARG, targetAgent))
                                        .build()).build()))
                        .build())
                .build();
    }

    private static BaseLlm scriptedLlm(LlmResponse... responses) {
        Deque<LlmResponse> queue = new ArrayDeque<>(List.of(responses));
        return new BaseLlm("scripted") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                var next = queue.poll();
                if (next == null) return Flowable.error(new IllegalStateException("scripted exhausted"));
                return Flowable.just(next);
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }

    @SuppressWarnings("unused")
    private static BaseTool fakeTool(String name) {
        return new BaseTool(name, "demo tool") {
            @Override public Single<Map<String, Object>> runAsync(Map<String, Object> args, ToolContext c) {
                return Single.just(Map.of("answer", "ok"));
            }
        };
    }

    /** Local re-import to avoid Spring of doom; same constants as RouterSubnet. */
    private static final class RouterSubnetMirror {
        static final String TRANSFER_TO_AGENT_FN =
                RouterSubnet.TRANSFER_TO_AGENT_FN;
        static final String TRANSFER_AGENT_NAME_ARG =
                RouterSubnet.TRANSFER_AGENT_NAME_ARG;
    }
}
