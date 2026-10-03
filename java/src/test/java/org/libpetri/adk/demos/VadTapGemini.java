package org.libpetri.adk.demos;

import com.google.adk.models.Gemini;
import com.google.adk.models.GeminiLiveTransport;
import com.google.genai.Client;
import com.google.genai.types.LiveConnectConfig;
import com.google.genai.types.LiveSendClientContentParameters;
import com.google.genai.types.LiveSendRealtimeInputParameters;
import com.google.genai.types.LiveSendToolResponseParameters;
import com.google.genai.types.LiveServerMessage;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.function.BiFunction;
import java.util.function.Consumer;
import org.libpetri.adk.demos.SyncGeminiLiveConnection.VoiceSignal;

/**
 * Exemplar: recovers the voice-activity edges ADK's live wrapper drops,
 * while keeping ADK's own {@code GeminiLlmConnection}. This is the preferred
 * no-fork path since ADK 1.9; {@link SyncGeminiLiveConnection} remains the
 * fallback when you want the whole live session in your own hands.
 *
 * <p>ADK 1.9 put a seam under its connection: {@link Gemini} opens a
 * {@link GeminiLiveTransport} through the protected
 * {@code connectLiveTransport}, and the connection reads raw
 * {@link LiveServerMessage}s from the transport's {@code receive}. This
 * subclass wraps that transport and tees every message through
 * {@link SyncGeminiLiveConnection#voiceSignals} before ADK sees it, so the
 * {@code voiceActivity} frames that {@code GeminiLlmConnection} still turns
 * into blank responses reach {@code onSignal}. Route them into the net the
 * usual way:
 *
 * <pre>{@code
 * var llm = new VadTapGemini("gemini-live-2.5-flash", client, signal -> {
 *     switch (signal) {
 *         case SPEECH_STARTED -> runner.signal(VadSubnet.Places.SPEECH_STARTED);
 *         case SPEECH_STOPPED -> runner.signal(VadSubnet.Places.SPEECH_STOPPED);
 *         default -> { }  // INTERRUPTED and TURN_COMPLETE also reach ADK
 *     }
 * });
 * }</pre>
 *
 * <p>Thin user code over ADK's public extension point; nothing is forked.
 */
public class VadTapGemini extends Gemini {

    private final Consumer<VoiceSignal> onSignal;
    private final BiFunction<String, LiveConnectConfig, CompletableFuture<GeminiLiveTransport>> opener;

    public VadTapGemini(String modelName, Client client, Consumer<VoiceSignal> onSignal) {
        this(modelName, client, null, onSignal);
    }

    /**
     * {@code opener} replaces the network connect, so a test can drive the
     * real {@code GeminiLlmConnection} offline; {@code null} keeps ADK's.
     */
    VadTapGemini(String modelName, Client client,
                 BiFunction<String, LiveConnectConfig, CompletableFuture<GeminiLiveTransport>> opener,
                 Consumer<VoiceSignal> onSignal) {
        super(modelName, client);
        this.opener = opener;
        this.onSignal = Objects.requireNonNull(onSignal, "onSignal");
    }

    @Override
    protected CompletableFuture<GeminiLiveTransport> connectLiveTransport(
            String modelName, LiveConnectConfig config) {
        var transport = opener != null
                ? opener.apply(modelName, config)
                : super.connectLiveTransport(modelName, config);
        return transport.thenApply(t -> new Tapped(t, onSignal));
    }

    /** Delegates everything; {@code receive} sees each message first. */
    private record Tapped(GeminiLiveTransport delegate, Consumer<VoiceSignal> onSignal)
            implements GeminiLiveTransport {

        @Override
        public CompletableFuture<Void> receive(Consumer<LiveServerMessage> onMessage, Runnable onStreamEnd) {
            return delegate.receive(message -> {
                SyncGeminiLiveConnection.voiceSignals(message).forEach(onSignal);
                onMessage.accept(message);
            }, onStreamEnd);
        }

        @Override
        public CompletableFuture<Void> sendClientContent(LiveSendClientContentParameters params) {
            return delegate.sendClientContent(params);
        }

        @Override
        public CompletableFuture<Void> sendRealtimeInput(LiveSendRealtimeInputParameters params) {
            return delegate.sendRealtimeInput(params);
        }

        @Override
        public CompletableFuture<Void> sendToolResponse(LiveSendToolResponseParameters params) {
            return delegate.sendToolResponse(params);
        }

        @Override
        public CompletableFuture<Void> close() {
            return delegate.close();
        }
    }
}
