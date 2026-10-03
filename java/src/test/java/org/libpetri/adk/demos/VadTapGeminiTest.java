package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.models.GeminiLiveTransport;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.Client;
import com.google.genai.types.Content;
import com.google.genai.types.LiveConnectConfig;
import com.google.genai.types.LiveSendClientContentParameters;
import com.google.genai.types.LiveSendRealtimeInputParameters;
import com.google.genai.types.LiveSendToolResponseParameters;
import com.google.genai.types.LiveServerContent;
import com.google.genai.types.LiveServerMessage;
import com.google.genai.types.Part;
import com.google.genai.types.VoiceActivity;
import com.google.genai.types.VoiceActivityType;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.demos.SyncGeminiLiveConnection.VoiceSignal;

/**
 * Drives ADK's real {@code GeminiLlmConnection} over an in-process transport
 * (ADK 1.9's seam), with no network: the tap surfaces the voice-activity
 * edges ADK drops, and ADK's own response stream is unchanged.
 */
class VadTapGeminiTest {

    @Test
    void voice_activity_edges_reach_the_tap_while_adk_still_streams_the_model_turn() {
        var signals = new CopyOnWriteArrayList<VoiceSignal>();

        var texts = streamTexts(signals::add);

        assertThat(signals).containsExactly(
                VoiceSignal.SPEECH_STARTED, VoiceSignal.SPEECH_STOPPED, VoiceSignal.TURN_COMPLETE)
                .inOrder();
        // ADK's own mapping is untouched: the model turn still arrives as text.
        assertThat(texts).contains("hello");
    }

    /**
     * {@code onSignal} runs inside ADK's receive callback. A throw from it is
     * contained: later signals still arrive and ADK still streams the turn.
     */
    @Test
    void a_throwing_signal_handler_does_not_end_the_live_stream() {
        var signals = new CopyOnWriteArrayList<VoiceSignal>();

        var texts = streamTexts(signal -> {
            signals.add(signal);
            if (signal == VoiceSignal.SPEECH_STARTED) {
                throw new IllegalStateException("handler bug");
            }
        });

        assertThat(signals).containsExactly(
                VoiceSignal.SPEECH_STARTED, VoiceSignal.SPEECH_STOPPED, VoiceSignal.TURN_COMPLETE)
                .inOrder();
        assertThat(texts).contains("hello");
    }

    /** Runs the scripted frames through ADK's real connection, returning the texts it streamed. */
    private static List<String> streamTexts(Consumer<VoiceSignal> onSignal) {
        var frames = List.of(
                vadEdge(VoiceActivityType.Known.ACTIVITY_START),
                LiveServerMessage.builder().serverContent(LiveServerContent.builder()
                        .modelTurn(Content.builder().role("model")
                                .parts(List.of(Part.fromText("hello"))).build())
                        .build()).build(),
                vadEdge(VoiceActivityType.Known.ACTIVITY_END),
                LiveServerMessage.builder().serverContent(
                        LiveServerContent.builder().turnComplete(true).build()).build());
        try (var client = Client.builder().apiKey("offline-test-key").build()) {
            var llm = new VadTapGemini("gemini-live-test", client,
                    (model, config) -> CompletableFuture.completedFuture(new ScriptedTransport(frames)),
                    onSignal);
            var connection = llm.connect(LlmRequest.builder()
                    .model("gemini-live-test")
                    .liveConnectConfig(LiveConnectConfig.builder().build())
                    .build());
            try {
                var responses = connection.receive().test();
                responses.awaitDone(2, TimeUnit.SECONDS);
                responses.assertNoErrors();
                return responses.values().stream()
                        .map(LlmResponse::content)
                        .flatMap(java.util.Optional::stream)
                        .map(Content::text)
                        .toList();
            } finally {
                connection.close();
            }
        }
    }

    private static LiveServerMessage vadEdge(VoiceActivityType.Known type) {
        return LiveServerMessage.builder()
                .voiceActivity(VoiceActivity.builder().voiceActivityType(type).build())
                .build();
    }

    /** Replays fixed frames on receive, then ends the stream. */
    private record ScriptedTransport(List<LiveServerMessage> frames) implements GeminiLiveTransport {
        @Override
        public CompletableFuture<Void> receive(Consumer<LiveServerMessage> onMessage, Runnable onStreamEnd) {
            frames.forEach(onMessage);
            onStreamEnd.run();
            return CompletableFuture.completedFuture(null);
        }

        @Override public CompletableFuture<Void> sendClientContent(LiveSendClientContentParameters p) {
            return CompletableFuture.completedFuture(null);
        }
        @Override public CompletableFuture<Void> sendRealtimeInput(LiveSendRealtimeInputParameters p) {
            return CompletableFuture.completedFuture(null);
        }
        @Override public CompletableFuture<Void> sendToolResponse(LiveSendToolResponseParameters p) {
            return CompletableFuture.completedFuture(null);
        }
        @Override public CompletableFuture<Void> close() {
            return CompletableFuture.completedFuture(null);
        }
    }
}
