package org.libpetri.adk.bridge;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import java.time.Instant;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.libpetri.event.EventStore;
import org.libpetri.event.NetEvent;

/**
 * {@link EventStore} decorator that emits one OpenTelemetry span per
 * transition fire — the canonical observability pattern for
 * libpetri-based runtimes, layered onto libpetri's
 * {@link EventStore} decorator chain.
 *
 * <p>Span lifecycle is derived <b>from the event timestamps alone</b>
 * — no in-flight state, no correlation table. Each
 * {@link NetEvent.TransitionCompleted} produces a finished span
 * spanning {@code [completed.timestamp - duration, completed.timestamp]}.
 * {@link NetEvent.TransitionFailed} produces a span at the failure
 * timestamp with {@link StatusCode#ERROR} and the exception recorded.
 * {@link NetEvent.TransitionTimedOut} produces a span over
 * {@code [timedOut - actualDuration, timedOut]} with the deadline as
 * an attribute. Other {@link NetEvent} variants are ignored (the
 * delegate still receives them).
 *
 * <p>Stateless span backfilling avoids the out-of-order-completion
 * correlation problem that a per-fire start/end mapping would have
 * (two fires of the same transition can complete in any order).
 *
 * <h2>Typical chain</h2>
 * <pre>{@code
 * new OtelEventStore(tracer,
 *     EventStore.logging(             // logs every NetEvent
 *         EventStore.inMemory()))      // captures for later inspection
 * }</pre>
 *
 * <p>The decorator never blocks the orchestrator — span creation +
 * end are non-blocking OT operations.
 *
 * <h2>Parent-context binding (root-span hookup)</h2>
 *
 * <p>{@link EventStore#append} runs on the orchestrator thread, not on
 * the caller that triggered the work — so {@link Context#current()} at
 * span-creation time would not see any parent set by the caller. Use
 * {@link #bindInvocationContext(Context)} to thread a parent
 * {@link Context} across that thread boundary: every transition span
 * created while a context is bound is attached as a child of it.
 *
 * <p>Two usage patterns, picked by the caller's lifetime model:
 * <ul>
 *   <li><b>Per-invocation</b> — the
 *       {@link org.libpetri.adk.runner.PetriAgent} adapter overload that
 *       accepts a {@link io.opentelemetry.api.trace.Tracer} +
 *       {@code OtelEventStore} opens a {@code petri.invocation.<agent>}
 *       span around each {@code runAsyncImpl(...)} call and binds it
 *       automatically. Right for turn-based text agents where each
 *       invocation is its own trace.</li>
 *   <li><b>Session-long</b> — the caller opens an externally-owned root
 *       span (e.g. a {@code VoiceWorkflow} span at WebSocket-open time)
 *       and binds it for the runner's whole lifetime:
 *       <pre>{@code
 * var rootSpan = tracer.spanBuilder("VoiceWorkflow").startSpan();
 * var binding = otelEventStore.bindInvocationContext(
 *         Context.current().with(rootSpan));
 * // ... runner lives here, side-channel transitions attach to rootSpan ...
 * binding.close();
 * rootSpan.end();
 *       }</pre>
 *       Right for BIDI/Live sessions where background transitions fire
 *       between turns (silence-recovery timers, voice-activity signals)
 *       and need a parent.</li>
 * </ul>
 *
 * <p>Binding is sequential by design — overlapping calls clobber each
 * other. ADK's turn-based per-session invocation model satisfies that
 * constraint. With nothing bound the slot stays at {@link Context#root()}
 * and spans remain well-formed roots (backward compatible).
 */
public final class OtelEventStore implements EventStore {

    private final Tracer tracer;
    private final EventStore delegate;
    private final AtomicReference<Context> invocationContext =
            new AtomicReference<>(Context.root());

    public OtelEventStore(Tracer tracer, EventStore delegate) {
        this.tracer = Objects.requireNonNull(tracer, "tracer");
        this.delegate = Objects.requireNonNull(delegate, "delegate");
    }

    /** Convenience: chain on top of {@link EventStore#noop()}. */
    public OtelEventStore(Tracer tracer) {
        this(tracer, EventStore.noop());
    }

    /**
     * Bind a parent {@link Context} for all transition spans emitted
     * until the returned handle is closed. The previous binding is
     * restored on close (LIFO). Intended for the per-ADK-invocation
     * root span — see the class javadoc.
     */
    public AutoCloseable bindInvocationContext(Context ctx) {
        Objects.requireNonNull(ctx, "ctx");
        Context previous = invocationContext.getAndSet(ctx);
        return () -> invocationContext.set(previous);
    }

    @Override
    public void append(NetEvent event) {
        switch (event) {
            case NetEvent.TransitionCompleted c ->
                    emitSpan(c.transitionName(), c.timestamp().minus(c.duration()), c.timestamp(),
                            StatusCode.OK, null, null, null);
            case NetEvent.TransitionFailed f ->
                    emitSpan(f.transitionName(), f.timestamp(), f.timestamp(),
                            StatusCode.ERROR, f.errorMessage(), f.exceptionType(), null);
            case NetEvent.TransitionTimedOut t ->
                    emitSpan(t.transitionName(),
                            t.timestamp().minus(t.actualDuration()),
                            t.timestamp(),
                            StatusCode.ERROR,
                            "deadline exceeded",
                            "org.libpetri.event.NetEvent.TransitionTimedOut",
                            t.deadline().toString());
            default -> { /* not a transition lifecycle event — no span */ }
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

    private void emitSpan(String name,
                          Instant start,
                          Instant end,
                          StatusCode status,
                          String errorMessage,
                          String exceptionType,
                          String deadline) {
        var builder = tracer.spanBuilder(name)
                .setParent(invocationContext.get())
                .setStartTimestamp(start.getEpochSecond() * 1_000_000_000L + start.getNano(),
                        TimeUnit.NANOSECONDS);
        Span span = builder.startSpan();
        try {
            if (status != null) {
                if (status == StatusCode.ERROR && errorMessage != null) {
                    span.setStatus(StatusCode.ERROR, errorMessage);
                } else {
                    span.setStatus(status);
                }
            }
            if (exceptionType != null) {
                span.setAttribute("exception.type", exceptionType);
            }
            if (errorMessage != null) {
                span.setAttribute("exception.message", errorMessage);
            }
            if (deadline != null) {
                span.setAttribute("libpetri.deadline", deadline);
            }
        } finally {
            span.end(end.getEpochSecond() * 1_000_000_000L + end.getNano(),
                    TimeUnit.NANOSECONDS);
        }
    }
}
