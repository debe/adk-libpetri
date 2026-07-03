package org.libpetri.adk.demos;

import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmResponse;
import com.google.genai.AsyncSession;
import com.google.genai.Client;
import com.google.genai.types.Blob;
import com.google.genai.types.Content;
import com.google.genai.types.LiveConnectConfig;
import com.google.genai.types.LiveSendClientContentParameters;
import com.google.genai.types.LiveSendRealtimeInputParameters;
import com.google.genai.types.LiveServerContent;
import com.google.genai.types.LiveServerMessage;
import com.google.genai.types.VoiceActivity;
import com.google.genai.types.VoiceActivityType;
import io.reactivex.rxjava3.core.Completable;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.processors.PublishProcessor;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.atomic.AtomicBoolean;
import org.libpetri.adk.runner.BidiPetriAgent;
import org.libpetri.adk.runner.LiveConnection;
import org.libpetri.adk.runner.PetriRunner;

/**
 * Example {@link LiveConnection} over genai's <b>Live</b> API
 * ({@code client.async.live}), the BIDI sibling of {@link SyncGeminiLlm}.
 *
 * <h2>Why this exists</h2>
 *
 * <p>The BIDI plumbing is shipped in {@code main}: {@link LiveConnection} (the
 * contract, {@link BaseLlmConnection} plus {@code rawReceive()}),
 * {@link BidiPetriAgent#bridge} (the bidirectional pump), and
 * {@link PetriRunner#signal(org.libpetri.core.Place)} (signal injection). The one
 * piece the library deliberately does <b>not</b> ship is this: the genai-specific
 * connection. Binding to {@code client.async.live} is tied to a genai SDK version
 * (down to the websocket close quirk), so the consumer owns it, the same way Marvin
 * owns its {@code MarvinLiveLlmConnection}. This class is the copy-and-adapt
 * reference: implement {@link LiveConnection} over genai's public Live API, with
 * <b>no</b> override of any {@code com.google.adk.*} class (no classpath shadow-fork
 * of {@code com.google.adk.models.Gemini} / {@code GeminiLlmConnection}, which would
 * break design commitment #4).
 *
 * <p>It carries the two genai-specific bits a {@link LiveConnection} needs:
 *
 * <ul>
 *   <li><b>Dropped voice-activity signals.</b> ADK's {@code GeminiLlmConnection}
 *       flattens each {@code LiveServerMessage} into an {@code LlmResponse} and
 *       <i>drops</i> the server-side VAD edges. They are public, typed genai API,
 *       so {@link #rawReceive()} exposes the unabstracted {@link LiveServerMessage}
 *       stream and {@link #voiceSignals(LiveServerMessage)} decodes the edges the
 *       net cares about (speech start/stop, barge-in interrupt, turn complete).</li>
 *   <li><b>Hardcoded {@code turnComplete=true}.</b> ADK's
 *       {@code BaseLlmConnection.sendContent} forces a turn boundary on every send,
 *       which breaks history replay and streaming tool responses.
 *       {@link #sendClientContent(Content, boolean)} restores explicit control.</li>
 * </ul>
 *
 * <h2>How the decoded edges reach the net (env-place injection only)</h2>
 *
 * <p>The decoded {@link VoiceSignal}s enter the running net the same way every
 * external signal does, through their own typed env place (design commitment #1)
 * via {@link PetriRunner#signal(org.libpetri.core.Place)}. The bidirectional pump
 * loop (queue to connection, connection to net, event merge, dispose) is owned by
 * {@link BidiPetriAgent#bridge}; this class supplies only the connection and the
 * per-message decode:
 *
 * <pre>{@code
 * LiveConnection conn = new SyncGeminiLiveConnection(client, "gemini-2.0-flash-live-001", liveConfig);
 * Flowable<Event> out = BidiPetriAgent.bridge(ctx.liveRequestQueue(), conn, runner, "voice_agent",
 *     (msg, r) -> {
 *         for (VoiceSignal s : SyncGeminiLiveConnection.voiceSignals(msg)) {
 *             switch (s) {
 *                 case SPEECH_STARTED -> r.signal(VadSubnet.Places.SPEECH_STARTED);
 *                 case SPEECH_STOPPED -> r.signal(VadSubnet.Places.SPEECH_STOPPED);
 *                 case INTERRUPTED    -> r.signal(BargeInSubnet.Places.INTERRUPTED);
 *                 case TURN_COMPLETE  -> drainStreamingBudget(r); // your call-site choice
 *             }
 *         }
 *     });
 * }</pre>
 *
 * <p>The window {@code VadSubnet} opens from {@code SPEECH_STARTED} is the exact
 * place {@code BargeInSubnet} reads, so a later {@code INTERRUPTED} routes to
 * barge-in structurally (see {@code SyncGeminiLiveConnectionTest}).
 *
 * <h2>Caveat: non-daemon WebSocket threads on shutdown</h2>
 *
 * <p>{@link #close()} issues genai's async close. Some genai SDK builds run the
 * underlying WebSocket on <i>non-daemon</i> threads that can keep the JVM alive
 * after your shutdown hook returns; a production consumer may need to reach the
 * underlying client and {@code closeBlocking()} it (bounded on a virtual thread).
 * That workaround is SDK-version-specific and deliberately left out of this
 * exemplar to keep the wire path readable.
 *
 * <p>This is an <b>exemplar</b> (stock subnets are templates; you own the call
 * site, README design commitment #7), not shipped library code. The Live-API
 * edge shape is transport-specific, so you copy and adapt it; what it documents
 * is the no-fork connection seam, identical across transports.
 */
public final class SyncGeminiLiveConnection implements LiveConnection {

    /**
     * The voice edges the net cares about, decoded from a {@link LiveServerMessage}.
     * Transport-agnostic so the call-site routing ({@code SPEECH_STARTED} →
     * {@code VadSubnet.Places.SPEECH_STARTED}, etc.) is a plain {@code switch}.
     */
    public enum VoiceSignal { SPEECH_STARTED, SPEECH_STOPPED, INTERRUPTED, TURN_COMPLETE }

    private final CompletableFuture<AsyncSession> sessionFuture;
    private final PublishProcessor<LiveServerMessage> raw = PublishProcessor.create();
    private final Flowable<LiveServerMessage> rawFlowable = raw.serialize();
    private final AtomicBoolean closed = new AtomicBoolean(false);

    public SyncGeminiLiveConnection(Client client, String model, LiveConnectConfig config) {
        Objects.requireNonNull(client, "client");
        Objects.requireNonNull(model, "model");
        Objects.requireNonNull(config, "config");
        // client.async and .live are public final fields; connect returns
        // CompletableFuture<AsyncSession>. The receive bridge is wired once the
        // session resolves, never on commonPool, since we only attach callbacks.
        this.sessionFuture = client.async.live.connect(model, config)
                .whenComplete((session, err) -> {
                    if (err != null) {
                        onConnectError(err);
                    } else if (session != null) {
                        setupReceiver(session);
                    }
                });
    }

    // ---- decode: the signals ADK's LlmResponse drops --------------------------

    /**
     * Pure decode of a single {@link LiveServerMessage} into the {@link VoiceSignal}s
     * the net injects. A message may carry more than one (e.g. a turn-complete
     * alongside an interrupt), so this returns a list. Empty when the message
     * carries no edge the net models (setup-complete, usage metadata, plain audio).
     */
    public static List<VoiceSignal> voiceSignals(LiveServerMessage msg) {
        Objects.requireNonNull(msg, "msg");
        var signals = new ArrayList<VoiceSignal>(2);
        // Server-side VAD edge. VoiceActivityType is an open-enum wrapper, so we
        // unwrap to .knownEnum() before comparing.
        msg.voiceActivity()
                .flatMap(VoiceActivity::voiceActivityType)
                .map(VoiceActivityType::knownEnum)
                .ifPresent(known -> {
                    if (known == VoiceActivityType.Known.ACTIVITY_START) {
                        signals.add(VoiceSignal.SPEECH_STARTED);
                    } else if (known == VoiceActivityType.Known.ACTIVITY_END) {
                        signals.add(VoiceSignal.SPEECH_STOPPED);
                    }
                });
        msg.serverContent().ifPresent(sc -> {
            sc.interrupted().filter(Boolean::booleanValue)
                    .ifPresent(i -> signals.add(VoiceSignal.INTERRUPTED));
            sc.turnComplete().filter(Boolean::booleanValue)
                    .ifPresent(t -> signals.add(VoiceSignal.TURN_COMPLETE));
        });
        return signals;
    }

    /** Raw server stream: carries {@code voiceActivity}, {@code interrupted}, etc. */
    @Override
    public Flowable<LiveServerMessage> rawReceive() {
        return rawFlowable;
    }

    // ---- BaseLlmConnection contract -------------------------------------------

    /**
     * Send client content with explicit {@code turnComplete} control, the
     * semantics ADK's {@link #sendContent(Content)} loses by hardcoding
     * {@code turnComplete=true}.
     */
    public Completable sendClientContent(Content content, boolean turnComplete) {
        Objects.requireNonNull(content, "content");
        return Completable.fromFuture(sessionFuture.thenCompose(session ->
                session.sendClientContent(LiveSendClientContentParameters.builder()
                        .turns(List.of(content))
                        .turnComplete(turnComplete)
                        .build())));
    }

    @Override
    public Completable sendHistory(List<Content> history) {
        Objects.requireNonNull(history, "history");
        return Completable.fromFuture(sessionFuture.thenCompose(session ->
                session.sendClientContent(LiveSendClientContentParameters.builder()
                        .turns(history)
                        .build())));
    }

    @Override
    public Completable sendContent(Content content) {
        return sendClientContent(content, true);
    }

    @Override
    public Completable sendRealtime(Blob blob) {
        Objects.requireNonNull(blob, "blob");
        return Completable.fromFuture(sessionFuture.thenCompose(session ->
                session.sendRealtimeInput(LiveSendRealtimeInputParameters.builder()
                        .media(blob)
                        .build())));
    }

    /**
     * ADK-shaped egress: {@code LiveServerContent} mapped to {@link LlmResponse}
     * (content + turnComplete + interrupted). Loses the VAD edges by design, those
     * ride {@link #rawReceive()}. Messages with no content payload are dropped.
     */
    @Override
    public Flowable<LlmResponse> receive() {
        return rawFlowable
                .map(SyncGeminiLiveConnection::toLlmResponse)
                .filter(Optional::isPresent)
                .map(Optional::get);
    }

    private static Optional<LlmResponse> toLlmResponse(LiveServerMessage msg) {
        Optional<LiveServerContent> maybe = msg.serverContent();
        if (maybe.isEmpty()) {
            return Optional.empty();
        }
        LiveServerContent sc = maybe.get();
        if (sc.modelTurn().isEmpty() && sc.turnComplete().isEmpty() && sc.interrupted().isEmpty()) {
            return Optional.empty();
        }
        var builder = LlmResponse.builder();
        sc.modelTurn().ifPresent(builder::content);
        sc.turnComplete().ifPresent(builder::turnComplete);
        sc.interrupted().ifPresent(builder::interrupted);
        return Optional.of(builder.build());
    }

    @Override
    public void close() {
        closeInternal(null);
    }

    @Override
    public void close(Throwable throwable) {
        Objects.requireNonNull(throwable, "throwable");
        closeInternal(throwable);
    }

    private void closeInternal(Throwable throwable) {
        if (closed.compareAndSet(false, true)) {
            if (throwable == null) {
                raw.onComplete();
            } else {
                raw.onError(throwable);
            }
            sessionFuture.thenAccept(session -> {
                if (session != null) {
                    session.close();
                }
            }).exceptionally(ignored -> null);
        }
    }

    // ---- receive wiring -------------------------------------------------------

    private void setupReceiver(AsyncSession session) {
        if (closed.get()) {
            session.close();
            return;
        }
        session.receive(this::onMessage).exceptionally(error -> {
            onConnectError(error);
            return null;
        });
    }

    private void onMessage(LiveServerMessage message) {
        if (!closed.get()) {
            raw.onNext(message);
        }
    }

    private void onConnectError(Throwable throwable) {
        if (closed.compareAndSet(false, true)) {
            raw.onError(throwable instanceof CompletionException ce ? ce.getCause() : throwable);
        }
    }
}
