"""ADK sample ``workflows/retry``: a flaky node retried up to five times.

``RetryConfig(max_attempts=5, initial_delay=1)``. Recorded trace
``tests/go.json`` mocks ``random.random`` as ``[0.5, 0.5, 0.8]``:
attempts 1 and 2 fail with ``HTTPError`` 500 (each followed by an error event),
attempt 3 outputs ``"sunny"``, and ``report_weather`` says "The weather is
sunny". Every attempt keeps the node path ``root_agent@1/get_weather@1`` and
says its own ``ctx.attempt_count``. No model.

The compiled net keeps ``retry_config`` on the node: ADK's node runner retries
inside the ``Wf_get_weather_Run`` transition (attempt count, node path, backoff
and jitter as natively), so there are no per-attempt transitions. The backoff is
real (1 s, then 2 s), so each run of the trace takes about 3 s.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest
from google.adk.runners import InMemoryRunner
from google.adk.workflow import RetryConfig, Workflow

from adk_libpetri.workflow import PetriWorkflow, compile_workflow, verify_workflow
from support.smt_proofs import requires_z3

from .._harness import Run, msg, run, run_both
from . import agent

TRACE_RANDOM = [0.5, 0.5, 0.8]
TEXTS = [
    "Getting weather... attempt 1",
    "Getting weather... attempt 2",
    "Getting weather... attempt 3",
    "The weather is sunny",
]
ERROR = ("HTTPError", "HTTP Error 500: Internal Server Error")


def _rolls(values: list[float]) -> SimpleNamespace:
    """Stand-in for the sample's ``random`` module, as the trace's mock."""
    it = iter(values)
    return SimpleNamespace(random=lambda: next(it))


def maker(values: list[float]) -> Callable[[], Workflow]:
    def make() -> Workflow:
        m = importlib.reload(agent)
        m.random = _rolls(values)  # type: ignore[attr-defined]
        return m.root_agent

    return make


def errors(r: Run) -> list[tuple[str | None, str | None]]:
    return [(e.error_code, e.error_message) for e in r.events if e.error_code]


def paths(r: Run) -> list[str]:
    return [e.node_info.path for e in r.events if e.node_info]


@pytest.mark.timeout(60)
async def test_native_run_reproduces_the_recorded_trace() -> None:
    r = await run(maker(TRACE_RANDOM)(), ["go"])
    assert r.texts == TEXTS
    assert r.final_output == "sunny"
    assert r.authors == ["root_agent"]
    assert errors(r) == [ERROR, ERROR]
    assert paths(r) == ["root_agent@1/get_weather@1"] * 6 + ["root_agent@1/report_weather@1"]


@pytest.mark.timeout(60)
async def test_compiled_run_recovers_like_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """Final output, authors, error events and the event count."""
    native, petri, _ = await run_both(maker(TRACE_RANDOM), ["go"], orchestrator)
    assert petri.final_output == native.final_output == "sunny"
    assert petri.authors == native.authors
    assert errors(petri) == errors(native) == [ERROR, ERROR]
    assert len(petri.events) == len(native.events)


@pytest.mark.timeout(60)
async def test_compiled_attempts_see_their_attempt_count(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(maker(TRACE_RANDOM), ["go"], orchestrator)
    assert petri.texts == native.texts == TEXTS


@pytest.mark.timeout(60)
async def test_compiled_attempts_keep_one_node_path(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(maker(TRACE_RANDOM), ["go"], orchestrator)
    assert paths(petri) == paths(native)
    assert paths(petri) == ["root_agent@1/get_weather@1"] * 6 + ["root_agent@1/report_weather@1"]


def _fast_exhausting() -> Workflow:
    """The sample's own nodes with a two-attempt, 10 ms retry and both attempts failing."""
    m = importlib.reload(agent)
    m.random = _rolls([0.0, 0.0])  # type: ignore[attr-defined]
    flaky = m.get_weather.model_copy(
        update={"retry_config": RetryConfig(max_attempts=2, initial_delay=0.01)}
    )
    return Workflow(name="root_agent", edges=[("START", flaky, m.report_weather)])


async def _run_catching(node: Any) -> tuple[Run, BaseException | None]:
    """``run`` with the events streamed before ``run_async`` raised, if it did."""
    runner = InMemoryRunner(node=node, app_name="app")
    session = await runner.session_service.create_session(app_name="app", user_id="u")
    result = Run()
    try:
        async for e in runner.run_async(user_id="u", session_id=session.id, new_message=msg("go")):
            result.events.append(e)
    except Exception as err:
        return result, err
    return result, None


async def test_exhausted_retries_fail_the_root_node_like_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """Spent retries fail the root node in both: ``run_async`` raises the
    node's ``HTTPError`` after the two attempt error events (ADK's own; the
    compiled workflow adds none), and ``report_weather`` never runs."""
    native, native_err = await _run_catching(_fast_exhausting())
    compiled = compile_workflow(_fast_exhausting())
    petri, petri_err = await _run_catching(
        PetriWorkflow.from_compiled(compiled, orchestrator=orchestrator)
    )
    assert type(petri_err) is type(native_err) is HTTPError
    assert native.final_output is petri.final_output is None
    assert "The weather is sunny" not in petri.texts
    assert errors(petri) == errors(native) == [ERROR, ERROR]
    assert paths(petri) == paths(native)


def test_report() -> None:
    cw = compile_workflow(maker(TRACE_RANDOM)())
    assert not cw.report.rejected
    exact = {(f.subject, f.message) for f in cw.report.of("exact")}
    assert (
        "get_weather",
        "retry_config kept on the node: ADK's node runner retries inside the transition",
    ) in exact
    names = set(cw.spec.transition_names)
    assert {"Wf_get_weather_Run", "Wf_report_weather_Run"} <= names
    assert "Wf_EndTurnOutput_report_weather" in names
    assert {n for n in names if "get_weather" in n} == {"Wf_get_weather_Run"}
    assert not [n for n in names if "Backoff" in n]


@requires_z3
@pytest.mark.timeout(600)
def test_every_safety_claim_and_deadlock_freedom_is_proven() -> None:
    cw = compile_workflow(maker(TRACE_RANDOM)())
    proofs = verify_workflow(cw, k=1)
    verdicts = {p.label: p.result.verdict for p in proofs}
    assert {p.kind for p in proofs} == {"safety", "deadlock"}
    assert "deadlock_free" in verdicts
    assert (
        "report_weather keeps one output: place_bound(report_weather/terminalOutput, 1)" in verdicts
    )
    assert len(verdicts) == 3 + 2 + 1
    assert all(v == "proven" for v in verdicts.values()), verdicts
