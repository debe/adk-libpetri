"""Port of Java's ``OtelEventStoreTest``.

libpetri-py's ``TransitionCompleted`` carries no duration, so a span's start
comes from the matching ``TransitionStarted``; tests feed both where Java fed
one completed event with a duration.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, Tracer

from adk_libpetri._spec import NetSpec, TransitionSpec, one
from adk_libpetri._spec import Place as SpecPlace
from adk_libpetri.bridge import SUBNET_ATTRIBUTE, TRANSITION_ATTRIBUTE, OtelEventStore
from adk_libpetri.bridge.otel_event_store import context_with_span
from adk_libpetri.subnet import llm_agent, llm_step, transfer_router

from ._fakes import (
    LoggingStore,
    RecordingStore,
    completed,
    execution_completed,
    execution_started,
    failed,
    started,
    timed_out,
    token_added,
    token_removed,
)

AT = 1_779_537_600_000  # 2026-05-23T12:00:00Z in epoch ms
MS = 1_000_000


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer(exporter: InMemorySpanExporter) -> Iterator[Tracer]:
    # A private provider, never the global one.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("test")
    provider.shutdown()


def _store(tracer: Tracer, **kw: object) -> OtelEventStore:
    return OtelEventStore(tracer, RecordingStore(), **kw)  # type: ignore[arg-type]


def _span(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    return next(s for s in exporter.get_finished_spans() if s.name == name)


def _duration_ns(span: ReadableSpan) -> int:
    assert span.start_time is not None and span.end_time is not None
    return span.end_time - span.start_time


def _ctx_of(span: trace.Span) -> otel_context.Context:
    return trace.set_span_in_context(span, otel_context.Context())


def test_spans_name_the_contributing_subnet_when_the_net_is_known(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    """Each span names the subnet composed into the top-level net.

    LlmStep's transitions roll up to the LlmAgent that composes them; a
    transition declared on the top-level net itself gets no subnet attribute.
    """
    own = TransitionSpec("App_Own", (one(SpecPlace("app_in", str)),))
    net = NetSpec.compose("app", llm_agent.DEF, transfer_router.def_(["billing"]), own)
    store = _store(tracer, subnet_of=net.subnet_of)

    names = [
        llm_step.Transitions.LLM_CALL,
        llm_agent.Transitions.RE_ASK,
        transfer_router.Transitions.DEMUX,
        "App_Own",
    ]
    for name in names:
        store.append(completed(name, AT))

    subnet_by_transition: dict[str, object] = {}
    for span in exporter.get_finished_spans():
        assert span.attributes is not None
        assert span.attributes[TRANSITION_ATTRIBUTE] == span.name
        subnet_by_transition[span.name] = span.attributes.get(SUBNET_ATTRIBUTE)
    assert subnet_by_transition == {
        llm_step.Transitions.LLM_CALL: "LlmAgent",
        llm_agent.Transitions.RE_ASK: "LlmAgent",
        transfer_router.Transitions.DEMUX: "TransferRouter",
        "App_Own": None,
    }


def test_without_subnet_of_no_subnet_attribute(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    _store(tracer).append(completed("T", AT))
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert SUBNET_ATTRIBUTE not in span.attributes


def test_transition_completed_produces_span_with_correct_duration(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)

    store.append(started("MyTransition", AT - 150))
    store.append(completed("MyTransition", AT))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "MyTransition"
    assert span.status.status_code is StatusCode.OK
    assert span.start_time == (AT - 150) * MS
    assert span.end_time == AT * MS
    assert _duration_ns(span) == 150 * MS


def test_completed_without_a_start_is_a_zero_length_span(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    # Python-specific: no TransitionStarted seen (e.g. store attached mid-fire).
    _store(tracer).append(completed("T", AT))
    (span,) = exporter.get_finished_spans()
    assert _duration_ns(span) == 0


def test_transition_failed_records_an_exception_event_on_an_error_span(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)

    store.append(started("BrokenTransition", AT - 20))
    store.append(failed("BrokenTransition", "OSError: model returned 500", AT))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "BrokenTransition"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "model returned 500"
    assert _duration_ns(span) == 20 * MS
    # OpenTelemetry models a failure as an "exception" span event carrying the
    # exception.* attributes, not as attributes on the span itself.
    assert len(span.events) == 1
    ev = span.events[0]
    assert ev.name == "exception"
    assert ev.timestamp == AT * MS
    assert dict(ev.attributes or {}) == {
        "exception.type": "OSError",
        "exception.message": "model returned 500",
    }


def test_transition_failed_without_a_type_prefix_reports_error(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    _store(tracer).append(failed("T", "something broke", AT))
    (span,) = exporter.get_finished_spans()
    assert span.status.description == "something broke"
    assert dict(span.events[0].attributes or {})["exception.type"] == "Error"


def test_transition_timed_out_produces_error_span(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)

    store.append(started("SlowTransition", AT - 6_000))
    store.append(timed_out("SlowTransition", AT))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "SlowTransition"
    assert span.status.status_code is StatusCode.ERROR
    # libpetri-py's TransitionTimedOut carries no deadline, so there is no
    # libpetri.deadline attribute (Java sets one).
    assert len(span.events) == 1
    assert span.events[0].name == "exception"
    assert dict(span.events[0].attributes or {}) == {
        "exception.type": "TransitionTimedOut",
        "exception.message": "deadline exceeded",
    }
    # span duration = actual duration
    assert _duration_ns(span) == 6_000 * MS


def test_non_transition_lifecycle_events_produce_no_spans(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)

    store.append(started("T", AT))
    store.append(token_added("p", "v", AT))
    store.append(token_removed("p", "v", AT))
    store.append(execution_started(AT))
    store.append(execution_completed(AT))

    assert exporter.get_finished_spans() == ()


def test_delegate_receives_every_event(tracer: Tracer, exporter: InMemorySpanExporter) -> None:
    captured = RecordingStore()
    store = OtelEventStore(tracer, captured)

    e0 = started("T", AT - 10)
    e1 = completed("T", AT)
    e2 = token_added("p", "v", AT)
    e3 = failed("Bad", "RuntimeError: err", AT)
    e4 = timed_out("Slow", AT)
    for e in (e0, e1, e2, e3, e4):
        store.append(e)

    assert captured.recorded == [e0, e1, e2, e3, e4]
    # Spans only for transition outcomes (Completed, Failed, TimedOut).
    assert len(exporter.get_finished_spans()) == 3


def test_events_delegates(tracer: Tracer) -> None:
    captured = RecordingStore()
    store = OtelEventStore(tracer, captured)
    store.append(started("T", AT))
    store.append(token_added("p", "v", AT))

    assert store.events() == captured.recorded
    assert store.events(type="TokenAdded") == [captured.recorded[1]]


def test_captures_tokens_follows_the_delegate(tracer: Tracer) -> None:
    # Python-specific: token capture is requested if anything downstream needs it.
    assert OtelEventStore(tracer, RecordingStore()).captures_tokens is True
    assert OtelEventStore(tracer, LoggingStore(RecordingStore())).captures_tokens is False


def test_a_delegate_is_required(tracer: Tracer) -> None:
    with pytest.raises(TypeError):
        OtelEventStore(tracer, None)


def test_chain_with_logging_event_store(tracer: Tracer, exporter: InMemorySpanExporter) -> None:
    # The canonical observability chain: OTel -> logging -> capture.
    captured = RecordingStore()
    logging = LoggingStore(captured)
    store = OtelEventStore(tracer, logging)

    store.append(started("T", AT - 5))
    store.append(completed("T", AT))

    assert len(exporter.get_finished_spans()) == 1
    assert len(captured.recorded) == 2
    assert logging.lines == ["TransitionStarted T", "TransitionCompleted T"]


def test_multiple_completed_events_produce_separate_spans_in_order(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)

    for i in range(5):
        end = AT + i * 1_000
        store.append(started(f"T{i}", end - 100))
        store.append(completed(f"T{i}", end))

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["T0", "T1", "T2", "T3", "T4"]
    assert all(_duration_ns(s) == 100 * MS for s in spans)


def test_bound_invocation_context_becomes_parent_of_transition_spans(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)
    parent = tracer.start_span("invocation")

    with store.bind_invocation_context(_ctx_of(parent)):
        store.append(completed("ChildTransition", AT))
    parent.end()

    parent_span = _span(exporter, "invocation")
    child = _span(exporter, "ChildTransition")
    assert child.parent is not None and parent_span.context is not None
    assert child.parent.span_id == parent_span.context.span_id
    assert child.context is not None
    assert child.context.trace_id == parent_span.context.trace_id


def test_context_with_span_helper_parents_spans(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)
    parent = tracer.start_span("invocation")
    with store.bind_invocation_context(context_with_span(parent)):
        store.append(completed("Child", AT))
    parent.end()

    child = _span(exporter, "Child")
    assert child.parent is not None
    assert child.parent.span_id == parent.get_span_context().span_id


def test_without_bound_context_spans_remain_well_formed_roots(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)
    # The caller's current span must not leak in: append runs on a libpetri
    # thread in production, so the store ignores the ambient context.
    with tracer.start_as_current_span("ambient"):
        store.append(completed("RootTransition", AT))

    span = _span(exporter, "RootTransition")
    assert span.parent is None


def test_close_restores_previous_binding_lifo(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)
    outer = tracer.start_span("outer")
    inner = tracer.start_span("inner")

    with store.bind_invocation_context(_ctx_of(outer)):
        with store.bind_invocation_context(_ctx_of(inner)):
            store.append(completed("DuringInner", AT))
        # Inner closed: back to outer's context.
        store.append(completed("AfterInnerClose", AT + 1_000))
    # Outer closed: back to no parent.
    store.append(completed("AfterOuterClose", AT + 2_000))
    outer.end()
    inner.end()

    assert _span(exporter, "DuringInner").parent.span_id == inner.get_span_context().span_id  # type: ignore[union-attr]
    assert _span(exporter, "AfterInnerClose").parent.span_id == outer.get_span_context().span_id  # type: ignore[union-attr]
    assert _span(exporter, "AfterOuterClose").parent is None


def test_bind_restores_on_exception(tracer: Tracer, exporter: InMemorySpanExporter) -> None:
    store = _store(tracer)
    parent = tracer.start_span("p")
    with pytest.raises(RuntimeError), store.bind_invocation_context(_ctx_of(parent)):
        raise RuntimeError("boom")
    store.append(completed("After", AT))
    parent.end()
    assert _span(exporter, "After").parent is None


def test_set_invocation_context_is_sticky_and_returns_previous(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)
    turn1 = tracer.start_span("turn1")
    turn2 = tracer.start_span("turn2")

    first_previous = store.set_invocation_context(_ctx_of(turn1))
    store.append(completed("A", AT))
    store.append(completed("B", AT + 1))
    previous = store.set_invocation_context(_ctx_of(turn2))
    store.append(completed("C", AT + 2))
    store.set_invocation_context(first_previous)
    store.append(completed("D", AT + 3))
    turn1.end()
    turn2.end()

    assert trace.get_current_span(previous).get_span_context() == turn1.get_span_context()
    t1 = turn1.get_span_context().span_id
    assert _span(exporter, "A").parent.span_id == t1  # type: ignore[union-attr]
    assert _span(exporter, "B").parent.span_id == t1  # type: ignore[union-attr]
    assert _span(exporter, "C").parent.span_id == turn2.get_span_context().span_id  # type: ignore[union-attr]
    assert _span(exporter, "D").parent is None


def test_concurrent_fires_of_same_transition_each_get_own_span(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    # Java backfills each span from its own event's duration. Python pairs
    # starts FIFO per transition name: both fires still get distinct spans.
    store = _store(tracer)

    store.append(started("T", AT))
    store.append(started("T", AT + 100))
    store.append(completed("T", AT + 50))
    store.append(completed("T", AT + 1_000))

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert sorted(_duration_ns(s) for s in spans) == [50 * MS, 900 * MS]


def test_starts_are_paired_per_transition_name(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    store = _store(tracer)
    store.append(started("A", AT))
    store.append(started("B", AT + 10))
    store.append(completed("B", AT + 30))
    store.append(failed("A", "ValueError: x", AT + 100))

    assert _duration_ns(_span(exporter, "A")) == 100 * MS
    assert _duration_ns(_span(exporter, "B")) == 20 * MS
