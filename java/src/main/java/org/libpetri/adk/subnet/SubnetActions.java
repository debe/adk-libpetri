package org.libpetri.adk.subnet;

import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.TreeSet;
import org.libpetri.core.PetriNet;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;

/**
 * Validates a subnet's transition-name → action bindings against the
 * declared {@link SubnetDef}, catching the silent-passthrough failure mode
 * of {@link org.libpetri.core.PetriNet#bindActions(Map)}.
 *
 * <p>{@code PetriNet.bindActions(Map)} substitutes a no-op
 * {@code passthrough()} for any transition with no binding and silently
 * ignores any binding key that matches no transition. A binding map with
 * a missing entry or a typo'd key therefore produces a silently-wrong net
 * rather than an error. {@code SubnetActions.bind} closes that gap for one
 * subnet, and {@link #bindComposed} for a whole composed net.
 *
 * <p>Composing several subnets means binding several maps at once. Chaining
 * {@code bindActions} calls does not work (each call rebinds every transition
 * its map omits to {@code passthrough()}), and merging by hand with
 * {@code putAll} silently lets a later map overwrite an earlier one.
 * {@link #merge} rejects that overlap, and {@link #bindComposed} also checks
 * the merged keys against the composed net's transitions:
 *
 * <pre>{@code
 * var net = SubnetActions.bindComposed(
 *     PetriNet.builder("app").compose(LlmAgentSubnet.DEF)
 *         .compose(TransferRouterSubnet.def(targets)).build(),
 *     LlmAgentSubnet.actionBindings(llm, agentConfig),
 *     TransferRouterSubnet.actionBindings(targets, routerConfig));
 * }</pre>
 */
public final class SubnetActions {

    private SubnetActions() {}

    /**
     * Validates that {@code session}'s keys exactly match the transition
     * names declared by {@code def}, then returns {@code session} unchanged.
     *
     * @throws IllegalStateException if a declared transition has no binding
     *                               or a binding key matches no declared
     *                               transition
     */
    public static Map<String, TransitionAction> bind(
            SubnetDef<?> def, Map<String, TransitionAction> session) {
        validate(def, session);
        return session;
    }

    /**
     * Merges {@code stateless} ctx-only actions with the caller-supplied
     * {@code session} actions, then validates the union against {@code def}.
     *
     * @throws IllegalStateException if the two maps bind the same transition,
     *                               or if the union does not exactly cover
     *                               {@code def}'s declared transitions
     */
    public static Map<String, TransitionAction> bind(
            SubnetDef<?> def,
            Map<String, TransitionAction> stateless,
            Map<String, TransitionAction> session) {
        var merged = new LinkedHashMap<String, TransitionAction>();
        merged.putAll(stateless);
        for (var e : session.entrySet()) {
            if (merged.put(e.getKey(), e.getValue()) != null) {
                throw new IllegalStateException("Subnet '" + def.name()
                        + "': transition '" + e.getKey()
                        + "' is bound by both the stateless and the session map.");
            }
        }
        validate(def, merged);
        return merged;
    }

    /**
     * Merges binding maps in order, rejecting any transition bound by more
     * than one of them.
     *
     * @throws IllegalStateException if two maps bind the same transition
     */
    @SafeVarargs
    public static Map<String, TransitionAction> merge(Map<String, TransitionAction>... maps) {
        var merged = new LinkedHashMap<String, TransitionAction>();
        for (var map : maps) {
            for (var e : map.entrySet()) {
                if (merged.put(e.getKey(), e.getValue()) != null) {
                    throw new IllegalStateException("Transition '" + e.getKey()
                            + "' is bound by more than one binding map.");
                }
            }
        }
        return merged;
    }

    /**
     * Merges {@code maps} with {@link #merge}, checks that the merged keys
     * exactly match {@code net}'s transition names, and binds them.
     *
     * @throws IllegalStateException if two maps bind the same transition, a
     *                               transition of {@code net} has no binding,
     *                               or a key matches no transition
     */
    @SafeVarargs
    public static PetriNet bindComposed(PetriNet net, Map<String, TransitionAction>... maps) {
        var merged = merge(maps);
        validate(net.name(), names(net.transitions()), merged);
        return net.bindActions(merged);
    }

    private static void validate(SubnetDef<?> def, Map<String, TransitionAction> bindings) {
        validate(def.name(), names(def.body().transitions()), bindings);
    }

    private static LinkedHashSet<String> names(Collection<Transition> transitions) {
        var declared = new LinkedHashSet<String>();
        for (var t : transitions) {
            declared.add(t.name());
        }
        return declared;
    }

    private static void validate(
            String netName, LinkedHashSet<String> declared, Map<String, TransitionAction> bindings) {
        var missing = new TreeSet<>(declared);
        missing.removeAll(bindings.keySet());
        var extra = new TreeSet<>(bindings.keySet());
        extra.removeAll(declared);
        if (!missing.isEmpty() || !extra.isEmpty()) {
            throw new IllegalStateException("'" + netName
                    + "' action-binding mismatch:"
                    + (missing.isEmpty() ? "" : " missing keys " + missing)
                    + (extra.isEmpty() ? "" : " extra keys " + extra)
                    + ". Declared transitions: " + new TreeSet<>(declared) + ".");
        }
    }
}
