"""Port of Java ``PetriRunnerTest``.

Java's ``orchestratorExecutor``/``actionExecutor`` pools have no Python
equivalent: every runner starts on one :class:`OrchestratorLoop` and actions
run on libpetri's Tokio threads. The executor-placement tests pin that instead.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import libpetri as lp
import pytest
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.bridge import TransitionFailure
from adk_libpetri.runner import PetriRunner
from adk_libpetri.subnet import llm_agent as LA
from support.fake_llm import ScriptedLlm, text


@pytest.fixture(scope="module")
def orchestrator() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop()
    yield loop
    loop.close()


async def test_send_and_observe_event_for_text_only_llm_response(
    orchestrator: OrchestratorLoop,
) -> None:
    llm = ScriptedLlm.of(text("hello back"))
    async with await new_runner(orchestrator, llm, "hello-agent") as runner:
        # Subscribe BEFORE inject to avoid the hot-stream race.
        sub = runner.adk_events().subscribe()

        assert runner.inject(C.USER_IN, user_message("hi")) is True

        events = await take(sub, 1)
        assert [text_of(e) for e in events] == ["hello back"]


async def test_runner_supports_multiple_sequential_sends(orchestrator: OrchestratorLoop) -> None:
    llm = ScriptedLlm.of(text("r1"), text("r2"), text("r3"))
    async with await new_runner(orchestrator, llm, "multi-agent") as runner:
        sub = runner.adk_events().subscribe()

        assert runner.inject(C.USER_IN, user_message("a"))
        assert runner.inject(C.USER_IN, user_message("b"))
        assert runner.inject(C.USER_IN, user_message("c"))

        events = await take(sub, 3, timeout=3)
        assert [text_of(e) for e in events] == ["r1", "r2", "r3"]


async def test_shutdown_completes_event_stream(orchestrator: OrchestratorLoop) -> None:
    runner = await new_runner(orchestrator, ScriptedLlm.of(), "shutdown-test")  # never invoked
    sub = runner.adk_events().subscribe()

    await runner.aclose()

    assert await drain_to_end(sub) == []


async def test_drain_returns_immediately_and_await_termination_confirms_completion(
    orchestrator: OrchestratorLoop,
) -> None:
    # drain() is fire-and-forget (right for on-close hooks where blocking the
    # caller is unacceptable); await_termination(timeout) is the bounded wait
    # that confirms teardown when the caller needs it.
    runner = await new_runner(orchestrator, ScriptedLlm.of(), "drain-async-test")
    sub = runner.adk_events().subscribe()

    start = time.monotonic()
    runner.drain()
    drain_latency = time.monotonic() - start
    # The caller's thread is not blocked on teardown.
    assert drain_latency < 0.1

    assert await asyncio.to_thread(runner.await_termination, 2.0) is True
    assert await drain_to_end(sub) == []


async def test_await_termination_returns_false_when_orchestrator_is_still_running(
    orchestrator: OrchestratorLoop,
) -> None:
    # Without a drain the run is alive: await_termination must report the
    # timeout instead of blocking the caller forever.
    async with await new_runner(orchestrator, ScriptedLlm.of(), "await-timeout-test") as runner:
        start = time.monotonic()
        terminated = runner.await_termination(0.05)
        elapsed = time.monotonic() - start

        assert terminated is False
        # We waited about the requested timeout, not forever.
        assert elapsed < 2


SIGNAL: Place[None] = Place("signalTest_sig")


async def test_signal_and_none_inject_put_unit_tokens_onto_a_void_place(
    orchestrator: OrchestratorLoop,
) -> None:
    # A unit (Void) signal place takes None tokens. signal(place) and
    # inject(place, None) both inject one; each fires T_Ack, which produces
    # one EVENT_OUT. A None onto a typed place is rejected.
    def ack(ctx: Ctx) -> None:
        ctx.input(SIGNAL)
        ctx.output(C.EVENT_OUT, Event(invocation_id="inv", author="net"))

    spec = NetSpec("signal-test", (TransitionSpec("T_Ack", (one(SIGNAL),), out(C.EVENT_OUT)),))
    runner = await (
        PetriRunner.builder(spec, {"T_Ack": ack})
        .environment_place(SIGNAL)
        .orchestrator(orchestrator)
        .astart()
    )
    async with runner:
        sub = runner.adk_events().subscribe()

        assert runner.signal(SIGNAL) is True
        assert runner.inject(SIGNAL, None) is True

        assert len(await take(sub, 2)) == 2


async def test_none_onto_a_typed_env_place_is_rejected(orchestrator: OrchestratorLoop) -> None:
    async with await new_runner(orchestrator, ScriptedLlm.of(), "typed-none") as runner:
        with pytest.raises(TypeError, match="signal"):
            runner.inject(C.USER_IN, None)


async def test_a_throwing_action_is_reported_and_does_not_kill_the_orchestrator(
    orchestrator: OrchestratorLoop, caplog: pytest.LogCaptureFixture
) -> None:
    # libpetri contains an action failure to the failing transition: the run
    # survives and the consumed tokens are lost (EXEC-031). The runner's
    # default chain records nothing, so without its own warning a throwing
    # action would vanish without trace.
    boom = Place("boom", str)

    def explode(ctx: Ctx) -> None:
        raise ValueError("action blew up")

    spec = NetSpec("throwing-host", (TransitionSpec("Boom", (one(C.USER_IN),), out(boom)),))
    caplog.set_level(logging.WARNING, logger="adk_libpetri.runner")
    runner = await (
        PetriRunner.builder(spec, {"Boom": explode})
        .environment_place(C.USER_IN)
        .orchestrator(orchestrator)
        .astart()
    )
    async with runner:
        failures = runner.failure_signal().subscribe()
        assert runner.inject(C.USER_IN, user_message("go"))

        failure = await asyncio.wait_for(anext(failures), 2)
        assert isinstance(failure, TransitionFailure)
        assert failure.transition_name == "Boom"

        deadline = time.monotonic() + 2
        while not runner_warnings(caplog) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        # Not silent: the failure was reported.
        records = runner_warnings(caplog)
        assert records
        assert records[0].levelno == logging.WARNING
        assert "Boom" in records[0].getMessage()
        assert "action blew up" in records[0].getMessage()

        # Contained: the run is still alive and still accepting.
        assert runner.inject(C.USER_IN, user_message("again")) is True


async def test_actions_run_on_libpetri_threads_not_the_orchestrator_loop(
    orchestrator: OrchestratorLoop,
) -> None:
    # Pins where transition actions run: on libpetri's own (Tokio) threads,
    # with no running asyncio loop -- not on the orchestrator thread the
    # executor was started from. Sync and async actions alike; this is why
    # stock actions hop ADK coroutines back with on_loop.
    sink = Place("threadSink", str)
    poke = Place("poke", str)
    seen: dict[str, tuple[threading.Thread, bool]] = {}
    done = threading.Event()

    def where() -> tuple[threading.Thread, bool]:
        try:
            asyncio.get_running_loop()
            has_loop = True
        except RuntimeError:
            has_loop = False
        return threading.current_thread(), has_loop

    def probe(ctx: Ctx) -> None:
        ctx.input(C.USER_IN)
        seen["sync"] = where()
        ctx.output(sink, "ok")

    async def aprobe(ctx: Ctx) -> None:
        ctx.input(poke)
        seen["async"] = where()
        ctx.output(sink, "ok")
        done.set()

    spec = NetSpec(
        "thread-probe",
        (
            TransitionSpec("Probe", (one(C.USER_IN),), out(sink)),
            TransitionSpec("AProbe", (one(poke),), out(sink)),
        ),
    )
    runner = await (
        PetriRunner.builder(spec, {"Probe": probe, "AProbe": aprobe})
        .environment_places(C.USER_IN, poke)
        .orchestrator(orchestrator)
        .astart()
    )
    async with runner:
        assert runner.inject(C.USER_IN, user_message("probe"))
        assert runner.inject(poke, "probe")
        assert await asyncio.to_thread(done.wait, 2)
        deadline = time.monotonic() + 2
        while "sync" not in seen and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        orchestrator_thread = orchestrator._thread  # pyright: ignore[reportPrivateUsage]
        for kind in ("sync", "async"):
            thread, has_loop = seen[kind]
            assert thread is not orchestrator_thread, kind
            assert has_loop is False, kind


async def test_runner_starts_with_only_the_required_options(
    orchestrator: OrchestratorLoop,
) -> None:
    # Java: actionExecutor is inert, so omitting it must build and run fine.
    # Python: the orchestrator is the one required option; no event store,
    # scope or tolerance is needed to run a turn.
    llm = ScriptedLlm.of(text("no extras"))
    runner = await (
        PetriRunner.builder(LA.DEF, agent_actions(llm, "no-ae-agent"))
        .environment_place(C.USER_IN)
        .orchestrator(orchestrator)
        .astart()
    )
    async with runner:
        sub = runner.adk_events().subscribe()
        assert runner.inject(C.USER_IN, user_message("hi"))
        assert len(await take(sub, 1)) == 1


async def test_signalling_a_terminal_end_invocation_place_ends_the_run_without_a_drain(
    orchestrator: OrchestratorLoop,
) -> None:
    # Session end as a terminal place: declare END_INVOCATION terminal on the
    # top-level net and signal it. The run ends on its own, with "terminal",
    # without a drain.
    actions = agent_actions(ScriptedLlm.of(text("hi")), "agent")
    b = lp.Net("terminal-session")
    for p in (*LA.DEF.places, C.END_INVOCATION):
        b = b.place(p.lp())
    for t in LA.DEF.transitions:
        b = b.transition(t.build(actions[t.name]))
    net = b.terminal(C.END_INVOCATION.name).build()
    runner = (
        await PetriRunner.builder(net)
        .environment_places(C.USER_IN, C.END_INVOCATION)
        .orchestrator(orchestrator)
        .astart()
    )
    egress = runner.adk_events().subscribe()
    assert runner.inject(C.USER_IN, user_message("hello"))
    assert len(await take(egress, 1)) == 1

    assert runner.signal(C.END_INVOCATION)

    assert await asyncio.to_thread(runner.await_termination, 2.0)
    assert runner.termination_reason == "terminal"
    assert await drain_to_end(egress) == []


# ============================================================
#  Fixtures
# ============================================================


def agent_actions(llm: BaseLlm, name: str) -> dict[str, Any]:
    return LA.action_bindings(llm, LA.Config(name=name, model="fake-model"))


async def new_runner(orchestrator: OrchestratorLoop, llm: BaseLlm, name: str) -> PetriRunner:
    return await (
        PetriRunner.builder(LA.DEF, agent_actions(llm, name))
        .environment_place(C.USER_IN)
        .orchestrator(orchestrator)
        .astart()
    )


async def take(sub: AsyncIterator[Event], n: int, timeout: float = 2) -> list[Event]:
    async def collect() -> list[Event]:
        got: list[Event] = []
        async for e in sub:
            got.append(e)
            if len(got) == n:
                break
        return got

    return await asyncio.wait_for(collect(), timeout)


async def drain_to_end(sub: AsyncIterator[Event], timeout: float = 2) -> list[Event]:
    """Every remaining item, once the stream completes (fails on timeout)."""

    async def collect() -> list[Event]:
        return [e async for e in sub]

    return await asyncio.wait_for(collect(), timeout)


def runner_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "adk_libpetri.runner" and r.levelno >= logging.WARNING
    ]


def user_message(t: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=t)])


def text_of(e: Event) -> str | None:
    assert e.content is not None and e.content.parts
    return e.content.parts[0].text
