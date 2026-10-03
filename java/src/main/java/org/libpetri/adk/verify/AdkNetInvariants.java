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
 *       primitive {@link SmtProperty.PlaceBound} for use with
 *       {@code org.libpetri.smt.SmtVerifier}. These need a {@code z3}
 *       binary at runtime — gate Z3-using tests with {@code @EnabledIf}.
 *       Anything already a libpetri primitive stays one: for mutual
 *       exclusion use {@link SmtProperty#mutualExclusion} directly.</li>
 * </ul>
 *
 * <p>The three invariant families correspond to the three bug classes
 * the README names as structurally absent in adk-libpetri:
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
 * <p>The SMT side adds bounded-resource properties: a budget-place bound
 * (the reask budget) and an event-out queue bound. Where each is proved, and
 * on which net, is listed in the README's verification section.
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
     * <p>These inhibitors end one invocation and leave the session's runner
     * serving the next. libpetri 7.0's terminal places
     * ({@code .terminal(AdkColours.END_INVOCATION)} on the top-level net) are
     * not a substitute: a terminal token ends the whole run, and with it the
     * per-session runner, abandoning any action in flight. ADK's
     * {@code endInvocation} ends a single invocation; a terminal
     * {@code END_INVOCATION} ends the session. A
     * {@code SessionExecutorRegistry} also keeps handing out the stopped
     * runner for that key until the key is closed, so later turns on it get
     * no answer. Use a terminal place only as a deliberate session end, closed
     * from the registry afterwards; it does not make these inhibitors
     * redundant.
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
     * Property: {@code budgetPlace} never exceeds {@code maxTokens} tokens in
     * any reachable marking. It is {@link SmtProperty#placeBound} under a
     * name that says what it is for: a budget that must not stack when its
     * seed transition fires again before the old budget is spent.
     *
     * <p>State the bound in seeds. libpetri has no weighted output arc, so a
     * seed transition that writes N permits is verified as writing one, and a
     * bound of N would hold trivially. {@code maxTokens = 1} is the claim that
     * matters: the place never holds more than one seed's worth, which fails
     * when a second seed can land before the first is cleared. Run it with
     * more than one arrival at the seed, or it cannot fail.
     *
     * <p>A budget only bounds something if a transition consumes a permit
     * without returning it, and an exhaustion path takes over when none is
     * left. The reask budget of {@code LlmAgentSubnet} is the case in this
     * repo.
     */
    public static SmtProperty budgetPlaceBounded(Place<?> budgetPlace, int maxTokens) {
        Objects.requireNonNull(budgetPlace, "budgetPlace");
        if (maxTokens < 1) {
            throw new IllegalArgumentException("maxTokens must be >= 1, got: " + maxTokens);
        }
        return SmtProperty.placeBound(budgetPlace, maxTokens);
    }

    /**
     * Property: {@link AdkColours#EVENT_OUT} never accumulates more than
     * {@code maxBuffered} tokens at once. For any other place, use
     * {@link #budgetPlaceBounded(Place, int)}, which takes one.
     * Useful for proving the agent's output queue is bounded — i.e.,
     * the net's output rate doesn't outpace the consumer.
     */
    public static SmtProperty eventOutBounded(int maxBuffered) {
        if (maxBuffered < 1) {
            throw new IllegalArgumentException("maxBuffered must be >= 1, got: " + maxBuffered);
        }
        return SmtProperty.placeBound(AdkColours.EVENT_OUT, maxBuffered);
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
