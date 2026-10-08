"""Fixtures for ``adk-libpetri web``: an agents folder holding the README hero's two nets."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from adk_libpetri._aio import OrchestratorLoop

HERO = Path(__file__).parents[1] / "readme_diagrams" / "hero"
LOOP_CONFIG = Path(__file__).parents[1] / "workflow" / "adk_samples" / "loop_config"
PATTERNS = Path(__file__).parents[1] / "demos" / "patterns" / "yaml"


def make_agents(root: Path) -> Path:
    """``root/race`` and ``root/race_naive``: each the hero YAML as ``root_agent.yaml``."""
    for app, src in (("race", "race.yaml"), ("race_naive", "race_naive.yaml")):
        d = root / app
        d.mkdir(parents=True)
        (d / "__init__.py").write_text("")
        shutil.copy(HERO / "agent.py", d / "agent.py")
        shutil.copy(HERO / src, d / "root_agent.yaml")
    return root


def add_workflow_app(root: Path, name: str = "loop_config") -> Path:
    """``root/<name>``: ADK's loop_config sample with its ``PetriWorkflow`` YAML as the root."""
    d = root / name
    shutil.copytree(LOOP_CONFIG, d, ignore=shutil.ignore_patterns("test_*", "__pycache__"))
    (d / "root_agent.yaml").write_text((LOOP_CONFIG / "petri_root_agent.yaml").read_text())
    (d / "petri_root_agent.yaml").unlink()
    return d


def add_composed_app(root: Path, name: str = "composed") -> Path:
    """``root/<name>``: the composed pattern (two races, a stock llm_agent) and its packages."""
    for pkg in ("yaml_race", "yaml_composed"):
        if not (root / pkg).exists():
            shutil.copytree(
                PATTERNS / pkg, root / pkg, ignore=shutil.ignore_patterns("__pycache__")
            )
    d = root / name
    d.mkdir()
    (d / "__init__.py").write_text("")
    (d / "root_agent.yaml").write_text((PATTERNS / "composed_agent.yaml").read_text())
    for pkg in ("yaml_race", "yaml_composed"):
        shutil.copytree(PATTERNS / pkg, d / pkg, ignore=shutil.ignore_patterns("__pycache__"))
    return d


@pytest.fixture
def agents(tmp_path: Path) -> Path:
    return make_agents(tmp_path / "agents")


@pytest.fixture(autouse=True)
def _adk_globals() -> Iterator[None]:
    """``get_fast_api_app`` turns on ADK's YAML key denylists process-wide; put them
    back, so a later test's YAML (a key named ``args``) loads as it would elsewhere."""
    from google.adk.agents import config_agent_utils as utils

    saved = {k: getattr(utils, k) for k in ("_ENFORCE_YAML_KEY_DENYLIST", "_ENFORCE_DENYLIST")}
    yield
    for k, v in saved.items():
        setattr(utils, k, v)


@pytest.fixture(scope="package", autouse=True)
def _shared_loop() -> Iterator[None]:
    """ADK's loader serves a net on the shared loop; close it with the package,
    so the next package's tests start their executors on a loop of their own."""
    yield
    OrchestratorLoop.close_shared()
