"""Fixtures shared by the from_workflow tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from adk_libpetri._aio import OrchestratorLoop


@pytest.fixture(scope="package")
def orchestrator() -> Iterator[OrchestratorLoop]:
    """One loop for every workflow test: a PetriWorkflow's session runners
    outlive the test that made them, and libpetri runs executors on one loop."""
    loop = OrchestratorLoop("workflow-tests-orchestrator")
    yield loop
    loop.close()
