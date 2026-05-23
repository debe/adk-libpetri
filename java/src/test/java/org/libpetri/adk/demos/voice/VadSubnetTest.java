package org.libpetri.adk.demos.voice;

import static com.google.common.truth.Truth.assertThat;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.runtime.BitmapNetExecutor;
import org.libpetri.runtime.Marking;

class VadSubnetTest {

    @Test
    void speech_start_when_closed_opens_the_window() {
        Marking m = run(vadNet(), seed(VadSubnet.Places.SPEECH_STARTED, 1));

        assertThat(m.peekTokens(VadSubnet.VOICE_ACTIVITY_OPEN)).hasSize(1);
        assertThat(m.peekTokens(VadSubnet.Places.SPEECH_EDGE_IGNORED)).isEmpty();
    }

    @Test
    void redundant_speech_start_while_open_is_absorbed() {
        var initial = seed(VadSubnet.Places.SPEECH_STARTED, 1);
        initial.put(VadSubnet.VOICE_ACTIVITY_OPEN, List.of(Token.of((Void) null)));

        Marking m = run(vadNet(), initial);

        // Window count stays exactly 1 (read, not re-produced); the edge is logged.
        assertThat(m.peekTokens(VadSubnet.VOICE_ACTIVITY_OPEN)).hasSize(1);
        assertThat(m.peekTokens(VadSubnet.Places.SPEECH_EDGE_IGNORED)).hasSize(1);
    }

    @Test
    void speech_stop_while_open_closes_the_window_and_ends_the_utterance() {
        var initial = seed(VadSubnet.Places.SPEECH_STOPPED, 1);
        initial.put(VadSubnet.VOICE_ACTIVITY_OPEN, List.of(Token.of((Void) null)));

        Marking m = run(vadNet(), initial);

        assertThat(m.peekTokens(VadSubnet.VOICE_ACTIVITY_OPEN)).isEmpty();
        assertThat(m.peekTokens(VadSubnet.Places.UTTERANCE_ENDED)).hasSize(1);
    }

    @Test
    void redundant_speech_stop_while_closed_is_absorbed() {
        Marking m = run(vadNet(), seed(VadSubnet.Places.SPEECH_STOPPED, 1));

        assertThat(m.peekTokens(VadSubnet.Places.UTTERANCE_ENDED)).isEmpty();
        assertThat(m.peekTokens(VadSubnet.Places.SPEECH_EDGE_IGNORED)).hasSize(1);
    }

    @Test
    void window_opened_by_vad_routes_a_subsequent_interrupt_to_barge_in() {
        // Composing the two subnets fuses VOICE_ACTIVITY_OPEN by place identity:
        // the window VadSubnet opens is the one BargeInSubnet reads.
        var net = PetriNet.builder("vad+bargein")
                .compose(VadSubnet.DEF)
                .compose(BargeInSubnet.DEF)
                .build()
                .bindActions(merge(VadSubnet.actionBindings(), BargeInSubnet.actionBindings()));

        // Phase 1: speech starts → VadSubnet opens the window.
        Marking afterSpeech = run(net, seed(VadSubnet.Places.SPEECH_STARTED, 1));
        assertThat(afterSpeech.peekTokens(VadSubnet.VOICE_ACTIVITY_OPEN)).hasSize(1);

        // Phase 2: feed that exact window state forward + a barge-in interrupt.
        var phase2 = new LinkedHashMap<Place<?>, List<Token<?>>>();
        phase2.put(VadSubnet.VOICE_ACTIVITY_OPEN,
                new ArrayList<>(afterSpeech.peekTokens(VadSubnet.VOICE_ACTIVITY_OPEN)));
        phase2.put(BargeInSubnet.Places.INTERRUPTED, List.of(Token.of((Void) null)));

        Marking afterInterrupt = run(net, phase2);
        assertThat(afterInterrupt.peekTokens(BargeInSubnet.Places.BARGE_IN_SENT)).hasSize(1);
        assertThat(afterInterrupt.peekTokens(BargeInSubnet.Places.INTERRUPT_DISCARDED)).isEmpty();
    }

    // ============================================================
    //  Fixture
    // ============================================================

    private static PetriNet vadNet() {
        return PetriNet.builder("vad")
                .compose(VadSubnet.DEF)
                .build()
                .bindActions(VadSubnet.actionBindings());
    }

    private static LinkedHashMap<Place<?>, List<Token<?>>> seed(Place<Void> place, int count) {
        var tokens = new ArrayList<Token<?>>();
        for (int i = 0; i < count; i++) tokens.add(Token.of((Void) null));
        var initial = new LinkedHashMap<Place<?>, List<Token<?>>>();
        initial.put(place, tokens);
        return initial;
    }

    private static Map<String, org.libpetri.core.TransitionAction> merge(
            Map<String, org.libpetri.core.TransitionAction> a,
            Map<String, org.libpetri.core.TransitionAction> b) {
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
