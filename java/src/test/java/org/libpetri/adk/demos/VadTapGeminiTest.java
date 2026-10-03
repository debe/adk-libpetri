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
        var frames = List.of(
                vadEdge(VoiceActivityType.Known.ACTIVITY_START),
                LiveServerMessage.builder().serverContent(LiveServerContent.builder()
                        .modelTurn(Content.builder().role("model")
                                .parts(List.of(Part.fromText("hello"))).build())
                        .build()).build(),
                vadEdge(VoiceActivityType.Known.ACTIVITY_END),
                LiveServerMessage.builder().serverContent(
                        LiveServerContent.builder().turnComplete(true).build()).build());
        var signals = new CopyOnWriteArrayList<VoiceSignal>();
        var llm = new VadTapGemini("gemini-live-test",
                Client.builder().apiKey("offline-test-key").build(),
                (model, config) -> CompletableFuture.completedFuture(new ScriptedTransport(frames)),
                signals::add);

        var connection = llm.connect(LlmRequest.builder()
                .model("gemini-live-test")
                .liveConnectConfig(LiveConnectConfig.builder().build())
                .build());
        var responses = connection.receive().test();
        responses.awaitDone(2, TimeUnit.SECONDS);

        assertThat(signals).containsExactly(
                VoiceSignal.SPEECH_STARTED, VoiceSignal.SPEECH_STOPPED, VoiceSignal.TURN_COMPLETE)
                .inOrder();
        // ADK's own mapping is untouched: the model turn still arrives as text.
        assertThat(responses.values().stream()
                .map(LlmResponse::content)
                .flatMap(java.util.Optional::stream)
                .map(Content::text)
                .toList())
                .contains("hello");
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
