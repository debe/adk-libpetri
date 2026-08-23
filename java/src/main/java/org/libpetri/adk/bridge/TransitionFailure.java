package org.libpetri.adk.bridge;

import java.time.Duration;
import java.time.Instant;
import java.util.Objects;
import java.util.Optional;
import org.libpetri.event.NetEvent;

/**
 * A transition failure, carried on
 * {@link org.libpetri.adk.runner.PetriRunner#failureSignal()} with its identity
 * intact.
 *
 * <p>libpetri reports failures as structured {@link NetEvent} records. This used
 * to be flattened into a bare {@code RuntimeException} whose message concatenated
 * the transition name, the error text and the exception type, which left a
 * consumer no way to act on a failure except to run a regular expression over
 * {@code getMessage()}. That matters because the whole point of the signal is
 * that the caller decides what a failure means: on a long-lived net, "the tool
 * call failed" and "the persist step blew its deadline" want different answers,
 * and on a composed net you often care only about failures from one instance.
 *
 * <p>The fields are therefore kept as fields. {@link #transitionName()} is the
 * one to branch on; {@link #instancePrefix()} narrows it to a composed instance.
 *
 * <p>{@link NetEvent.ActionTimedOut} is deliberately <b>not</b> represented here.
 * That event means an async action exceeded its {@code Out.Timeout} and the net
 * routed its tokens down the declared timeout branch: a modelled outcome the net
 * already handled, not a failure the caller has to rescue.
 */
public final class TransitionFailure extends RuntimeException {

    private static final long serialVersionUID = 1L;

    /** What went wrong, kept apart from the transition's identity. */
    public enum Kind {
        /** The action threw. Consumed tokens are lost (libpetri EXEC-031). */
        ACTION_THREW,
        /** The transition exceeded the deadline configured by its {@code Timing}. */
        DEADLINE_EXCEEDED,
    }

    private final String transitionName;
    private final Kind kind;
    private final transient Instant occurredAt;
    private final String exceptionType;
    private final transient Duration deadline;
    private final transient Duration actualDuration;

    private TransitionFailure(String message,
                              String transitionName,
                              Kind kind,
                              Instant occurredAt,
                              String exceptionType,
                              Duration deadline,
                              Duration actualDuration) {
        super(message);
        this.transitionName = transitionName;
        this.kind = kind;
        this.occurredAt = occurredAt;
        this.exceptionType = exceptionType;
        this.deadline = deadline;
        this.actualDuration = actualDuration;
    }

    static TransitionFailure of(NetEvent.TransitionFailed e) {
        return new TransitionFailure(
                "Transition " + e.transitionName() + " failed: "
                        + e.errorMessage() + " (" + e.exceptionType() + ")",
                e.transitionName(), Kind.ACTION_THREW, e.timestamp(),
                e.exceptionType(), null, null);
    }

    static TransitionFailure of(NetEvent.TransitionTimedOut e) {
        return new TransitionFailure(
                "Transition " + e.transitionName() + " exceeded its deadline of "
                        + e.deadline() + " (ran for " + e.actualDuration() + ")",
                e.transitionName(), Kind.DEADLINE_EXCEEDED, e.timestamp(),
                null, e.deadline(), e.actualDuration());
    }

    /** Name of the transition that failed, as declared in the net. */
    public String transitionName() {
        return transitionName;
    }

    /** Whether the action threw or the transition blew its deadline. */
    public Kind kind() {
        return kind;
    }

    /** When libpetri observed the failure. */
    public Instant occurredAt() {
        return occurredAt;
    }

    /**
     * Class name of the throwable the action raised.
     *
     * <p>Empty for {@link Kind#DEADLINE_EXCEEDED}: nothing was thrown, the
     * transition simply ran out of time.
     */
    public Optional<String> exceptionType() {
        return Optional.ofNullable(exceptionType);
    }

    /** Configured deadline, present only for {@link Kind#DEADLINE_EXCEEDED}. */
    public Optional<Duration> deadline() {
        return Optional.ofNullable(deadline);
    }

    /** Time actually elapsed, present only for {@link Kind#DEADLINE_EXCEEDED}. */
    public Optional<Duration> actualDuration() {
        return Optional.ofNullable(actualDuration);
    }

    /**
     * Instance prefix when this transition belongs to a composed subnet instance
     * (libpetri MOD-041), so a caller can tell which instance failed.
     */
    public Optional<String> instancePrefix() {
        return NetEvent.instancePrefixOf(Objects.requireNonNull(transitionName));
    }
}
