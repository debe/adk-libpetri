package org.libpetri.adk.runner;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.LiveRequestQueue;
import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.runner.InMemoryRunner;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.subscribers.TestSubscriber;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;

/**
 * End-to-end: drive the libpetri agent through the <b>stock ADK
 * {@code Runner}</b> with no source changes to ADK. The {@link PetriAgent}
 * adapter looks like any other {@code BaseAgent} from ADK's perspective.
 */
class PetriAgentIntegrationTest {

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    @Test
    void agent_responds_via_adk_runner_runAsync() {
        var llm = scriptedLlm(textResponse("hello from petri"));

        // The same factory each session-runner uses to build its long-lived net.
        var registry = new SessionExecutorRegistry();
        var sessionOwners = sessionOwnerMap();
        var agent = PetriAgent.of(
                "petri_agent",
                "Petri-backed LLM agent",
                registry,
                key -> petriRunner(llm, "petri_agent"),
                ctx -> sessionOwners.computeIfAbsent(SessionKey.from(ctx.session()), k -> new Object()));

        // Stock InMemoryRunner — no fork, no special wiring.
        var adkRunner = new InMemoryRunner(agent);
        var session = adkRunner.sessionService()
                .createSession(adkRunner.appName(), "user-1", (Map<String, Object>) null, "session-1")
                .blockingGet();

        var events = adkRunner.runAsync(
                        session.userId(),
                        session.id(),
                        userMessage("hi"),
                        RunConfig.builder().build())
                .toList()
                .blockingGet();

        assertThat(events).isNotEmpty();
        // The final event in the stream is our agent's response.
        Event lastFromAgent = events.stream()
                .filter(e -> "petri_agent".equals(e.author()))
                .reduce((first, second) -> second)
                .orElseThrow();
        assertThat(lastFromAgent.content().get().text()).isEqualTo("hello from petri");
    }

    @Test
    void multiple_invocations_in_same_session_reuse_the_long_lived_runner() {
        var llm = scriptedLlm(textResponse("first"), textResponse("second"), textResponse("third"));

        var registry = new SessionExecutorRegistry();
        var sessionOwners = sessionOwnerMap();
        var agent = PetriAgent.of(
                "long_lived",
                "stays alive across messages",
                registry,
                key -> petriRunner(llm, "long_lived"),
                ctx -> sessionOwners.computeIfAbsent(SessionKey.from(ctx.session()), k -> new Object()));

        var adkRunner = new InMemoryRunner(agent);
        var session = adkRunner.sessionService()
                .createSession(adkRunner.appName(), "user-1", (Map<String, Object>) null, "sess-1")
                .blockingGet();

        var first = lastAgentText(adkRunner.runAsync(
                session.userId(), session.id(),
                userMessage("msg 1"), RunConfig.builder().build()).toList().blockingGet(),
                "long_lived");
        var second = lastAgentText(adkRunner.runAsync(
                session.userId(), session.id(),
                userMessage("msg 2"), RunConfig.builder().build()).toList().blockingGet(),
                "long_lived");
        var third = lastAgentText(adkRunner.runAsync(
                session.userId(), session.id(),
                userMessage("msg 3"), RunConfig.builder().build()).toList().blockingGet(),
                "long_lived");

        assertThat(first).isEqualTo("first");
        assertThat(second).isEqualTo("second");
        assertThat(third).isEqualTo("third");
        // Three invocations, one registered runner — long-lived net was reused.
        assertThat(registry.size()).isEqualTo(1);
    }

    @Test
    void run_live_bridges_to_runner_event_stream() throws Exception {
        // ADK_FINDINGS.md #3: runLiveImpl previously threw
        // UnsupportedOperationException, forcing voice consumers to
        // bypass PetriAgent. After the fix it returns the runner's hot
        // adkEvents() stream, so Runner.runLive(...) works end-to-end —
        // the input half is the caller's responsibility (forward
        // LiveRequestQueue frames to runner.inject), and we exercise
        // that pattern below.
        var llm = scriptedLlm(textResponse("live answer"));

        var registry = new SessionExecutorRegistry();
        var sessionOwners = sessionOwnerMap();
        var agent = PetriAgent.of(
                "live_agent",
                "BIDI bridge test",
                registry,
                key -> petriRunner(llm, "live_agent"),
                ctx -> sessionOwners.computeIfAbsent(SessionKey.from(ctx.session()), k -> new Object()));

        var adkRunner = new InMemoryRunner(agent);
        var session = adkRunner.sessionService()
                .createSession(adkRunner.appName(), "live-user", (Map<String, Object>) null, "live-sess")
                .blockingGet();

        // Subscribe to the live event stream BEFORE injecting input —
        // the BIDI Flowable is hot, late subscribers miss earlier events.
        var liveQueue = new LiveRequestQueue();
        TestSubscriber<Event> sub = adkRunner.runLive(session, liveQueue, RunConfig.builder().build())
                .take(1)
                .test();

        // Caller-side input wiring: forward content into the live runner.
        // The InvocationContext path inside ADK creates/resolves the
        // PetriRunner; once it's in the registry we can inject directly.
        // We wait briefly for runLiveImpl to populate the registry.
        var sessionKey = SessionKey.from(session);
        for (int i = 0; i < 50 && registry.get(sessionKey) == null; i++) {
            Thread.sleep(20);
        }
        var runner = registry.get(sessionKey);
        assertThat(runner).isNotNull();
        runner.inject(AdkColours.USER_IN, userMessage("hi live"))
                .get(2, TimeUnit.SECONDS);

        sub.awaitDone(3, TimeUnit.SECONDS);
        sub.assertValueCount(1);
        sub.assertValue(e -> "live answer".equals(e.content().get().text()));

        liveQueue.close();
    }

    @Test
    void different_sessions_get_isolated_runners() {
        var llm = scriptedLlm(textResponse("from a"), textResponse("from b"));

        var registry = new SessionExecutorRegistry();
        var sessionOwners = sessionOwnerMap();
        var agent = PetriAgent.of(
                "iso_agent",
                "isolated per session",
                registry,
                key -> petriRunner(llm, "iso_agent"),
                ctx -> sessionOwners.computeIfAbsent(SessionKey.from(ctx.session()), k -> new Object()));

        var adkRunner = new InMemoryRunner(agent);

        var s1 = adkRunner.sessionService()
                .createSession(adkRunner.appName(), "u-a", (Map<String, Object>) null, "sess-a").blockingGet();
        var s2 = adkRunner.sessionService()
                .createSession(adkRunner.appName(), "u-b", (Map<String, Object>) null, "sess-b").blockingGet();

        adkRunner.runAsync(s1.userId(), s1.id(),
                userMessage("hello"), RunConfig.builder().build()).blockingForEach(e -> {});
        adkRunner.runAsync(s2.userId(), s2.id(),
                userMessage("hello"), RunConfig.builder().build()).blockingForEach(e -> {});

        assertThat(registry.size()).isEqualTo(2);
        assertThat(registry.get(SessionKey.from(s1))).isNotNull();
        assertThat(registry.get(SessionKey.from(s2))).isNotNull();
    }

    // ============================================================
    //  Fixtures
    // ============================================================

    /**
     * Stable per-session owner identity. ADK's {@code InMemorySessionService}
     * returns defensive copies on {@code getSession}, so {@code ctx.session()}
     * is a fresh object every invocation — we need our own stable map keyed
     * by {@link SessionKey} to provide the lifetime owner. The map's
     * lifetime equals the test method's lifetime; when the test ends, the
     * owners become unreachable and the Cleaner tears the runners down.
     */
    private static ConcurrentMap<SessionKey, Object> sessionOwnerMap() {
        return new ConcurrentHashMap<>();
    }

    private static PetriRunner petriRunner(BaseLlm llm, String agentName) {
        var config = LlmAgentSubnet.Config.builder(agentName, "fake-model")
                .dispatchExecutor(EXECUTOR)
                .build();
        var net = PetriNet.builder("agent-net")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));
        return PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .actionExecutor(EXECUTOR)
                .orchestratorExecutor(EXECUTOR)
                .start();
    }

    private static String lastAgentText(List<Event> events, String author) {
        return events.stream()
                .filter(e -> author.equals(e.author()))
                .reduce((first, second) -> second)
                .orElseThrow(() -> new AssertionError("no event from author '" + author + "'"))
                .content().get().text();
    }

    private static Content userMessage(String text) {
        return Content.builder().role("user").parts(List.of(Part.fromText(text))).build();
    }

    private static LlmResponse textResponse(String text) {
        return LlmResponse.builder()
                .content(Content.builder().role("model").parts(List.of(Part.fromText(text))).build())
                .build();
    }

    private static BaseLlm scriptedLlm(LlmResponse... responses) {
        Deque<LlmResponse> queue = new ArrayDeque<>(List.of(responses));
        return new BaseLlm("scripted") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                var next = queue.poll();
                if (next == null) return Flowable.error(new IllegalStateException("scriptedLlm exhausted"));
                return Flowable.just(next);
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
    }
}
