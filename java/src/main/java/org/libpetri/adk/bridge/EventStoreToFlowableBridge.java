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
            case NetEvent.ExecutionCompleted ignored -> events.onComplete();
            case NetEvent.TransitionFailed failed -> events.onError(
                    new RuntimeException("Transition " + failed.transitionName() + " failed: "
                            + failed.errorMessage() + " (" + failed.exceptionType() + ")"));
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

    /** Subscribers see every Event token produced into the configured output place. */
    public Flowable<Event> asFlowable() {
        return events.hide();
    }
}
