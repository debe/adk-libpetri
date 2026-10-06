"""Fixtures for the YAML pattern twins: one warmed-up loop, the twins' packages on ``sys.path``."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from adk_libpetri._aio import OrchestratorLoop

from ._drive import HERE, run_turn_then_drain, warm_up


@pytest.fixture(autouse=True)
def _twins_on_sys_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADK resolves ``.agent.fn`` in ``yaml_race/root_agent.yaml`` as
    ``yaml_race.agent.fn``, imported from ``sys.path`` (``adk run`` runs from
    the folder holding the agents)."""
    monkeypatch.syspath_prepend(str(HERE))


@pytest.fixture(scope="package")
def orch() -> Iterator[OrchestratorLoop]:
    """One loop for the package, warmed up with one untimed turn, closed at the end."""
    loop = OrchestratorLoop("patterns-yaml-orchestrator")
    try:
        asyncio.run(run_turn_then_drain(loop, warm_up(), text="hi"))
        yield loop
    finally:
        loop.close()
