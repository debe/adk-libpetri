"""Fixtures shared by the net-blueprint tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.net import PetriNet
from adk_libpetri.runner import SessionExecutorRegistry

BLUEPRINTS = Path(__file__).parent / "blueprints"


@pytest.fixture(scope="package")
def orchestrator() -> Iterator[OrchestratorLoop]:
    """One loop for every net test: a PetriNet's session runners outlive the
    test that made them, and libpetri runs executors on one loop."""
    loop = OrchestratorLoop("net-tests-orchestrator")
    yield loop
    loop.close()


Serve = Callable[[PetriNet], PetriNet]


@pytest.fixture(scope="package")
def serve(orchestrator: OrchestratorLoop) -> Iterator[Serve]:
    """Serve a node on the package's loop, its session runners in one registry
    that is closed with the package: libpetri runs executors on one loop at a
    time, and the next package's tests start theirs on another."""
    registry = SessionExecutorRegistry.strong_owned()
    yield lambda node: node.serve_on(orchestrator, registry=registry)
    registry.close_all()


@pytest.fixture(autouse=True)
def _blueprints_on_sys_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADK resolves ``.agent.fn`` in a YAML file as ``<its directory name>.agent.fn``,
    imported from ``sys.path`` (``adk run`` runs from the folder holding the agents)."""
    for d in (
        BLUEPRINTS,
        BLUEPRINTS / "bp_nested",
        BLUEPRINTS / "bp_nested" / "bp_mid",
    ):
        monkeypatch.syspath_prepend(str(d))
