package org.libpetri.adk.bridge;

import static com.google.common.truth.Truth.assertThat;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;

class OtelEventStoreTest {

    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private Tracer tracer;

    @BeforeEach
    void setUp() {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter))
                .build();
        tracer = OpenTelemetrySdk.builder()
                .setTracerProvider(provider)
                .build()
                .getTracer("test");
    }

    @AfterEach
    void tearDown() {
        provider.close();
    }

    @Test
    void transition_completed_produces_span_with_correct_duration() {
        var store = new OtelEventStore(tracer);
        var end = Instant.parse("2026-05-23T12:00:00Z");
        var duration = Duration.ofMillis(150);

        store.append(new NetEvent.TransitionCompleted(end, "MyTransition", List.of(), duration));

        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(1);
        var span = spans.get(0);
        assertThat(span.getName()).isEqualTo("MyTransition");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        // span duration = end - start = duration
        long actualDurationNs = span.getEndEpochNanos() - span.getStartEpochNanos();
        assertThat(actualDurationNs).isEqualTo(duration.toNanos());
    }

    @Test
    void transition_failed_produces_error_span_with_exception_attributes() {
        var store = new OtelEventStore(tracer);
        var at = Instant.parse("2026-05-23T12:00:00Z");

        store.append(new NetEvent.TransitionFailed(at, "BrokenTransition",
                "model returned 500", "java.io.IOException"));

        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(1);
        var span = spans.get(0);
        assertThat(span.getName()).isEqualTo("BrokenTransition");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(span.getStatus().getDescription()).isEqualTo("model returned 500");
        assertThat(span.getAttributes().asMap()).containsAtLeast(
                AttributeKey.stringKey("exception.type"),
                "java.io.IOException",
                AttributeKey.stringKey("exception.message"),
                "model returned 500");
    }

    @Test
    void transition_timed_out_produces_span_with_deadline_attribute() {
        var store = new OtelEventStore(tracer);
        var at = Instant.parse("2026-05-23T12:00:00Z");

        store.append(new NetEvent.TransitionTimedOut(at, "SlowTransition",
                Duration.ofSeconds(5), Duration.ofSeconds(6)));

        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(1);
        var span = spans.get(0);
        assertThat(span.getName()).isEqualTo("SlowTransition");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(span.getAttributes().asMap()).containsAtLeast(
                AttributeKey.stringKey("libpetri.deadline"),
                "PT5S");
        // span duration = actualDuration
        long actualDurationNs = span.getEndEpochNanos() - span.getStartEpochNanos();
        assertThat(actualDurationNs).isEqualTo(Duration.ofSeconds(6).toNanos());
    }

    @Test
    void non_transition_lifecycle_events_produce_no_spans() {
        var store = new OtelEventStore(tracer);
        var at = Instant.parse("2026-05-23T12:00:00Z");

        store.append(new NetEvent.TransitionStarted(at, "T", List.of()));
        store.append(new NetEvent.TokenAdded(at, "p", Token.of("v")));
        store.append(new NetEvent.TokenRemoved(at, "p", Token.of("v")));
        store.append(new NetEvent.ExecutionStarted(at, "net", "exec-1"));
        store.append(new NetEvent.ExecutionCompleted(at, "net", "exec-1", Duration.ZERO));

        assertThat(exporter.getFinishedSpanItems()).isEmpty();
    }

    @Test
    void delegate_receives_every_event() {
        var captured = EventStore.inMemory();
        var store = new OtelEventStore(tracer, captured);
        var at = Instant.parse("2026-05-23T12:00:00Z");

        var e1 = new NetEvent.TransitionCompleted(at, "T", List.of(), Duration.ofMillis(10));
        var e2 = new NetEvent.TokenAdded(at, "p", Token.of("v"));
        var e3 = new NetEvent.TransitionFailed(at, "Bad", "err", "java.lang.RuntimeException");

        store.append(e1);
        store.append(e2);
        store.append(e3);

        assertThat(captured.events()).containsExactly(e1, e2, e3).inOrder();
        // Spans only for the two transition-lifecycle events (Completed + Failed).
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
    }

    @Test
    void chain_with_logging_event_store() {
        // Demonstrates the canonical observability chain pattern.
        var captured = EventStore.inMemory();
        var store = new OtelEventStore(tracer, EventStore.logging(captured));
        var at = Instant.parse("2026-05-23T12:00:00Z");

        store.append(new NetEvent.TransitionCompleted(at, "T", List.of(), Duration.ofMillis(5)));

        // Both OT and the underlying capture saw the event.
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(captured.events()).hasSize(1);
    }

    @Test
    void multiple_completed_events_produce_separate_spans_in_order() {
        var store = new OtelEventStore(tracer);
        var base = Instant.parse("2026-05-23T12:00:00Z");

        for (int i = 0; i < 5; i++) {
            store.append(new NetEvent.TransitionCompleted(
                    base.plusSeconds(i), "T" + i, List.of(), Duration.ofMillis(100)));
        }

        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(5);
        // SDK preserves insertion order in the exporter; assert names match.
        var names = spans.stream().map(s -> s.getName()).toList();
        assertThat(names).containsExactly("T0", "T1", "T2", "T3", "T4").inOrder();
    }

    @Test
    void bound_invocation_context_becomes_parent_of_transition_spans() throws Exception {
        // Open a known parent span, bind its Context onto the store, append a
        // transition event, and verify the emitted transition span is a child
        // of the parent span.
        var store = new OtelEventStore(tracer);
        Span parent = tracer.spanBuilder("invocation").startSpan();
        Context parentCtx = Context.current().with(parent);

        try (AutoCloseable ignored = store.bindInvocationContext(parentCtx)) {
            store.append(new NetEvent.TransitionCompleted(
                    Instant.parse("2026-05-23T12:00:00Z"),
                    "ChildTransition", List.of(), Duration.ofMillis(10)));
        }
        parent.end();

        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(2);
        var parentSpan = spans.stream().filter(s -> s.getName().equals("invocation")).findFirst().orElseThrow();
        var childSpan = spans.stream().filter(s -> s.getName().equals("ChildTransition")).findFirst().orElseThrow();
        assertThat(childSpan.getParentSpanId()).isEqualTo(parentSpan.getSpanId());
        assertThat(childSpan.getTraceId()).isEqualTo(parentSpan.getTraceId());
    }

    @Test
    void without_bound_context_spans_remain_well_formed_roots() {
        // Backward-compat: without bindInvocationContext, the store's slot
        // sits at Context.root() and spans are valid roots — same behaviour
        // as before this hook existed.
        var store = new OtelEventStore(tracer);

        store.append(new NetEvent.TransitionCompleted(
                Instant.parse("2026-05-23T12:00:00Z"),
                "RootTransition", List.of(), Duration.ofMillis(10)));

        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(1);
        // A root span has an invalid parent span id ("0000000000000000").
        assertThat(spans.get(0).getParentSpanContext().isValid()).isFalse();
    }

    @Test
    void close_restores_previous_binding_lifo() throws Exception {
        // Nested bindings restore the previous parent on close.
        var store = new OtelEventStore(tracer);
        Span outer = tracer.spanBuilder("outer").startSpan();
        Span inner = tracer.spanBuilder("inner").startSpan();

        try (AutoCloseable outerBind = store.bindInvocationContext(Context.current().with(outer))) {
            try (AutoCloseable innerBind = store.bindInvocationContext(Context.current().with(inner))) {
                store.append(new NetEvent.TransitionCompleted(
                        Instant.parse("2026-05-23T12:00:00Z"),
                        "DuringInner", List.of(), Duration.ofMillis(1)));
            }
            // Inner closed — back to outer's context.
            store.append(new NetEvent.TransitionCompleted(
                    Instant.parse("2026-05-23T12:00:01Z"),
                    "AfterInnerClose", List.of(), Duration.ofMillis(1)));
        }
        outer.end();
        inner.end();

        var spans = exporter.getFinishedSpanItems();
        var duringInner = spans.stream().filter(s -> s.getName().equals("DuringInner")).findFirst().orElseThrow();
        var afterInnerClose = spans.stream().filter(s -> s.getName().equals("AfterInnerClose")).findFirst().orElseThrow();
        var outerSpan = spans.stream().filter(s -> s.getName().equals("outer")).findFirst().orElseThrow();
        var innerSpan = spans.stream().filter(s -> s.getName().equals("inner")).findFirst().orElseThrow();
        assertThat(duringInner.getParentSpanId()).isEqualTo(innerSpan.getSpanId());
        assertThat(afterInnerClose.getParentSpanId()).isEqualTo(outerSpan.getSpanId());
    }

    @Test
    void out_of_order_fires_of_same_transition_each_get_own_span() {
        // Two fires of "T" complete in reverse order — both still get distinct spans
        // because we backfill from event timestamps, no correlation table needed.
        var store = new OtelEventStore(tracer);
        var t1Complete = Instant.parse("2026-05-23T12:00:01Z");
        var t2Complete = Instant.parse("2026-05-23T12:00:02Z");

        // Note: t2 completes BEFORE t1 in append order (out-of-order)
        store.append(new NetEvent.TransitionCompleted(t2Complete, "T", List.of(), Duration.ofMillis(50)));
        store.append(new NetEvent.TransitionCompleted(t1Complete, "T", List.of(), Duration.ofMillis(900)));

        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(2);
        // Each span has its own duration derived from its own event — no mixing.
        var durations = spans.stream().mapToLong(s -> s.getEndEpochNanos() - s.getStartEpochNanos())
                .sorted().toArray();
        assertThat(durations[0]).isEqualTo(Duration.ofMillis(50).toNanos());
        assertThat(durations[1]).isEqualTo(Duration.ofMillis(900).toNanos());
    }
}
