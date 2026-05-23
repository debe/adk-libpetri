package org.libpetri.adk.subnet;

import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.TreeSet;
import org.libpetri.core.SubnetDef;
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
 * rather than an error. {@code SubnetActions.bind} closes that gap.
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

    private static void validate(SubnetDef<?> def, Map<String, TransitionAction> bindings) {
        var declared = new LinkedHashSet<String>();
        for (var t : def.body().transitions()) {
            declared.add(t.name());
        }
        var missing = new TreeSet<>(declared);
        missing.removeAll(bindings.keySet());
        var extra = new TreeSet<>(bindings.keySet());
        extra.removeAll(declared);
        if (!missing.isEmpty() || !extra.isEmpty()) {
            throw new IllegalStateException("Subnet '" + def.name()
                    + "' action-binding mismatch:"
                    + (missing.isEmpty() ? "" : " missing keys " + missing)
                    + (extra.isEmpty() ? "" : " extra keys " + extra)
                    + ". Declared transitions: " + new TreeSet<>(declared) + ".");
        }
    }
}
