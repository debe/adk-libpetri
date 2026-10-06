package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.runner.InMemoryRunner;
import com.google.genai.types.Content;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.Part;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.reactivex.rxjava3.core.Flowable;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIf;
import org.libpetri.core.PetriNet;
import org.libpetri.adk.bridge.OtelEventStore;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.runner.PetriAgent;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.adk.runner.SessionExecutorRegistry;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.subnet.LlmStepSubnet;
import org.libpetri.adk.subnet.RouterSubnet;
import org.libpetri.adk.subnet.SubnetActions;
import org.libpetri.adk.subnet.TransferRouterSubnet;
import org.libpetri.adk.verify.AdkNetInvariants;
import org.libpetri.adk.verify.SmtProofs;
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
 *             ({@code singleLegacySessionWriter}), which holds vacuously
 *             here: this net composes no {@code PersistStateSubnet};</li>
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
        return SmtVerifier.z3Available();
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
        // One bindComposed call for all subnets. Chained .bindActions()
        // doesn't work because the Map overload defaults unbound names to
        // passthrough(), so the second call would wipe the first's bindings;
        // bindComposed also rejects overlapping maps and uncovered transitions.
        var bound = SubnetActions.bindComposed(net,
                LlmAgentSubnet.actionBindings(plannerLlm, plannerConfig),
                TransferRouterSubnet.actionBindings(knownSpecialists, routerConfig));

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
        var registry = SessionExecutorRegistry.strongOwned();
        var agent = PetriAgent.builder("multi_agent", registry,
                key -> PetriRunner.builder(bound)
                        .environmentPlace(AdkColours.USER_IN)
                        .eventStore(observabilityChain)
                        .orchestratorExecutor(EXECUTOR)
                        .start())
                .description("Planner that routes to specialists")
                .tracing(tracer, observabilityChain)
                .build();

        try {
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

            // Assert the agent's actual reply, not just that events exist.
            // InMemoryRunner emits the user-message event unconditionally, so
            // isNotEmpty() stayed green even if the net produced nothing at all,
            // which is the whole thing this demo is here to show.
            var agentText = events.stream()
                    .filter(e -> "planner".equals(e.author()))
                    .map(e -> e.content().map(Content::text).orElse(""))
                    .filter(t -> t != null && !t.isBlank())
                    .reduce((a, b) -> b)
                    .orElse(null);
            assertThat(agentText).isEqualTo("Here's the answer to your question.");
        } finally {
            registry.closeAll();
        }

        // ============================================================
        //  6. Observability assertion — OT spans were emitted for the
        //     LlmAgent pipeline transitions that fired.
        // ============================================================
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
        // UNKNOWN agent name. ADK's findAgent returns Optional.empty() and
        // AgentTransfer records the string unvalidated, so the failure is
        // untyped downstream (see TransferUnknownTargetAdkFoilTest); our
        // TransferRouterSubnet routes to UNKNOWN_TARGET →
        // TransferRouter_EmitUnknownError produces a typed error Event on
        // EVENT_OUT. PetriAgent's take(1) sees it.
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

        var net = SubnetActions.bindComposed(
                PetriNet.builder("hallucination-app")
                        .compose(plannerSubnet)
                        .compose(routerDef)
                        .build(),
                LlmAgentSubnet.actionBindings(plannerLlm, plannerConfig),
                TransferRouterSubnet.actionBindings(knownSpecialists, routerConfig));

        var registry = SessionExecutorRegistry.strongOwned();
        var agent = PetriAgent.builder("halluc_agent", registry,
                key -> PetriRunner.builder(net)
                        .environmentPlace(AdkColours.USER_IN)
                        .orchestratorExecutor(EXECUTOR)
                        .start())
                .description("Demonstrates structural elimination of hallucinated-transfer NPE")
                .build();

        try {
            var runner = new InMemoryRunner(agent);
            var session = runner.sessionService()
                    .createSession(runner.appName(), "u", (Map<String, Object>) null, "s").blockingGet();

            var events = runner.runAsync(
                            session.userId(), session.id(),
                            userMessage("help"),
                            RunConfig.builder().build())
                    .toList()
                    .blockingGet();

            // No exception, no NPE. But "the runner returned events" is not the
            // claim this test's name makes: assert the hallucinated name actually
            // surfaced as a typed error event. The previous comment widened the
            // claim until nothing could falsify it, and isNotEmpty() passes on the
            // user-message event alone.
            var errorText = events.stream()
                    .map(e -> e.content().map(Content::text).orElse(""))
                    .filter(t -> t != null && t.contains("hallucinated_typo"))
                    .findFirst();
            assertThat(errorText).isPresent();
        } finally {
            registry.closeAll();
        }
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
        var net = SubnetActions.bindComposed(
                PetriNet.builder("dlf-check")
                        .compose(LlmAgentSubnet.DEF)
                        .compose(TransferRouterSubnet.def(knownSpecialists))
                        .build(),
                LlmAgentSubnet.actionBindings(
                        scriptedLlm(textResponse("verification stub")), dlfConfig),
                TransferRouterSubnet.actionBindings(knownSpecialists, dlfRouterConfig));

        // One verify() per property: SmtVerifier.property() replaces rather
        // than adds, so the chain this used to be checked only its last one.
        // Assert the strong form: the README and CLAUDE.md both say Z3
        // *proves* this net deadlock-free, and libpetri downgrades a verdict
        // it cannot validate (certificate or closed enumeration) to Unknown.
        SmtProofs.assertEachProven(net,
                v -> v.initialMarking(b -> b.tokens(AdkColours.USER_IN, 1)
                                // The planner's turn permit, which PetriRunner seeds.
                                .tokens(AdkColours.TURN_PERMIT, 1))
                        .sinkPlaces(
                                // Terminal places — a marking with tokens here is
                                // a finished agent turn, not a deadlock.
                                AdkColours.EVENT_OUT,
                                AdkColours.LEGACY_SESSION_WRITE,
                                TransferRouterSubnet.UNKNOWN_TARGET,
                                TransferRouterSubnet.targetPlace("billing"),
                                TransferRouterSubnet.targetPlace("tech_support"),
                                // The planner at rest holds its permit and nothing
                                // else: the transition that ends a turn clears its
                                // conversation and reask budget. Excusing the permit
                                // cannot hide a stalled turn, which holds no permit.
                                AdkColours.TURN_PERMIT),
                Map.of(
                        "deadlockFree",
                        SmtProperty.deadlockFree(),
                        // One user turn yields at most one egress event: the
                        // router's answer, or the reask-exhausted fallback,
                        // never both.
                        "one egress event per turn: eventOutBounded(1)",
                        AdkNetInvariants.eventOutBounded(1)));
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
                                        .name(RouterSubnet.TRANSFER_TO_AGENT_FN)
                                        .args(Map.of(RouterSubnet.TRANSFER_AGENT_NAME_ARG, targetAgent))
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

}
