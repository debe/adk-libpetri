package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.Client;
import com.google.genai.types.Content;
import com.google.genai.types.HttpOptions;
import com.google.genai.types.Part;
import com.sun.net.httpserver.HttpServer;
import io.reactivex.rxjava3.core.Flowable;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ForkJoinWorkerThread;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmStepSubnet;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.runtime.BitmapNetExecutor;

/**
 * Proves the design claim behind {@link SyncGeminiLlm}: a <b>synchronous</b>
 * {@link BaseLlm} drives the entire model call on the caller's action executor
 * thread and never touches {@link java.util.concurrent.ForkJoinPool#commonPool()}.
 *
 * <ul>
 *   <li><b>{@code synchronous_call_runs_on_action_thread_not_commonPool}</b> —
 *       the architectural guard, through the real {@link LlmStepSubnet} on a
 *       named single-thread executor.</li>
 *   <li><b>{@code sync_gemini_llm_runs_real_genai_call_on_caller_thread}</b> —
 *       the real {@link SyncGeminiLlm} against a localhost genai stub: response
 *       mapping happens on the caller's named thread, not commonPool.</li>
 *   <li><b>{@code connect_is_unsupported}</b> — live/BIDI is out of scope.</li>
 * </ul>
 */
class SyncGeminiLlmTest {

    // ============================================================
    //  Test A — architectural guard through the subnet
    // ============================================================

    @Test
    void synchronous_call_runs_on_action_thread_not_commonPool() throws Exception {
        var producedOn = new AtomicReference<Thread>();
        var response = textResponse("hi");

        var net = PetriNet.builder("sync-guard")
                .compose(LlmStepSubnet.DEF)
                .build()
                .bindActions(LlmStepSubnet.actionBindings(new SyncPatternLlm(response, producedOn)));

        Map<Place<?>, List<Token<?>>> initial =
                Map.of(AdkColours.LLM_REQUEST, List.of(Token.of(simpleRequest("q"))));

        // Drive the net on a caller-owned named thread (as PetriRunner drives it on a
        // caller-supplied executor). A synchronous BaseLlm must keep the model call on
        // this thread and never hop to ForkJoinPool.commonPool().
        String threadName = "adk-net-runner";
        ExecutorService netRunner = Executors.newSingleThreadExecutor(r -> new Thread(r, threadName));
        try {
            var finalMarking = netRunner.submit(() ->
                    BitmapNetExecutor.builder(net, initial).build().run()).get(15, TimeUnit.SECONDS);
            var responses = finalMarking.peekTokens(AdkColours.LLM_RESPONSE).stream()
                    .map(Token::value)
                    .toList();
            assertThat(responses).containsExactly(response);
        } finally {
            netRunner.shutdownNow();
        }

        assertThat(producedOn.get()).isNotNull();
        assertThat(producedOn.get().getName()).isEqualTo(threadName);
        assertThat(producedOn.get()).isNotInstanceOf(ForkJoinWorkerThread.class);
    }

    // ============================================================
    //  Test B — the real SyncGeminiLlm against a localhost stub
    // ============================================================

    private static final String CANNED_RESPONSE =
            """
            {
              "candidates": [
                {
                  "content": { "role": "model", "parts": [ { "text": "sync hello" } ] },
                  "finishReason": "STOP",
                  "index": 0
                }
              ],
              "usageMetadata": { "promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3 }
            }
            """;

    @Test
    void sync_gemini_llm_runs_real_genai_call_on_caller_thread() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            exchange.getRequestBody().readAllBytes(); // drain
            byte[] body = CANNED_RESPONSE.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, body.length);
            try (var os = exchange.getResponseBody()) {
                os.write(body);
            }
        });
        server.start();
        int port = server.getAddress().getPort();

        Client client = Client.builder()
                .apiKey("test-key")
                .httpOptions(HttpOptions.builder().baseUrl("http://127.0.0.1:" + port).build())
                .build();

        String threadName = "adk-genai-sync-test";
        ExecutorService caller = Executors.newSingleThreadExecutor(r -> new Thread(r, threadName));
        var mappedOn = new AtomicReference<Thread>();
        try {
            var llm = new SyncGeminiLlm("gemini-2.0-flash", client);
            LlmResponse resp = caller.submit(() ->
                    llm.generateContent(simpleRequest("hi"), /* stream */ false)
                            .doOnNext(r -> mappedOn.set(Thread.currentThread()))
                            .blockingFirst()).get(15, TimeUnit.SECONDS);

            assertThat(firstText(resp)).isEqualTo("sync hello");
            assertThat(mappedOn.get()).isNotNull();
            assertThat(mappedOn.get().getName()).isEqualTo(threadName);
            assertThat(mappedOn.get()).isNotInstanceOf(ForkJoinWorkerThread.class);
        } finally {
            caller.shutdownNow();
            client.close();
            server.stop(0);
        }
    }

    // ============================================================
    //  Test C — connect()/BIDI is out of scope
    // ============================================================

    @Test
    void connect_is_unsupported() {
        Client client = Client.builder().apiKey("test-key").build();
        try {
            var llm = new SyncGeminiLlm("gemini-2.0-flash", client);
            assertThrows(UnsupportedOperationException.class, () -> llm.connect(simpleRequest("q")));
        } finally {
            client.close();
        }
    }

    // ============================================================
    //  Helpers
    // ============================================================

    private static String firstText(LlmResponse resp) {
        return resp.content()
                .flatMap(Content::parts)
                .filter(parts -> !parts.isEmpty())
                .flatMap(parts -> parts.get(0).text())
                .orElseThrow(() -> new AssertionError("response had no text part: " + resp));
    }

    private static LlmRequest simpleRequest(String userText) {
        return LlmRequest.builder()
                .model("gemini-2.0-flash")
                .contents(List.of(Content.builder()
                        .role("user")
                        .parts(List.of(Part.fromText(userText)))
                        .build()))
                .build();
    }

    private static LlmResponse textResponse(String text) {
        return LlmResponse.builder()
                .content(Content.builder()
                        .role("model")
                        .parts(List.of(Part.fromText(text)))
                        .build())
                .build();
    }

    /** A {@link BaseLlm} that produces its response inside {@code Flowable.fromCallable} — the same
     *  synchronous-on-subscribe shape as {@link SyncGeminiLlm} — recording the producing thread. */
    private static final class SyncPatternLlm extends BaseLlm {
        private final LlmResponse response;
        private final AtomicReference<Thread> producedOn;

        SyncPatternLlm(LlmResponse response, AtomicReference<Thread> producedOn) {
            super("sync-pattern");
            this.response = response;
            this.producedOn = producedOn;
        }

        @Override
        public Flowable<LlmResponse> generateContent(LlmRequest req, boolean stream) {
            return Flowable.fromCallable(() -> {
                producedOn.set(Thread.currentThread());
                return response;
            });
        }

        @Override
        public BaseLlmConnection connect(LlmRequest req) {
            throw new UnsupportedOperationException("SyncPatternLlm.connect()");
        }
    }
}
