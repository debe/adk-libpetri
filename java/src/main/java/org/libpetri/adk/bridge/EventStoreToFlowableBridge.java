package org.libpetri.adk.bridge;

import com.google.adk.events.Event;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.processors.PublishProcessor;
import java.util.List;
import java.util.Objects;
import org.libpetri.core.Place;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;

/**
 * {@link EventStore} decorator that exposes a place's token stream as an
 * RxJava {@link Flowable}.
 *
 * <p>The bridge watches every {@link NetEvent.TokenAdded} for the
 * configured output place; each matching token's value is forwarded to a
 * {@link PublishProcessor} that subscribers consume via
 * {@link #asFlowable()}. {@link NetEvent.ExecutionCompleted} completes
 * the stream; {@link NetEvent.TransitionFailed} surfaces as an
 * {@code onError}.
 *
 * <p>This bridge is purely additive — every event still flows to the
 * supplied {@code delegate}, so it can be chained with other decorators
 * (OT spans, debug session recording, etc.) without interference.
 * The delegate is required; callers who genuinely want no downstream
 * chain must pass {@link EventStore#noop()} explicitly.
 *
 * <p>The interaction model with the running net is still env-place
 * injection and EventStore observation; this class is just a convenience
 * for callers who want an RxJava-shaped output.
 */
public final class EventStoreToFlowableBridge implements EventStore {

    private final Place<Event> eventOutPlace;
    private final EventStore delegate;
    private final PublishProcessor<Event> events = PublishProcessor.create();

    /**
     * Transition failures, as a <b>non-terminal</b> signal.
     *
     * <p>This used to be {@code events.onError(...)}, which was wrong in a way
     * that only shows up on a long-lived net. {@code events} is one
     * {@code PublishProcessor} per {@link org.libpetri.adk.runner.PetriRunner},
     * so per session; {@code onError} is terminal, so a single failing
     * transition ended the session's whole egress. libpetri contains an action
     * failure to its own transition and keeps the orchestrator running
     * (EXEC-031), and we were undoing exactly that containment one layer up:
     * the net went on firing while every later turn received nothing.
     *
     * <p>Failures are therefore published here instead, as {@code onNext} on a
     * separate stream that never terminates the event stream. Deciding what a
     * failure means is the consumer's job, because only the consumer knows the
     * unit of work: the ADK adapter fails the turn that was in flight and
     * leaves the session usable.
     */
    private final PublishProcessor<Throwable> failures = PublishProcessor.create();

    /** Bridge chained on top of another event store. */
    public EventStoreToFlowableBridge(Place<Event> eventOutPlace, EventStore delegate) {
        this.eventOutPlace = Objects.requireNonNull(eventOutPlace, "eventOutPlace");
        this.delegate = Objects.requireNonNull(delegate, "delegate");
    }

    @Override
    public void append(NetEvent event) {
        switch (event) {
            case NetEvent.TokenAdded tokenAdded when eventOutPlace.name().equals(tokenAdded.placeName()) -> {
                Object value = tokenAdded.token().value();
                if (value instanceof Event adkEvent) {
                    events.onNext(adkEvent);
                }
            }
            case NetEvent.ExecutionCompleted ignored -> {
                events.onComplete();
                failures.onComplete();
            }
            case NetEvent.TransitionFailed failed -> failures.onNext(TransitionFailure.of(failed));
            // A blown deadline is a failure too, and it is reachable from a shipped
            // subnet (PersistStateSubnet gives its persist step a 5s deadline). It
            // used to fall through to default and produce no signal at all, so that
            // turn waited for a terminal event that was never coming.
            case NetEvent.TransitionTimedOut timedOut -> failures.onNext(TransitionFailure.of(timedOut));
            default -> { /* not relevant to bridge */ }
        }
        delegate.append(event);
    }

    @Override
    public List<NetEvent> events() {
        return delegate.events();
    }

    @Override
    public boolean isEnabled() {
        return true;
    }

    /**
     * Transition failures observed on this net, hot and non-terminating.
     *
     * <p>One {@code onNext} per failed transition. The stream completes only
     * when the net does. A consumer scoping work to a turn should merge this
     * for the life of that turn and drop it afterwards, so a failure fails the
     * turn without outliving it; see {@code PetriAgent.runAsyncImpl}.
     *
     * <p>This is a control signal for the caller's unit of work, not the
     * observability channel. Observability stays on the {@link EventStore}
     * decorator chain, which sees every {@code TransitionFailed} regardless of
     * whether anyone subscribes here.
     */
    public Flowable<Throwable> failureSignal() {
        return failures.hide();
    }

    /** Subscribers see every Event token produced into the configured output place. */
    public Flowable<Event> asFlowable() {
        return events.hide();
    }
}
