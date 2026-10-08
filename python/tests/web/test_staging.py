"""The builder's draft is loaded under a package name of its own, never the app's."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adk_libpetri.net import PetriNet
from adk_libpetri.web.builder import check_petri_blueprint, write_petri_blueprints
from adk_libpetri.web.loader import PetriAgentLoader, net_nodes
from adk_libpetri.web.staging import staged

ONLY = "\n\ndef only(node_input: object) -> str:\n    return 'only'\n"


def _draft(agents: Path, *, absolute: bool = False) -> Path:
    """``race/tmp/race``: the app copied, with a function only the draft has, used by its net."""
    draft = agents / "race" / "tmp" / "race"
    shutil.copytree(agents / "race", draft, ignore=shutil.ignore_patterns("tmp"))
    (draft / "agent.py").write_text((draft / "agent.py").read_text() + ONLY)
    ref = "race.agent.only" if absolute else ".agent.only"
    text = (draft / "root_agent.yaml").read_text()
    text = text.replace("nodes: [[.agent.fast], [.agent.slow]]", f"nodes: [[{ref}]]")
    text = text.replace("node: fast", "node: only").replace("node: slow", "node: only")
    (draft / "root_agent.yaml").write_text(text)
    return draft


def _ctx(draft: Path) -> Any:
    return SimpleNamespace(state={"root_directory": str(draft)})


@pytest.fixture
def loader(agents: Path) -> Iterator[PetriAgentLoader]:
    for k in [k for k in sys.modules if k == "race" or k.startswith("race.")]:
        del sys.modules[k]
    loader = PetriAgentLoader(str(agents))
    yield loader
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()
    for k in [k for k in sys.modules if k == "race" or k.startswith("race.")]:
        del sys.modules[k]


@pytest.mark.parametrize("absolute", [False, True])
async def test_checking_the_draft_first_leaves_the_app_its_own_code(
    agents: Path, loader: PetriAgentLoader, absolute: bool
) -> None:
    draft = _draft(agents, absolute=absolute)
    r = await check_petri_blueprint("root_agent.yaml", _ctx(draft))
    assert r["ok"] is True, r
    assert not [k for k in sys.modules if k == "race" or k.startswith("race.")]
    node = loader.load_agent("race")
    assert isinstance(node, PetriNet)
    app_code = Path(sys.modules["race.agent"].__file__ or "")
    # The app's own file, not the draft's copy (<app>/tmp/<app>); the test's own
    # folder may sit under /tmp, as it does on Linux.
    assert app_code.resolve() == (agents / "race" / "agent.py").resolve()
    assert not hasattr(sys.modules["race.agent"], "only")
    assert not [k for k in sys.modules if k.startswith("_petri_stage_")]


async def test_checking_the_draft_after_the_app_loaded_uses_the_drafts_code(
    agents: Path, loader: PetriAgentLoader
) -> None:
    loader.load_agent("race")  # the app is open in the UI
    app_module = sys.modules["race.agent"]
    draft = _draft(agents)
    r = await check_petri_blueprint("root_agent.yaml", _ctx(draft))
    assert r["ok"] is True, r  # `only` resolves: the draft's agent.py, not the app's
    w = await write_petri_blueprints(
        {"root_agent.yaml": (draft / "root_agent.yaml").read_text()}, _ctx(draft)
    )
    assert w["success"] is True, w
    assert sys.modules["race.agent"] is app_module


async def test_errors_name_the_app_not_the_copy(agents: Path, loader: PetriAgentLoader) -> None:
    draft = _draft(agents, absolute=True)
    text = (draft / "root_agent.yaml").read_text().replace("race.agent.only", "race.agent.gone")
    (draft / "root_agent.yaml").write_text(text)
    r = await check_petri_blueprint("root_agent.yaml", _ctx(draft))
    assert r["ok"] is False
    assert "_petri_stage_" not in r["error"] and "petri-stage-" not in r["error"]
    assert "race.agent.gone" in r["error"]


def test_a_stage_renames_only_the_apps_own_refs(tmp_path: Path) -> None:
    app = tmp_path / "race"
    app.mkdir()
    (app / "root_agent.yaml").write_text(
        "nodes: [[race.agent.fast], [other_race.agent.x], [race.yaml], [sub/race.agent.y]]\n"
        "name: race\n"
    )
    with staged(app) as stage:
        text = (stage.root / "root_agent.yaml").read_text()
        assert f"[{stage.package}.agent.fast]" in text
        assert "[other_race.agent.x]" in text and "[race.yaml]" in text
        assert "[sub/race.agent.y]" in text and "name: race\n" in text
        assert stage.restore(f"{stage.root}/x.yaml: {stage.package}.agent.z") == (
            f"{app.resolve()}/x.yaml: race.agent.z"
        )


def test_a_stage_leaves_no_import_finders_behind(agents: Path) -> None:
    before = {k for k in sys.path_importer_cache if "petri-stage-" in k}
    for _ in range(3):
        with staged(agents / "race") as stage:
            sys.path.insert(0, str(stage.root.parent))
            try:
                __import__(f"{stage.package}.agent")
            finally:
                sys.path.remove(str(stage.root.parent))
    after = {k for k in sys.path_importer_cache if "petri-stage-" in k}
    assert after <= before
