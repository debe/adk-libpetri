package org.libpetri.adk.bridge;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.events.Event;
import io.reactivex.rxjava3.subscribers.TestSubscriber;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;
import org.libpetri.adk.colours.AdkColours;

class EventStoreToFlowableBridgeTest {

    private static final Place<Event> EVENT_OUT = AdkColours.EVENT_OUT;

    @Test
    void forwards_token_added_on_event_out_place_as_flowable_event() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        TestSubscriber<Event> sub = bridge.asFlowable().test();

        var adkEvent = Event.builder().invocationId("inv-1").author("agent").build();
        bridge.append(new NetEvent.TokenAdded(Instant.now(), EVENT_OUT.name(), Token.of(adkEvent)));

        sub.assertValueCount(1);
        sub.assertValue(adkEvent);
    }

    @Test
    void ignores_token_added_on_other_places() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        TestSubscriber<Event> sub = bridge.asFlowable().test();

        bridge.append(new NetEvent.TokenAdded(Instant.now(), "someOtherPlace", Token.of("not-an-event")));

        sub.assertNoValues();
        sub.assertNotComplete();
    }

    @Test
    void ignores_token_added_on_event_out_when_value_is_not_an_event() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        TestSubscriber<Event> sub = bridge.asFlowable().test();

        // Could happen if a misconfigured transition produces a wrong-typed token —
        // the bridge ignores it rather than crashing the stream.
        bridge.append(new NetEvent.TokenAdded(Instant.now(), EVENT_OUT.name(), Token.of("string-not-event")));

        sub.assertNoValues();
        sub.assertNoErrors();
    }

    @Test
    void execution_completed_completes_the_flowable() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        TestSubscriber<Event> sub = bridge.asFlowable().test();

        bridge.append(new NetEvent.ExecutionCompleted(
                Instant.now(), "test-net", "exec-1", Duration.ofMillis(42)));

        sub.assertComplete();
    }

    @Test
    void transition_failed_is_published_on_the_failure_signal() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        TestSubscriber<Throwable> failures = bridge.failureSignal().test();

        bridge.append(new NetEvent.TransitionFailed(
                Instant.now(), "T_llm_call", "model returned 500", "java.io.IOException"));

        failures.assertValueCount(1);
        // Identity intact, not flattened into a message a consumer has to regex.
        var f = (TransitionFailure) failures.values().get(0);
        assertThat(f.transitionName()).isEqualTo("T_llm_call");
        assertThat(f.kind()).isEqualTo(TransitionFailure.Kind.ACTION_THREW);
        assertThat(f.exceptionType()).hasValue("java.io.IOException");
        assertThat(f.deadline()).isEmpty();
        assertThat(f.getMessage())
                .isEqualTo("Transition T_llm_call failed: model returned 500 (java.io.IOException)");
        // Non-terminal: the signal stays open for the next failure.
        failures.assertNotComplete();
        failures.assertNoErrors();
    }

    /**
     * The regression that motivated the failure signal. This used to call
     * {@code events.onError}, which is terminal on a per-session processor, so
     * one failed transition ended the egress for every later turn even though
     * libpetri had contained the failure and the net was still running.
     */
    /**
     * A blown deadline is a failure the caller must hear about. It used to fall
     * into the switch's default branch and emit nothing, so a turn whose only
     * terminal event was going to come from the timed-out transition waited
     * forever. PersistStateSubnet ships a 5s deadline, so this is reachable.
     */
    @Test
    void a_deadline_timeout_is_also_published_on_the_failure_signal() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        TestSubscriber<Throwable> failures = bridge.failureSignal().test();

        bridge.append(new NetEvent.TransitionTimedOut(
                Instant.now(), "PersistState_Persist",
                Duration.ofSeconds(5), Duration.ofSeconds(7)));

        failures.assertValueCount(1);
        var f = (TransitionFailure) failures.values().get(0);
        assertThat(f.transitionName()).isEqualTo("PersistState_Persist");
        assertThat(f.kind()).isEqualTo(TransitionFailure.Kind.DEADLINE_EXCEEDED);
        assertThat(f.deadline()).hasValue(Duration.ofSeconds(5));
        assertThat(f.actualDuration()).hasValue(Duration.ofSeconds(7));
        // Nothing was thrown, so there is no exception type to report.
        assertThat(f.exceptionType()).isEmpty();
    }

    @Test
    void a_transition_failure_does_not_kill_the_event_stream() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        TestSubscriber<Event> sub = bridge.asFlowable().test();

        bridge.append(new NetEvent.TransitionFailed(
                Instant.now(), "T_llm_call", "model returned 500", "java.io.IOException"));

        sub.assertNoErrors();
        sub.assertNotComplete();

        // ... and an Event produced after the failure still reaches subscribers.
        bridge.append(new NetEvent.TokenAdded(
                Instant.now(), EVENT_OUT.name(), Token.of(Event.builder().invocationId("inv-2").author("agent").build())));

        sub.assertNoErrors();
        sub.assertValueCount(1);
    }

    @Test
    void a_late_subscriber_after_a_failure_still_receives_events() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());

        // Failure happens with nobody attached, as between ADK turns.
        bridge.append(new NetEvent.TransitionFailed(
                Instant.now(), "T_llm_call", "model returned 500", "java.io.IOException"));

        TestSubscriber<Event> late = bridge.asFlowable().test();
        bridge.append(new NetEvent.TokenAdded(
                Instant.now(), EVENT_OUT.name(), Token.of(Event.builder().invocationId("inv-3").author("agent").build())));

        late.assertNoErrors();
        late.assertValueCount(1);
    }

    @Test
    void multiple_events_arrive_in_order() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        TestSubscriber<Event> sub = bridge.asFlowable().test();

        var e1 = Event.builder().invocationId("inv-1").author("agent").id("e1").build();
        var e2 = Event.builder().invocationId("inv-1").author("agent").id("e2").build();
        var e3 = Event.builder().invocationId("inv-1").author("agent").id("e3").build();

        bridge.append(new NetEvent.TokenAdded(Instant.now(), EVENT_OUT.name(), Token.of(e1)));
        bridge.append(new NetEvent.TokenAdded(Instant.now(), EVENT_OUT.name(), Token.of(e2)));
        bridge.append(new NetEvent.TokenAdded(Instant.now(), EVENT_OUT.name(), Token.of(e3)));

        sub.assertValueCount(3);
        assertThat(sub.values().stream().map(Event::id).toList())
                .containsExactly("e1", "e2", "e3")
                .inOrder();
    }

    @Test
    void delegate_receives_every_event() {
        var captured = EventStore.inMemory();
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, captured);

        var n1 = new NetEvent.TokenAdded(Instant.now(), EVENT_OUT.name(),
                Token.of(Event.builder().invocationId("inv").author("agent").build()));
        var n2 = new NetEvent.TokenAdded(Instant.now(), "other", Token.of("x"));
        var n3 = new NetEvent.ExecutionCompleted(Instant.now(), "n", "id", Duration.ZERO);

        bridge.append(n1);
        bridge.append(n2);
        bridge.append(n3);

        assertThat(captured.events()).containsExactly(n1, n2, n3).inOrder();
    }

    @Test
    void as_flowable_supports_multiple_subscribers() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());

        TestSubscriber<Event> s1 = bridge.asFlowable().test();
        TestSubscriber<Event> s2 = bridge.asFlowable().test();

        var adkEvent = Event.builder().invocationId("inv-1").author("agent").build();
        bridge.append(new NetEvent.TokenAdded(Instant.now(), EVENT_OUT.name(), Token.of(adkEvent)));

        s1.assertValue(adkEvent);
        s2.assertValue(adkEvent);
    }

    @Test
    void events_emitted_before_subscription_are_not_replayed() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        var early = Event.builder().invocationId("inv").author("agent").id("early").build();
        bridge.append(new NetEvent.TokenAdded(Instant.now(), EVENT_OUT.name(), Token.of(early)));

        TestSubscriber<Event> sub = bridge.asFlowable().test();
        // PublishProcessor is hot — late subscribers miss what already passed.
        sub.assertNoValues();

        var live = Event.builder().invocationId("inv").author("agent").id("live").build();
        bridge.append(new NetEvent.TokenAdded(Instant.now(), EVENT_OUT.name(), Token.of(live)));
        sub.assertValue(live);
    }

    @Test
    void unrelated_net_events_are_silently_passed_through() {
        var captured = EventStore.inMemory();
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, captured);
        TestSubscriber<Event> sub = bridge.asFlowable().test();

        bridge.append(new NetEvent.TransitionStarted(Instant.now(), "T_x", List.of()));
        bridge.append(new NetEvent.TokenRemoved(Instant.now(), "p", Token.of("x")));

        sub.assertNoValues();
        sub.assertNoErrors();
        assertThat(captured.events()).hasSize(2);
    }
}
