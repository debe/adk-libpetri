package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.Map;
import org.junit.jupiter.api.Test;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;

class SubnetActionsTest {

    private static final Place<String> A = Place.of("a", String.class);
    private static final Place<String> B = Place.of("b", String.class);
    private static final Place<String> C = Place.of("c", String.class);

    private static final TransitionAction ONE = TransitionAction.fork();
    private static final TransitionAction TWO = TransitionAction.fork();

    private static PetriNet twoStepNet() {
        return PetriNet.builder("two-step")
                .transition(Transition.builder("first")
                        .inputs(Arc.In.one(A)).outputs(Arc.Out.place(B)).build())
                .transition(Transition.builder("second")
                        .inputs(Arc.In.one(B)).outputs(Arc.Out.place(C)).build())
                .build();
    }

    @Test
    void merge_keeps_every_binding_in_order() {
        var merged = SubnetActions.merge(Map.of("first", ONE), Map.of("second", TWO));
        assertThat(merged).containsExactly("first", ONE, "second", TWO).inOrder();
    }

    @Test
    void merge_and_bind_return_unmodifiable_maps() {
        var merged = SubnetActions.merge(Map.of("first", ONE), Map.of("second", TWO));
        assertThrows(UnsupportedOperationException.class, () -> merged.put("third", ONE));

        var def = org.libpetri.core.SubnetDef.fromNet(twoStepNet(),
                org.libpetri.core.Interface.builder().build());
        var session = new java.util.HashMap<>(Map.of("first", ONE, "second", TWO));
        var bound = SubnetActions.bind(def, session);
        assertThrows(UnsupportedOperationException.class, () -> bound.remove("first"));
        // A copy: a later change to the caller's map cannot unvalidate it.
        session.remove("first");
        assertThat(bound).containsKey("first");
        assertThrows(UnsupportedOperationException.class,
                () -> SubnetActions.bind(def, Map.of("first", ONE), Map.of("second", TWO)).clear());
    }

    @Test
    void merge_rejects_a_transition_bound_twice() {
        var e = assertThrows(IllegalStateException.class,
                () -> SubnetActions.merge(Map.of("first", ONE), Map.of("first", TWO)));
        assertThat(e).hasMessageThat().contains("'first'");
    }

    @Test
    void bind_composed_binds_every_transition() {
        var bound = SubnetActions.bindComposed(twoStepNet(),
                Map.of("first", ONE), Map.of("second", TWO));
        var actions = bound.transitions().stream()
                .collect(java.util.stream.Collectors.toMap(Transition::name, Transition::action));
        assertThat(actions).containsExactly("first", ONE, "second", TWO);
    }

    /**
     * The failure mode this exists for: {@code PetriNet.bindActions(Map)}
     * would silently bind the missing transition to passthrough().
     */
    @Test
    void bind_composed_rejects_an_unbound_transition() {
        var e = assertThrows(IllegalStateException.class,
                () -> SubnetActions.bindComposed(twoStepNet(), Map.of("first", ONE)));
        assertThat(e).hasMessageThat().contains("missing keys [second]");
    }

    @Test
    void bind_composed_rejects_a_key_that_matches_no_transition() {
        var e = assertThrows(IllegalStateException.class,
                () -> SubnetActions.bindComposed(twoStepNet(),
                        Map.of("first", ONE, "second", TWO), Map.of("thrid", ONE)));
        assertThat(e).hasMessageThat().contains("extra keys [thrid]");
    }
}
