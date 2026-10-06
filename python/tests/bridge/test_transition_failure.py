"""``TransitionFailure``, ``Kind`` and ``split_error`` (Java's ``TransitionFailure`` record)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adk_libpetri.bridge.transition_failure import Kind, TransitionFailure, split_error

from ._fakes import completed, failed, started, timed_out, token_added


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("ValueError: boom", ("ValueError", "boom")),
        ("OSError: model returned 500", ("OSError", "model returned 500")),
        ("mod.CustomError: a: b", ("mod.CustomError", "a: b")),
        ("boom", ("Error", "boom")),
        ("", ("Error", "")),
        # A head with a space is prose, not a type name.
        ("model said: no", ("Error", "model said: no")),
        # A bracketed head (e.g. "[T] ...") is not a type name either.
        ("[T_x]: boom", ("Error", "[T_x]: boom")),
        (": boom", ("Error", ": boom")),
    ],
)
def test_split_error(error: str, expected: tuple[str, str]) -> None:
    assert split_error(error) == expected


def test_from_failed_event_keeps_identity() -> None:
    f = TransitionFailure.from_event(failed("LlmStep_LlmCall", "ValueError: boom", at=1_000))
    assert f is not None
    assert f.transition_name == "LlmStep_LlmCall"
    assert f.kind is Kind.ACTION_THREW
    assert f.exception_type == "ValueError"
    assert f.error_message == "boom"
    assert f.occurred_at == datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert str(f) == "Transition LlmStep_LlmCall failed: boom (ValueError)"


def test_from_failed_event_without_a_type_prefix() -> None:
    f = TransitionFailure.from_event(failed("T", "something broke"))
    assert f is not None
    assert f.exception_type == "Error"
    assert f.error_message == "something broke"


def test_from_timed_out_event() -> None:
    f = TransitionFailure.from_event(timed_out("PersistState_Persist", at=2_500))
    assert f is not None
    assert f.kind is Kind.DEADLINE_EXCEEDED
    assert f.exception_type is None
    assert f.error_message is None
    assert f.occurred_at == datetime.fromtimestamp(2.5, tz=UTC)
    assert str(f) == "Transition PersistState_Persist exceeded its deadline"


@pytest.mark.parametrize("event", [started("T"), completed("T"), token_added("p", "v")])
def test_non_failure_events_give_none(event: object) -> None:
    assert TransitionFailure.from_event(event) is None


def test_is_a_runtime_error() -> None:
    f = TransitionFailure.from_event(failed("T", "ValueError: boom"))
    assert f is not None
    with pytest.raises(RuntimeError, match="Transition T failed"):
        raise f


@pytest.mark.parametrize(
    ("name", "prefix"),
    [("a/b/T", "a/b"), ("inst/T", "inst"), ("T", None)],
)
def test_instance_prefix(name: str, prefix: str | None) -> None:
    f = TransitionFailure.from_event(timed_out(name))
    assert f is not None
    assert f.transition_name == name
    assert f.instance_prefix == prefix


def test_kind_values() -> None:
    assert {k.name for k in Kind} == {"ACTION_THREW", "DEADLINE_EXCEEDED"}
