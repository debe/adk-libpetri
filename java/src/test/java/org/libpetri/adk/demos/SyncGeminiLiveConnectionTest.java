package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import com.google.genai.types.LiveServerContent;
import com.google.genai.types.LiveServerMessage;
import com.google.genai.types.VoiceActivity;
import com.google.genai.types.VoiceActivityType;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.demos.SyncGeminiLiveConnection.VoiceSignal;
import org.libpetri.adk.demos.voice.BargeInSubnet;
import org.libpetri.adk.demos.voice.VadSubnet;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.core.TransitionAction;
import org.libpetri.event.EventStore;
import org.libpetri.runtime.BitmapNetExecutor;
import org.libpetri.runtime.Marking;

/**
 * Proves the live-connection exemplar's value without a live WebSocket: the
 * server-side signals ADK's {@code LlmResponse} drops are decoded into typed
 * {@link VoiceSignal}s ({@link #vad_activity_start_decodes_to_speech_started}
 * etc.), and those signals, injected as env-place tokens, drive the stock
 * {@code VadSubnet}/{@code BargeInSubnet} exactly as a real voice frontend would
 * ({@link #decoded_interrupt_while_window_open_routes_to_barge_in}).
 *
 * <p>The connection's wire path ({@code client.async.live.connect(...)},
 * send/close) needs a real genai {@code Client} and is exercised by the consumer,
 * not here, same boundary {@code SyncGeminiLlmTest} draws for the unary path.
 */
class SyncGeminiLiveConnectionTest {

    // ============================================================
    //  Decode: the signals ADK drops, surfaced as distinct events
    // ============================================================

    @Test
    void vad_activity_start_decodes_to_speech_started() {
        assertThat(SyncGeminiLiveConnection.voiceSignals(vadEdge(VoiceActivityType.Known.ACTIVITY_START)))
                .containsExactly(VoiceSignal.SPEECH_STARTED);
    }

    @Test
    void vad_activity_end_decodes_to_speech_stopped() {
        assertThat(SyncGeminiLiveConnection.voiceSignals(vadEdge(VoiceActivityType.Known.ACTIVITY_END)))
                .containsExactly(VoiceSignal.SPEECH_STOPPED);
    }

    @Test
    void interrupted_content_decodes_to_interrupted() {
        assertThat(SyncGeminiLiveConnection.voiceSignals(
                serverContent(LiveServerContent.builder().interrupted(true).build())))
                .containsExactly(VoiceSignal.INTERRUPTED);
    }

    @Test
    void turn_complete_content_decodes_to_turn_complete() {
        assertThat(SyncGeminiLiveConnection.voiceSignals(
                serverContent(LiveServerContent.builder().turnComplete(true).build())))
                .containsExactly(VoiceSignal.TURN_COMPLETE);
    }

    @Test
    void interrupt_and_turn_complete_in_one_message_decode_to_both() {
        assertThat(SyncGeminiLiveConnection.voiceSignals(serverContent(
                LiveServerContent.builder().interrupted(true).turnComplete(true).build())))
                .containsExactly(VoiceSignal.INTERRUPTED, VoiceSignal.TURN_COMPLETE);
    }

    @Test
    void message_with_no_modeled_edge_decodes_to_empty() {
        // A setup-complete / usage-metadata / plain-audio frame carries no edge
        // the net models, so it must not spuriously inject anything.
        assertThat(SyncGeminiLiveConnection.voiceSignals(LiveServerMessage.builder().build()))
                .isEmpty();
    }

    @Test
    void unspecified_vad_type_decodes_to_empty() {
        assertThat(SyncGeminiLiveConnection.voiceSignals(vadEdge(VoiceActivityType.Known.TYPE_UNSPECIFIED)))
                .isEmpty();
    }

    // ============================================================
    //  Integration: decoded signals reach the Vad/BargeIn subnets
    // ============================================================

    @Test
    void decoded_speech_start_opens_the_vad_window() {
        var signals = SyncGeminiLiveConnection.voiceSignals(vadEdge(VoiceActivityType.Known.ACTIVITY_START));
        assertThat(signals).containsExactly(VoiceSignal.SPEECH_STARTED);

        Marking m = run(vadNet(), seedFor(signals));

        assertThat(m.peekTokens(VadSubnet.VOICE_ACTIVITY_OPEN)).hasSize(1);
    }

    @Test
    void decoded_interrupt_while_window_open_routes_to_barge_in() {
        var net = PetriNet.builder("vad+bargein")
                .compose(VadSubnet.DEF)
                .compose(BargeInSubnet.DEF)
                .build()
                .bindActions(merge(VadSubnet.actionBindings(), BargeInSubnet.actionBindings()));

        // Phase 1: a decoded ACTIVITY_START opens the window via VadSubnet.
        Marking afterSpeech = run(net,
                seedFor(SyncGeminiLiveConnection.voiceSignals(vadEdge(VoiceActivityType.Known.ACTIVITY_START))));
        assertThat(afterSpeech.peekTokens(VadSubnet.VOICE_ACTIVITY_OPEN)).hasSize(1);

        // Phase 2: carry the open window forward, then a decoded interrupt.
        var phase2 = new LinkedHashMap<Place<?>, List<Token<?>>>();
        phase2.put(VadSubnet.VOICE_ACTIVITY_OPEN,
                new ArrayList<>(afterSpeech.peekTokens(VadSubnet.VOICE_ACTIVITY_OPEN)));
        phase2.putAll(seedFor(SyncGeminiLiveConnection.voiceSignals(
                serverContent(LiveServerContent.builder().interrupted(true).build()))));

        Marking afterInterrupt = run(net, phase2);
        assertThat(afterInterrupt.peekTokens(BargeInSubnet.Places.BARGE_IN_SENT)).hasSize(1);
        assertThat(afterInterrupt.peekTokens(BargeInSubnet.Places.INTERRUPT_DISCARDED)).isEmpty();
    }

    // ============================================================
    //  Fixture
    // ============================================================

    /** Maps decoded signals to their env place, the call-site routing the exemplar documents. */
    private static Place<Void> placeFor(VoiceSignal signal) {
        return switch (signal) {
            case SPEECH_STARTED -> VadSubnet.Places.SPEECH_STARTED;
            case SPEECH_STOPPED -> VadSubnet.Places.SPEECH_STOPPED;
            case INTERRUPTED -> BargeInSubnet.Places.INTERRUPTED;
            case TURN_COMPLETE -> null; // no place in this fixture; budget-drain in real wiring
        };
    }

    private static Map<Place<?>, List<Token<?>>> seedFor(List<VoiceSignal> signals) {
        var initial = new LinkedHashMap<Place<?>, List<Token<?>>>();
        for (VoiceSignal s : signals) {
            Place<Void> place = placeFor(s);
            if (place != null) {
                initial.computeIfAbsent(place, k -> new ArrayList<>()).add(Token.of((Void) null));
            }
        }
        return initial;
    }

    private static LiveServerMessage vadEdge(VoiceActivityType.Known type) {
        return LiveServerMessage.builder()
                .voiceActivity(VoiceActivity.builder().voiceActivityType(type).build())
                .build();
    }

    private static LiveServerMessage serverContent(LiveServerContent content) {
        return LiveServerMessage.builder().serverContent(content).build();
    }

    private static PetriNet vadNet() {
        return PetriNet.builder("vad")
                .compose(VadSubnet.DEF)
                .build()
                .bindActions(VadSubnet.actionBindings());
    }

    private static Map<String, TransitionAction> merge(
            Map<String, TransitionAction> a, Map<String, TransitionAction> b) {
        var merged = new LinkedHashMap<>(a);
        merged.putAll(b);
        return merged;
    }

    private static Marking run(PetriNet net, Map<Place<?>, List<Token<?>>> initial) {
        return BitmapNetExecutor.builder(net, initial)
                .eventStore(EventStore.inMemory())
                .build()
                .run();
    }
}
