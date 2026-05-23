package org.libpetri.adk.demos.voice;

import static com.google.common.truth.Truth.assertThat;

import java.util.ArrayList;
import java.util.Collection;
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

class BargeInSubnetTest {

    @Test
    void voice_open_routes_interrupt_to_send_barge_in() {
        var fixture = run(/*voiceOpen=*/ true, /*interruptCount=*/ 1);

        assertThat(fixture.tokens(BargeInSubnet.Places.BARGE_IN_SENT)).hasSize(1);
        assertThat(fixture.tokens(BargeInSubnet.Places.INTERRUPT_DISCARDED)).isEmpty();
    }

    @Test
    void voice_closed_routes_interrupt_to_discard() {
        var fixture = run(/*voiceOpen=*/ false, /*interruptCount=*/ 1);

        assertThat(fixture.tokens(BargeInSubnet.Places.BARGE_IN_SENT)).isEmpty();
        assertThat(fixture.tokens(BargeInSubnet.Places.INTERRUPT_DISCARDED)).hasSize(1);
    }

    @Test
    void multiple_interrupts_each_routed_independently() {
        var fixture = run(/*voiceOpen=*/ true, /*interruptCount=*/ 3);

        // VOICE_ACTIVITY_OPEN is read (not consumed), so all 3 interrupts
        // see it and route to BARGE_IN_SENT.
        assertThat(fixture.tokens(BargeInSubnet.Places.BARGE_IN_SENT)).hasSize(3);
        assertThat(fixture.tokens(BargeInSubnet.Places.INTERRUPT_DISCARDED)).isEmpty();
    }

    @Test
    void interface_exposes_four_ports() {
        var ports = BargeInSubnet.DEF.iface().ports().stream()
                .map(p -> p.name()).sorted().toList();
        assertThat(ports).containsExactly(
                "bargeInSent",
                "interruptDiscarded",
                "interrupted",
                "voiceActivityOpen");
    }

    @Test
    void def_declares_exactly_send_and_discard_transitions() {
        var transitions = BargeInSubnet.DEF.body().transitions().stream()
                .map(t -> t.name()).sorted().toList();
        assertThat(transitions).containsExactly(
                BargeInSubnet.Transitions.DISCARD_INTERRUPT,
                BargeInSubnet.Transitions.SEND_BARGE_IN);
    }

    // ============================================================
    //  Fixture
    // ============================================================

    private record Fixture(Marking marking) {
        List<Token<?>> tokens(Place<?> p) {
            Collection<? extends Token<?>> raw = marking.peekTokens(p);
            return new ArrayList<>(raw);
        }
    }

    private static Fixture run(boolean voiceOpen, int interruptCount) {
        var net = PetriNet.builder("test")
                .compose(BargeInSubnet.DEF)
                .build()
                .bindActions(BargeInSubnet.actionBindings());

        // Seed initial marking: N interrupts; optionally a voice-activity token.
        List<Token<?>> interrupts = new ArrayList<>();
        for (int i = 0; i < interruptCount; i++) interrupts.add(Token.of((Void) null));
        var initial = new LinkedHashMap<Place<?>, List<Token<?>>>();
        initial.put(BargeInSubnet.Places.INTERRUPTED, interrupts);
        if (voiceOpen) {
            initial.put(BargeInSubnet.Places.VOICE_ACTIVITY_OPEN, List.of(Token.of((Void) null)));
        }

        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(EventStore.inMemory())
                .build();
        return new Fixture(executor.run());
    }
}
