"""Port of Java ``PetriAgentSseTest``: ``StreamingLlmAgent`` under ``StreamingMode.SSE``.

Java's ``deferredExecutorRef`` is ``handle_ref`` here, and Java's
``IllegalStateException`` on a duplicate env place is a ``ValueError``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest
from google.adk.agents.run_config import (
    RunConfig,
    StreamingMode,  # pyright: ignore[reportPrivateImportUsage]
)
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import PrivateAttr

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.runner import (
    Checkpoint,
    HandleRef,
    InMemoryCheckpointStore,
    PetriAgent,
    SessionExecutorRegistry,
    SessionKey,
)
from adk_libpetri.subnet import streaming_llm_agent as SA
from support.fake_llm import ScriptedLlm, text

AGENT_NAME = "sse_agent"
SSE = RunConfig(streaming_mode=StreamingMode.SSE)
NONE = RunConfig(streaming_mode=StreamingMode.NONE)


@pytest.fixture(scope="module")
def orch() -> Iterator[OrchestratorLoop]:
    loop = OrchestratorLoop("sse-orchestrator")
    yield loop
    loop.close()


# ============================================================
#  Fakes
# ============================================================


def last_user_text(request: LlmRequest) -> str:
    last = request.contents[-1]
    return "".join(p.text or "" for p in last.parts or [])


class EchoingLlm(BaseLlm):
    """Streams ``"echo: "`` plus the request's last user text, in two chunks.

    The first ``fail_first`` calls stream ``"half "`` and then raise.
    """

    model: str = "echoing"
    _fail_first: int = PrivateAttr(default=0)
    _calls: int = PrivateAttr(default=0)

    @classmethod
    def failing_first(cls, n: int) -> EchoingLlm:
        llm = cls()
        llm._fail_first = n
        return llm

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._calls += 1
        if self._calls <= self._fail_first:
            yield text("half ")
            raise RuntimeError("stream dropped")
        yield text("echo: ")
        yield text(last_user_text(llm_request))


def streaming_llm(chunks: list[LlmResponse]) -> ScriptedLlm:
    return ScriptedLlm.of().stream(0, chunks)


# ============================================================
#  Helpers
# ============================================================


def user_message(t: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=t)])


def text_of(event: Event) -> str:
    assert event.content is not None and event.content.parts
    return "".join(p.text or "" for p in event.content.parts)


def agent_for(
    orch: OrchestratorLoop,
    llm: BaseLlm,
    registry: SessionExecutorRegistry,
    config: SA.Config | None = None,
) -> PetriAgent:
    config = config or SA.Config(name=AGENT_NAME, model="fake-model")
    return (
        PetriAgent.builder(
            AGENT_NAME,
            registry,
            SA.runner_factory(llm, config, lambda key, b: b.orchestrator(orch)),
        )
        .description("Streaming SSE test agent")
        .build()
    )


async def new_session(runner: InMemoryRunner, user: str) -> Any:
    return await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user, session_id=f"session-{uuid.uuid4()}"
    )


async def run_turn(
    runner: InMemoryRunner, session: Any, msg: str, run_config: RunConfig
) -> list[Event]:
    async def collect() -> list[Event]:
        return [
            e
            async for e in runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=user_message(msg),
                run_config=run_config,
            )
        ]

    return await asyncio.wait_for(collect(), 3)


def final_text(events: list[Event]) -> str:
    return text_of(events[-1])


async def run_streaming_turn(
    orch: OrchestratorLoop, llm: BaseLlm, run_config: RunConfig
) -> list[Event]:
    registry = SessionExecutorRegistry.strong_owned()
    try:
        supplied = iter(range(1, 1_000))
        config = SA.Config(
            name=AGENT_NAME,
            model="fake-model",
            invocation_id_supplier=lambda: f"net-generated-{next(supplied)}",
        )
        runner = InMemoryRunner(agent=agent_for(orch, llm, registry, config), app_name="app")
        return await run_turn(runner, await new_session(runner, "user-1"), "what's 6*7", run_config)
    finally:
        await registry.aclose_all()


THREE_CHUNKS = [text("Sure, "), text("the answer "), text("is 42.")]


# ============================================================
#  Tests
# ============================================================


async def test_sse_mode_emits_ordered_partials_then_final_event_and_completes_with_one_invocation_id(  # noqa: E501
    orch,
) -> None:
    events = await run_streaming_turn(orch, streaming_llm(THREE_CHUNKS), SSE)

    assert len(events) == 4
    assert [text_of(e) for e in events] == [
        "Sure, ",
        "the answer ",
        "is 42.",
        "Sure, the answer is 42.",
    ]
    for partial in events[:-1]:
        assert partial.partial is True
        assert partial.author == AGENT_NAME
    final = events[-1]
    assert not final.partial
    assert final.author == AGENT_NAME

    turn_id = events[0].invocation_id
    assert turn_id
    assert "net-generated-" not in turn_id
    assert {e.invocation_id for e in events} == {turn_id}


async def test_none_mode_on_streaming_net_returns_one_terminal_non_partial_event(orch) -> None:
    events = await run_streaming_turn(orch, streaming_llm(THREE_CHUNKS), NONE)

    assert len(events) == 1
    only = events[0]
    assert not only.partial
    assert only.author == AGENT_NAME
    assert text_of(only) == "Sure, the answer is 42."


async def test_concurrent_sse_sessions_each_receive_only_their_own_chunks(orch) -> None:
    # Each session's runner needs its own handle ref: a shared one would inject
    # an earlier session's chunks into the newest session's executor.
    registry = SessionExecutorRegistry.strong_owned()
    try:
        runner = InMemoryRunner(agent=agent_for(orch, EchoingLlm(), registry), app_name="app")
        alice = await new_session(runner, "alice")
        bob = await new_session(runner, "bob")

        assert final_text(await run_turn(runner, alice, "hello from alice", SSE)) == (
            "echo: hello from alice"
        )
        assert final_text(await run_turn(runner, bob, "hello from bob", SSE)) == (
            "echo: hello from bob"
        )
        assert final_text(await run_turn(runner, alice, "alice again", SSE)) == (
            "echo: alice again"
        )
    finally:
        await registry.aclose_all()


async def test_concurrent_sse_turns_on_two_sessions_interleave_without_crosstalk(orch) -> None:
    # Python-only: the two turns really run at once, on one pytest loop.
    registry = SessionExecutorRegistry.strong_owned()
    try:
        runner = InMemoryRunner(agent=agent_for(orch, EchoingLlm(), registry), app_name="app")
        alice = await new_session(runner, "alice")
        bob = await new_session(runner, "bob")

        a, b = await asyncio.gather(
            run_turn(runner, alice, "from alice", SSE), run_turn(runner, bob, "from bob", SSE)
        )

        assert final_text(a) == "echo: from alice"
        assert final_text(b) == "echo: from bob"
        assert "from bob" not in [text_of(e) for e in a]
        assert "from alice" not in [text_of(e) for e in b]
    finally:
        await registry.aclose_all()


async def test_a_stream_that_fails_mid_turn_fails_that_turn_and_the_next_turn_streams(
    orch,
) -> None:
    registry = SessionExecutorRegistry.strong_owned()
    try:
        runner = InMemoryRunner(
            agent=agent_for(orch, EchoingLlm.failing_first(1), registry), app_name="app"
        )
        session = await new_session(runner, "user-1")

        with pytest.raises(Exception):  # noqa: B017 - any failure, as Java asserts Throwable
            await run_turn(runner, session, "first", SSE)

        events = await run_turn(runner, session, "second", SSE)
        assert final_text(events) == "echo: second"
        assert "half " not in [text_of(e) for e in events]
    finally:
        await registry.aclose_all()


class RecordingStore:
    """Records every ``load`` key, delegating to an in-memory store."""

    def __init__(self) -> None:
        self.inner = InMemoryCheckpointStore()
        self.loaded: list[SessionKey] = []

    def save(self, key: SessionKey, marking: Checkpoint) -> None:
        self.inner.save(key, marking)

    def load(self, key: SessionKey) -> Checkpoint | None:
        self.loaded.append(key)
        return self.inner.load(key)

    def remove(self, key: SessionKey) -> None:
        self.inner.remove(key)


async def test_customize_gets_the_session_key_and_cannot_displace_the_executor_ref(
    orch,
) -> None:
    # customize runs before the factory's own settings: a stray handle_ref set
    # there cannot displace the per-session one.
    store = RecordingStore()
    config = SA.Config(name=AGENT_NAME, model="fake-model")
    registry = SessionExecutorRegistry.strong_owned()
    try:
        agent = PetriAgent.builder(
            AGENT_NAME,
            registry,
            SA.runner_factory(
                EchoingLlm(),
                config,
                lambda key, b: b.orchestrator(orch).handle_ref(HandleRef()).resume_from(store, key),
            ),
        ).build()
        runner = InMemoryRunner(agent=agent, app_name="app")
        session = await new_session(runner, "user-1")

        assert final_text(await run_turn(runner, session, "hi", SSE)) == "echo: hi"
        assert store.loaded == [SessionKey.of(session)]
    finally:
        await registry.aclose_all()


async def test_customize_that_declares_a_factory_owned_env_place_fails_the_start(orch) -> None:
    config = SA.Config(name=AGENT_NAME, model="fake-model")
    factory = SA.runner_factory(
        EchoingLlm(), config, lambda key, b: b.orchestrator(orch).environment_place(C.USER_IN)
    )

    with pytest.raises(ValueError, match="already declared"):
        await factory(SessionKey("app", "user", "session"))
