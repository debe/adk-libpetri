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
    void transition_failed_errors_the_flowable() {
        var bridge = new EventStoreToFlowableBridge(EVENT_OUT, EventStore.noop());
        TestSubscriber<Event> sub = bridge.asFlowable().test();

        bridge.append(new NetEvent.TransitionFailed(
                Instant.now(), "T_llm_call", "model returned 500", "java.io.IOException"));

        sub.assertError(RuntimeException.class);
        sub.assertError(t -> "Transition T_llm_call failed: model returned 500 (java.io.IOException)"
                .equals(t.getMessage()));
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
