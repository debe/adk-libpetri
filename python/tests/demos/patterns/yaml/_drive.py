"""Drive a blueprint through ADK's loader and a stock ``InMemoryRunner``: one turn, then drain.

The YAML twin of ``_petri_harness.run_turn_then_drain``. ``from_config`` (what
``adk run`` and ``adk web`` call) loads the blueprint; the turn runs under
``InMemoryRunner``; then the session's runner is closed, so every in-flight
branch has finished, and the run hands back the final marking and every token
the net ever put on ``eventOut`` (read from the event store, over the runner's
whole life).

One difference from ``PetriAgent`` shapes the latency assertions: a
``PetriNet`` yields its answer as soon as it lands on ``eventOut``, but keeps
the invocation open until the node runs its turn started are over (a race's
loser runs inside it). So "the turn is fast" is measured as the time to the
answer event, not as the time ``run_async`` takes.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import libpetri as lp
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.net import PetriNet
from adk_libpetri.runner import SessionExecutorRegistry, SessionKey

HERE = Path(__file__).parent
APP = "patterns_yaml"
USER = "u"


def agent_module(package: str) -> ModuleType:
    """``<package>.agent`` as ADK's loader imports it (``.agent.fn`` in its YAML),
    so a test reads the very module the nodes live in."""
    return importlib.import_module(f"{package}.agent")


@dataclass
class NetRun:
    node: PetriNet
    events: list[Event]
    """What the stock ``InMemoryRunner`` yielded for the turn."""
    at: list[float]
    """Seconds from ``run_async`` to each of ``events``."""
    elapsed: float
    """Wall time of the whole invocation, losers' node runs included."""
    egress: list[Any]
    """Every token the net put on ``eventOut`` over the runner's whole life."""
    final: lp.MarkingView
    """The marking after the drained runner came to rest."""

    def answer(self) -> tuple[Event, float]:
        """The net's own event and when it came: the net's output, emitted under the
        transition that answered (``<name>@1/<Transition>@1``), not a node run's."""
        own = f"{self.node.name}@1"
        answering = {t.split("/", 1)[0] for t in self.node.spec.transition_names}
        [(event, at)] = [
            (e, t)
            for e, t in zip(self.events, self.at, strict=True)
            if e.node_info.path.startswith(own + "/")
            and e.node_info.path.removeprefix(own + "/").split("@")[0] in answering
        ]
        return event, at

    def node_run_at(self, node: str, run: int = 1) -> float:
        """When the event of node run ``<node>@<run>`` came."""
        path = f"{self.node.name}@1/{node}@{run}"
        [at] = [t for e, t in zip(self.events, self.at, strict=True) if e.node_info.path == path]
        return at


def load(path: Path, orch: OrchestratorLoop, **serving: Any) -> PetriNet:
    return PetriNet.from_config(str(path), orchestrator=orch, **serving)


async def run_turn_then_drain(
    orch: OrchestratorLoop,
    blueprint: Path | PetriNet,
    *,
    text: str,
    configure: Callable[[PetriNet], None] | None = None,
) -> NetRun:
    """Load ``blueprint`` (a YAML path, or a node), run one turn, close its runner."""
    registry = SessionExecutorRegistry.strong_owned()
    store = lp.InMemoryEventStore()
    if isinstance(blueprint, PetriNet):
        node = blueprint.serve_on(orch, registry=registry, event_store=store)
    else:
        node = load(blueprint, orch, registry=registry, event_store=store)
    if configure is not None:
        configure(node)
    adk = InMemoryRunner(node=node, app_name=APP)
    session = await adk.session_service.create_session(app_name=APP, user_id=USER)
    message = types.Content(role="user", parts=[types.Part(text=text)])
    events: list[Event] = []
    at: list[float] = []
    start = time.monotonic()
    try:
        async for e in adk.run_async(user_id=USER, session_id=session.id, new_message=message):
            at.append(time.monotonic() - start)
            events.append(e)
        elapsed = time.monotonic() - start
        runner = registry.get(SessionKey(APP, USER, session.id, node.session_scope()))
        assert runner is not None, "the turn started the session's runner"
    finally:
        await registry.aclose_all()  # drain: in-flight actions finish, then the run ends
    final = await runner.wait_closed()
    egress = [
        e.token
        for e in store.events()
        if e.type == "TokenAdded" and e.place_name == C.EVENT_OUT.name
    ]
    return NetRun(node, events, at, elapsed, egress, final)


def warm_up() -> PetriNet:
    """An untimed echo net: the first turn in a process pays ADK's and libpetri's
    first-call costs, which would otherwise eat the first demo's latency margin."""
    return PetriNet(
        name="warm_up",
        transitions={"Warm_Echo": {"in": ["userIn"], "out": "eventOut", "action": "emit"}},
    )
