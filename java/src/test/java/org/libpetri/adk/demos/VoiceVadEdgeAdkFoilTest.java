package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.models.GeminiLlmConnection;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.LiveServerMessage;
import com.google.genai.types.VoiceActivity;
import com.google.genai.types.VoiceActivityType;
import io.reactivex.rxjava3.core.Observable;
import java.lang.reflect.Method;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.demos.SyncGeminiLiveConnection.VoiceSignal;

/**
 * ADK-only foil for the voice/BIDI domain (README design commitment #4). ADK's
 * stock live-receive path drops the server-side VAD speech-activity edges, so a
 * barge-in frontend built on {@code BaseLlmConnection.receive()} never sees
 * speech start/stop. The Petri path recovers them through
 * {@link org.libpetri.adk.runner.LiveConnection#rawReceive()} plus
 * {@link SyncGeminiLiveConnection#voiceSignals(LiveServerMessage)}.
 *
 * <p>This runs ADK's <b>actual</b> mapping
 * ({@code GeminiLlmConnection.convertToServerResponse}, the package-private
 * static that {@code receive()} delegates to), not a strawman reimplementation.
 * The observed behavior is in fact stronger than "dropped": ADK classifies a
 * VAD-only frame as an <i>"Unknown server message"</i> error
 * ({@code errorCode}/{@code errorMessage} set, no content, not
 * {@code interrupted}, no {@code turnComplete}). A barge-in frontend on
 * {@code receive()} therefore gets an error, never a speech-start. This
 * green-locks that behavior: if a future ADK release starts carrying VAD edges
 * in {@code LlmResponse}, these assertions flip red and design commitment #4
 * gets revisited.
 *
 * <p>Scope note: barge-in ({@code interrupted}) and {@code turnComplete} <i>are</i>
 * carried by ADK's {@code LlmResponse}, so the precise, honest claim is narrow:
 * the two VAD <i>speech-activity</i> edges (start/stop) have no channel in the
 * type {@code receive()} emits. That is exactly what the exemplar's javadoc says.
 */
class VoiceVadEdgeAdkFoilTest {

    @Test
    void adk_receive_mapping_drops_the_vad_speech_start_edge() throws Exception {
        LiveServerMessage speechStart = vadEdge(VoiceActivityType.Known.ACTIVITY_START);

        // Petri path: the edge is recovered as a typed signal.
        assertThat(SyncGeminiLiveConnection.voiceSignals(speechStart))
                .containsExactly(VoiceSignal.SPEECH_STARTED);

        // ADK path: its own conversion carries none of the edge's meaning. The
        // frame surfaces as an "unknown server message" error, never a speech-start.
        assertEdgeLost(adkConvert(speechStart));
    }

    @Test
    void adk_receive_mapping_drops_the_vad_speech_stop_edge() throws Exception {
        LiveServerMessage speechStop = vadEdge(VoiceActivityType.Known.ACTIVITY_END);

        assertThat(SyncGeminiLiveConnection.voiceSignals(speechStop))
                .containsExactly(VoiceSignal.SPEECH_STOPPED);
        assertEdgeLost(adkConvert(speechStop));
    }

    /**
     * Green-locks the observed ADK behavior: the VAD frame yields response(s)
     * that carry no speech-activity meaning (no content, not interrupted, no
     * turnComplete) and instead surface as an error.
     */
    private static void assertEdgeLost(List<LlmResponse> responses) {
        assertThat(responses).isNotEmpty();
        for (LlmResponse r : responses) {
            assertThat(r.content()).isEmpty();
            assertThat(r.interrupted()).isEmpty();
            assertThat(r.turnComplete()).isEmpty();
            assertThat(r.errorMessage()).isPresent();
        }
    }

    @Test
    void the_vad_edge_lives_on_voiceActivity_a_sibling_of_serverContent() {
        // The structural reason ADK cannot carry it: the edge rides
        // voiceActivity(), not serverContent(), and LlmResponse is built only
        // from serverContent-derived fields.
        LiveServerMessage speechStart = vadEdge(VoiceActivityType.Known.ACTIVITY_START);
        assertThat(speechStart.voiceActivity()).isPresent();
        assertThat(speechStart.serverContent()).isEmpty();
    }

    /** ADK's real LiveServerMessage -> LlmResponse mapping, reached via reflection. */
    @SuppressWarnings("unchecked")
    private static List<LlmResponse> adkConvert(LiveServerMessage msg) throws Exception {
        Method m = GeminiLlmConnection.class
                .getDeclaredMethod("convertToServerResponse", LiveServerMessage.class);
        m.setAccessible(true);
        Observable<LlmResponse> out = (Observable<LlmResponse>) m.invoke(null, msg);
        return out.toList().blockingGet();
    }

    private static LiveServerMessage vadEdge(VoiceActivityType.Known type) {
        return LiveServerMessage.builder()
                .voiceActivity(VoiceActivity.builder().voiceActivityType(type).build())
                .build();
    }
}
