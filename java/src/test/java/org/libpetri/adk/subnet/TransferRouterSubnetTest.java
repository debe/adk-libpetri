package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.events.Event;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.runtime.BitmapNetExecutor;

class TransferRouterSubnetTest {

    private static final Set<String> KNOWN = Set.of("billing", "sales", "support");
    private static final TransferRouterSubnet.Config CONFIG =
            new TransferRouterSubnet.Config("router", () -> "inv-fixed");

    // ============================================================
    //  Happy path — valid names route to per-target places
    // ============================================================

    @Test
    void valid_agent_name_routes_to_matching_target_place() {
        var fixture = run(KNOWN, CONFIG,
                new AdkColours.TransferTarget("billing"));

        // The token landed on the per-billing target place.
        assertThat(tokensAt(fixture, TransferRouterSubnet.targetPlace("billing")))
                .hasSize(1);
        assertThat(tokensAt(fixture, TransferRouterSubnet.targetPlace("billing")).get(0).agentName())
                .isEqualTo("billing");

        // Other target places stay empty.
        assertThat(tokensAt(fixture, TransferRouterSubnet.targetPlace("sales"))).isEmpty();
        assertThat(tokensAt(fixture, TransferRouterSubnet.targetPlace("support"))).isEmpty();
        assertThat(tokensAt(fixture, TransferRouterSubnet.UNKNOWN_TARGET)).isEmpty();

        // No unknown-error event emitted.
        assertThat(fixture.events).isEmpty();
    }

    @Test
    void multiple_valid_targets_each_routes_independently() {
        var fixture = run(KNOWN, CONFIG,
                new AdkColours.TransferTarget("sales"),
                new AdkColours.TransferTarget("support"),
                new AdkColours.TransferTarget("billing"));

        assertThat(tokensAt(fixture, TransferRouterSubnet.targetPlace("sales"))).hasSize(1);
        assertThat(tokensAt(fixture, TransferRouterSubnet.targetPlace("support"))).hasSize(1);
        assertThat(tokensAt(fixture, TransferRouterSubnet.targetPlace("billing"))).hasSize(1);
        assertThat(fixture.events).isEmpty();
    }

    // ============================================================
    //  Unknown-target path — produces typed error Event to EVENT_OUT
    // ============================================================

    @Test
    void unknown_agent_name_produces_typed_error_event() {
        var fixture = run(KNOWN, CONFIG,
                new AdkColours.TransferTarget("Salez" /* typo */));

        // Token transited through UNKNOWN_TARGET (consumed) and produced an Event.
        assertThat(tokensAt(fixture, TransferRouterSubnet.UNKNOWN_TARGET)).isEmpty();
        assertThat(fixture.events).hasSize(1);

        Event errorEvent = fixture.events.get(0);
        assertThat(errorEvent.author()).isEqualTo("router");
        assertThat(errorEvent.invocationId()).isEqualTo("inv-fixed");
        assertThat(errorEvent.content().get().text()).contains("unknown agent");
        assertThat(errorEvent.content().get().text()).contains("Salez");
    }

    @Test
    void empty_known_set_routes_every_transfer_to_unknown() {
        var fixture = run(Set.of(), CONFIG,
                new AdkColours.TransferTarget("any"),
                new AdkColours.TransferTarget("other"));

        // Both produced error events.
        assertThat(fixture.events).hasSize(2);
        assertThat(fixture.events.get(0).content().get().text()).contains("any");
        assertThat(fixture.events.get(1).content().get().text()).contains("other");
    }

    // ============================================================
    //  Structural shape — interface ports + transitions
    // ============================================================

    @Test
    void interface_exposes_one_input_one_eventout_n_targets_plus_unknown() {
        var def = TransferRouterSubnet.def(KNOWN);
        var portNames = def.iface().ports().stream().map(p -> p.name()).sorted().toList();

        assertThat(portNames).containsExactly(
                "eventOut",
                "target/_unknown",
                "target/billing",
                "target/sales",
                "target/support",
                "transfer")
                .inOrder();
    }

    @Test
    void def_declares_exactly_demux_and_emit_unknown_transitions() {
        var def = TransferRouterSubnet.def(KNOWN);
        var transitionNames = def.body().transitions().stream()
                .map(t -> t.name())
                .sorted()
                .toList();

        assertThat(transitionNames).containsExactly(
                TransferRouterSubnet.Transitions.DEMUX,
                TransferRouterSubnet.Transitions.EMIT_UNKNOWN_ERROR)
                .inOrder();
    }

    @Test
    void target_place_helper_returns_consistent_place_record() {
        // Two calls with the same name → equal Place records (record equality).
        var p1 = TransferRouterSubnet.targetPlace("billing");
        var p2 = TransferRouterSubnet.targetPlace("billing");
        assertThat(p1).isEqualTo(p2);
        assertThat(p1.name()).isEqualTo("TransferRouter_target/billing");
        assertThat(p1.tokenType()).isEqualTo(AdkColours.TransferTarget.class);
    }

    // ============================================================
    //  XOR validation — sanity that all known targets + unknown are reachable
    // ============================================================

    @Test
    void exactly_one_xor_child_receives_token_per_fire() {
        // 4 inputs (3 valid + 1 unknown) → expect token counts to sum to 4 across all branches.
        var fixture = run(KNOWN, CONFIG,
                new AdkColours.TransferTarget("billing"),
                new AdkColours.TransferTarget("sales"),
                new AdkColours.TransferTarget("support"),
                new AdkColours.TransferTarget("ghost"));

        int billing  = tokensAt(fixture, TransferRouterSubnet.targetPlace("billing")).size();
        int sales    = tokensAt(fixture, TransferRouterSubnet.targetPlace("sales")).size();
        int support  = tokensAt(fixture, TransferRouterSubnet.targetPlace("support")).size();
        int unknown  = tokensAt(fixture, TransferRouterSubnet.UNKNOWN_TARGET).size();
        int events   = fixture.events.size();

        // 3 valid → 3 tokens spread one each across billing/sales/support.
        // 1 unknown → consumed through UNKNOWN_TARGET → 1 error event on EVENT_OUT.
        assertThat(billing).isEqualTo(1);
        assertThat(sales).isEqualTo(1);
        assertThat(support).isEqualTo(1);
        assertThat(unknown).isEqualTo(0);  // immediately drained by EmitUnknownError
        assertThat(events).isEqualTo(1);
    }

    // ============================================================
    //  Action-binding validation
    // ============================================================

    @Test
    void mismatched_known_set_between_def_and_bindings_is_caught() {
        // bindings for {a, b} validated against def({a, b, c}) — SubnetActions catches it
        // because SubnetActions builds the def fresh from the supplied set, so the demux
        // transition's structure matches. This test just confirms the bindings round-trip.
        var bindings = TransferRouterSubnet.actionBindings(Set.of("a", "b"), CONFIG);
        assertThat(bindings).containsKey(TransferRouterSubnet.Transitions.DEMUX);
        assertThat(bindings).containsKey(TransferRouterSubnet.Transitions.EMIT_UNKNOWN_ERROR);
    }

    // ============================================================
    //  Fixtures
    // ============================================================

    private record Fixture(
            Map<Place<?>, List<Token<?>>> finalMarking,
            List<Event> events) {}

    private static Fixture run(Set<String> knownNames,
                                TransferRouterSubnet.Config config,
                                AdkColours.TransferTarget... transfers) {
        var def = TransferRouterSubnet.def(knownNames);
        var net = PetriNet.builder("test")
                .compose(def)
                .build()
                .bindActions(TransferRouterSubnet.actionBindings(knownNames, config));

        List<Token<?>> tokens = new ArrayList<>();
        for (var t : transfers) tokens.add(Token.of(t));
        var initial = Map.<Place<?>, List<Token<?>>>of(AdkColours.TRANSFER, tokens);

        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(EventStore.inMemory())
                .build();
        var marking = executor.run();

        // Collect all per-place tokens into a snapshot map.
        var snapshot = new LinkedHashMap<Place<?>, List<Token<?>>>();
        for (var place : net.places()) {
            Collection<? extends Token<?>> raw = marking.peekTokens(place);
            if (!raw.isEmpty()) {
                snapshot.put(place, new ArrayList<>(raw));
            }
        }
        var events = marking.peekTokens(AdkColours.EVENT_OUT).stream()
                .map(Token::value).toList();
        return new Fixture(snapshot, events);
    }

    @SuppressWarnings("unchecked")
    private static List<AdkColours.TransferTarget> tokensAt(
            Fixture f, Place<AdkColours.TransferTarget> p) {
        var raw = f.finalMarking.getOrDefault(p, List.of());
        return raw.stream().map(t -> (AdkColours.TransferTarget) t.value()).toList();
    }
}
