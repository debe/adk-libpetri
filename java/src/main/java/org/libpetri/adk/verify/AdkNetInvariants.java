package org.libpetri.adk.verify;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.smt.SmtProperty;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.TransferRouterSubnet;

/**
 * Structural + SMT invariants for adk-libpetri nets.
 *
 * <p>Two flavours:
 * <ul>
 *   <li><b>Structural checks</b> (no Z3 needed) walk the {@link PetriNet}'s
 *       declared topology and verify load-bearing patterns. They return a
 *       {@link List} of {@link Violation}s — empty list means the
 *       invariant holds. These run on every {@code mvn verify} and catch
 *       wiring bugs at net-build time.</li>
 *   <li><b>{@link SmtProperty} factories</b> wrap libpetri's verification
 *       primitives ({@link SmtProperty.PlaceBound},
 *       {@link SmtProperty.MutualExclusion}) for use with
 *       {@code org.libpetri.smt.SmtVerifier}. These need Z3 native libs at
 *       runtime — gate Z3-using tests with {@code @EnabledIf}.</li>
 * </ul>
 *
 * <p>The three invariant families correspond to the three bug classes
 * the plan calls out as structurally absent in adk-libpetri:
 * <ol>
 *   <li><b>Legacy-session-write race</b> — only one transition
 *       consumes from {@link AdkColours#LEGACY_SESSION_WRITE} (typically
 *       the stock {@code PersistStateSubnet}). Multiple consumers would
 *       race on the ADK {@code Session.state} external store.
 *       Validated by {@link #singleLegacySessionWriter}.</li>
 *   <li><b>Hallucinated transfer target</b> — every transfer demux has
 *       an inhibitor-guarded unknown fallback. Validated by
 *       {@link #transferDemuxHasUnknownFallback}.</li>
 *   <li><b>Fire after end-invocation</b> — every "advancing" transition
 *       has an inhibitor arc on {@link AdkColours#END_INVOCATION}.
 *       Validated by {@link #endInvocationInhibitsAll}.</li>
 * </ol>
 *
 * <p>The SMT side adds bounded-resource properties: reask budget bound,
 * event-out queue bound, and termination-vs-end-invocation mutex.
 */
public final class AdkNetInvariants {

    private AdkNetInvariants() {}

    /** A topology violation found by one of the structural checks. */
    public record Violation(String invariant, String message) {}

    // ============================================================
    //  Structural checks (no Z3)
    // ============================================================

    /**
     * Verifies that at most one transition in the net consumes from
     * {@link AdkColours#LEGACY_SESSION_WRITE}. Multiple consumers would
     * reintroduce the parallel-write race that
     * {@link org.libpetri.adk.subnet.PersistStateSubnet} (the
     * sanctioned single sink) was designed to eliminate.
     */
    public static List<Violation> singleLegacySessionWriter(PetriNet net) {
        Objects.requireNonNull(net, "net");
        var consumers = consumersOf(net, AdkColours.LEGACY_SESSION_WRITE);
        if (consumers.size() > 1) {
            return List.of(new Violation("singleLegacySessionWriter",
                    "Expected at most one transition consuming from LEGACY_SESSION_WRITE;"
                            + " found " + consumers.size() + ": " + consumers));
        }
        return List.of();
    }

    /**
     * Verifies that every named "advancing" transition has an inhibitor
     * arc on {@link AdkColours#END_INVOCATION}. After end-invocation is
     * signalled (a token in that place), inhibitor-guarded advancing
     * transitions stop firing, so the net winds down without leaking
     * additional output past the end signal.
     *
     * @param net              the net to inspect
     * @param advancingNames   the set of transition names that count as
     *                         "advancing" — typically every transition
     *                         that produces a token to {@code EVENT_OUT},
     *                         {@code TOOL_CALLS}, {@code TRANSFER}, or
     *                         to any place reachable by downstream
     *                         agents. The user supplies this set
     *                         explicitly since "advancing" is
     *                         application-defined.
     */
    public static List<Violation> endInvocationInhibitsAll(
            PetriNet net, Set<String> advancingNames) {
        Objects.requireNonNull(net, "net");
        Objects.requireNonNull(advancingNames, "advancingNames");
        var missing = new ArrayList<String>();
        var seenNames = new HashSet<String>();
        for (var t : net.transitions()) {
            seenNames.add(t.name());
            if (!advancingNames.contains(t.name())) continue;
            boolean hasInhibitor = t.inhibitors().stream()
                    .anyMatch(a -> a.place().equals(AdkColours.END_INVOCATION));
            if (!hasInhibitor) missing.add(t.name());
        }
        var unknown = new ArrayList<>(advancingNames);
        unknown.removeAll(seenNames);
        var issues = new ArrayList<Violation>();
        if (!missing.isEmpty()) {
            issues.add(new Violation("endInvocationInhibitsAll",
                    "Advancing transitions missing inhibitor(END_INVOCATION): " + missing));
        }
        if (!unknown.isEmpty()) {
            issues.add(new Violation("endInvocationInhibitsAll",
                    "Declared advancing transitions not in net: " + unknown));
        }
        return issues;
    }

    /**
     * Verifies that any net composing a
     * {@link TransferRouterSubnet}-shaped demux has the
     * {@link TransferRouterSubnet#UNKNOWN_TARGET} fallback place
     * present AND a consumer for it (so an unknown agent name produces
     * a typed error event rather than dead-letter accumulation).
     *
     * <p>This catches manual transfer-router topologies where the user
     * forgot the unknown-fallback transition — without it, hallucinated
     * agent names silently pile up on {@code UNKNOWN_TARGET}.
     */
    public static List<Violation> transferDemuxHasUnknownFallback(PetriNet net) {
        Objects.requireNonNull(net, "net");
        var hasUnknownPlace = net.places().contains(TransferRouterSubnet.UNKNOWN_TARGET);
        if (!hasUnknownPlace) {
            return List.of();  // no transfer demux in this net — invariant vacuously holds
        }
        var consumers = consumersOf(net, TransferRouterSubnet.UNKNOWN_TARGET);
        if (consumers.isEmpty()) {
            return List.of(new Violation("transferDemuxHasUnknownFallback",
                    "TransferRouterSubnet's UNKNOWN_TARGET place has no consumer — "
                            + "hallucinated agent names will accumulate as dead-letters"));
        }
        return List.of();
    }

    // ============================================================
    //  SMT property factories (Z3 needed at runtime)
    // ============================================================

    /**
     * Property: {@code REASK_BUDGET} (or any budget-style place) never
     * exceeds {@code maxBudget} tokens. Combined with the reset-arc on
     * the seed transition, this proves the budget place is exactly
     * {@code [0, maxBudget]}-bounded across all reachable markings.
     */
    public static SmtProperty reaskBudgetIsBounded(Place<?> reaskBudget, int maxBudget) {
        Objects.requireNonNull(reaskBudget, "reaskBudget");
        if (maxBudget < 1) {
            throw new IllegalArgumentException("maxBudget must be >= 1, got: " + maxBudget);
        }
        return SmtProperty.placeBound(reaskBudget, maxBudget);
    }

    /**
     * Property: {@link AdkColours#EVENT_OUT} (or any output place)
     * never accumulates more than {@code maxBuffered} tokens at once.
     * Useful for proving the agent's output queue is bounded — i.e.,
     * the net's output rate doesn't outpace the consumer.
     */
    public static SmtProperty eventOutBounded(int maxBuffered) {
        if (maxBuffered < 1) {
            throw new IllegalArgumentException("maxBuffered must be >= 1, got: " + maxBuffered);
        }
        return SmtProperty.placeBound(AdkColours.EVENT_OUT, maxBuffered);
    }

    /**
     * Property: {@link AdkColours#END_INVOCATION} and the given
     * {@code restrictedPlace} are never marked simultaneously — once
     * end-invocation is signalled, the restricted place stays empty.
     *
     * <p>This is the structural-bug-class invariant for "no fire after
     * end_invocation": pick {@code restrictedPlace} to be a place that
     * an advancing transition produces to (e.g.
     * {@link AdkColours#LLM_REQUEST} for the LLM-loop case), and the
     * property holds iff no transition writes to it after the end signal.
     */
    public static SmtProperty noFireAfterEndInvocation(Place<?> restrictedPlace) {
        Objects.requireNonNull(restrictedPlace, "restrictedPlace");
        return SmtProperty.mutualExclusion(AdkColours.END_INVOCATION, restrictedPlace);
    }

    /**
     * Property: the transitions whose post-set includes {@code postsetA}
     * and {@code postsetB} respectively never both contribute in the
     * same reachable trace.
     *
     * <p>This is the structural form of "at-most-one commit fires":
     * pick {@code postsetA} and {@code postsetB} as the marker places
     * that distinct commit transitions produce to (typically two
     * {@code Place<Void>} flags), and the property holds iff no
     * reachable marking has both flags populated. Useful for the
     * optimistic-commit-with-fallback pattern, where a cheap-path
     * commit and a slow-path commit must be mutually exclusive.
     */
    public static SmtProperty atMostOneCommits(Place<?> postsetA, Place<?> postsetB) {
        Objects.requireNonNull(postsetA, "postsetA");
        Objects.requireNonNull(postsetB, "postsetB");
        return SmtProperty.mutualExclusion(postsetA, postsetB);
    }

    // ============================================================
    //  Helpers
    // ============================================================

    private static List<String> consumersOf(PetriNet net, Place<?> place) {
        var consumers = new ArrayList<String>();
        for (var t : net.transitions()) {
            for (Arc.In in : t.inputSpecs()) {
                if (in.place().equals(place)) {
                    consumers.add(t.name());
                    break;
                }
            }
        }
        return consumers;
    }
}
