package org.libpetri.adk.demos;

import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.GeminiUtil;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.Client;
import com.google.genai.ResponseStream;
import com.google.genai.types.GenerateContentConfig;
import com.google.genai.types.GenerateContentResponse;
import io.reactivex.rxjava3.core.Flowable;
import java.util.Objects;

/**
 * Example {@link BaseLlm} that calls the genai SDK's <b>synchronous</b> API so
 * that the entire model call — HTTP I/O, JSON parsing, response mapping — runs
 * on the <i>subscribing</i> thread. In adk-libpetri that is the Petri
 * transition action's thread (a virtual thread, by the
 * {@code PetriRunner.Builder.actionExecutor} convention).
 *
 * <h2>Why this exists</h2>
 *
 * <p>The default async path schedules work on {@link
 * java.util.concurrent.ForkJoinPool#commonPool()} — the JVM-global shared
 * executor singleton this project forbids (executor lifetime is
 * caller-owned; see the README design commitments). The commonPool
 * usage sits in <i>two</i> layers:
 *
 * <ul>
 *   <li>genai's async client (<code>client.async.*</code>) chains
 *       <code>thenApplyAsync(fn)</code> / <code>supplyAsync(fn)</code> with no
 *       executor; and</li>
 *   <li>ADK's own {@code Gemini.generateContent} adds a further
 *       <code>...generateContent(...).thenApplyAsync(LlmResponse::create)</code>.</li>
 * </ul>
 *
 * <p>genai's commonPool use is only on the <i>async</i> client. The
 * <b>sync</b> facade (<code>client.models.*</code>) runs entirely on the
 * calling thread, and OkHttp's synchronous {@code execute()} likewise runs on
 * the calling thread (its Dispatcher pool is used only by {@code enqueue}). So
 * calling genai synchronously on a virtual thread bypasses <i>both</i>
 * commonPool hops with no fork of genai or ADK (principles 4, 5, 7).
 *
 * <h2>Usage</h2>
 *
 * <pre>{@code
 * Client client = Client.builder().apiKey(key).build();   // build once, share, close on shutdown
 * BaseLlm llm  = new SyncGeminiLlm("gemini-2.0-flash", client);
 * net.bindActions(LlmStepSubnet.actionBindings(llm));
 * // run the net with a virtual-thread actionExecutor so the blocking call is cheap
 * }</pre>
 *
 * <p>This is an exemplar (stock subnets are templates; you own the call
 * site), not shipped library code — it is a thin adapter over ADK's public mappers
 * ({@link GeminiUtil}, {@link LlmResponse#create}) and genai's sync
 * {@code client.models}.
 *
 * <p>Live/BIDI ({@link #connect}) is intentionally unsupported: it is
 * inherently async/full-duplex and enters the net via env-place injection,
 * not through this adapter.
 */
public final class SyncGeminiLlm extends BaseLlm {

    private final Client client;

    public SyncGeminiLlm(String modelName, Client client) {
        super(modelName);
        this.client = Objects.requireNonNull(client, "client");
    }

    @Override
    public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
        // Same request preparation ADK's Gemini does — reused, not reimplemented.
        LlmRequest prepared = GeminiUtil.prepareGenenerateContentRequest(
                request, /* sanitize= */ !client.vertexAI(), /* stripThoughts= */ false);
        GenerateContentConfig config = prepared.config().orElse(null);
        String effectiveModel = prepared.model().orElse(model());

        if (stream) {
            // genai sync streaming returns a pull-based Iterable; each chunk is
            // read on the subscribing thread (lazy SSE readLine), never commonPool.
            return Flowable.defer(() -> {
                ResponseStream<GenerateContentResponse> rs =
                        client.models.generateContentStream(effectiveModel, prepared.contents(), config);
                return Flowable.fromIterable(rs)
                        .map(LlmResponse::create)
                        .doFinally(rs::close);
            });
        }
        // fromCallable runs the SYNC genai call on the subscribing thread.
        return Flowable.fromCallable(
                () -> LlmResponse.create(
                        client.models.generateContent(effectiveModel, prepared.contents(), config)));
    }

    @Override
    public BaseLlmConnection connect(LlmRequest request) {
        throw new UnsupportedOperationException(
                "SyncGeminiLlm covers generateContent (unary + server-streaming). "
                        + "Live/BIDI uses the connect() path and env-place injection.");
    }
}
