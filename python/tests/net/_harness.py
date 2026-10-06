"""Drive a node through ADK's stock ``InMemoryRunner``, one session, turn by turn."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.genai import types

from adk_libpetri.runner import PetriRunner, SessionKey


@dataclass
class Turn:
    events: list[Event] = field(default_factory=list)
    error: BaseException | None = None
    first_event_after: float | None = None
    """Seconds from ``run_async`` to the first event the node authored."""
    elapsed: float = 0.0

    @property
    def texts(self) -> list[str]:
        return [t for e in self.events if (t := text_of(e))]

    def by(self, author: str) -> list[Event]:
        return [e for e in self.events if e.author == author]


@dataclass
class Session:
    runner: InMemoryRunner
    session_id: str
    turns: list[Turn] = field(default_factory=list)

    async def say(self, text: str) -> Turn:
        message = types.Content(role="user", parts=[types.Part(text=text)])
        turn = Turn()
        start = time.monotonic()
        try:
            async for e in self.runner.run_async(
                user_id="u", session_id=self.session_id, new_message=message
            ):
                if turn.first_event_after is None and e.author != "user":
                    turn.first_event_after = time.monotonic() - start
                turn.events.append(e)
        except Exception as err:
            turn.error = err
        turn.elapsed = time.monotonic() - start
        self.turns.append(turn)
        return turn

    async def stored_events(self) -> list[Event]:
        s = await self.runner.session_service.get_session(
            app_name=self.runner.app_name, user_id="u", session_id=self.session_id
        )
        assert s is not None
        return list(s.events)


async def session(node: Any, app_name: str = "app") -> Session:
    runner = InMemoryRunner(node=node, app_name=app_name)
    s = await runner.session_service.create_session(app_name=app_name, user_id="u")
    return Session(runner, s.id)


def text_of(e: Event) -> str:
    if e.content is None or not e.content.parts:
        return ""
    return "".join(p.text or "" for p in e.content.parts)


def runner_of(node: Any, s: Session) -> PetriRunner:
    key = SessionKey(s.runner.app_name, "u", s.session_id, node.session_scope())
    runner = node.registry.get(key)
    assert runner is not None
    return runner
