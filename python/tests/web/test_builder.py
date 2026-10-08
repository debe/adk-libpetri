"""The Petri builder assistant: ADK's assistant with tools to write, check and verify nets.

No model is called: the tools are plain functions, run here as ADK would.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adk_libpetri.net.report import ClaimResult, VerifyReport
from adk_libpetri.web import proof_view
from adk_libpetri.web.builder import (
    ADK_ASSISTANT,
    INSTRUCTION_ADDENDUM,
    check_petri_blueprint,
    create_petri_builder_assistant,
    known_verdicts,
    petri_authoring_guide,
    petri_schema,
    verdict_line,
    verify_petri_blueprint,
    write_petri_blueprints,
)
from adk_libpetri.web.loader import PetriAgentLoader
from support.smt_proofs import requires_z3

from .conftest import HERO

PETRI_TOOLS = {
    "petri_authoring_guide",
    "petri_schema",
    "write_petri_blueprints",
    "check_petri_blueprint",
    "verify_petri_blueprint",
}


def tool_context(root: Path) -> Any:
    """What the tools read of ADK's ToolContext: the session's root_directory."""
    return SimpleNamespace(state={"root_directory": str(root)})


def test_the_dev_uis_builder_gets_the_petri_tools(agents: Path) -> None:
    loader = PetriAgentLoader(str(agents))
    loader._allow_special_agents = True  # as get_fast_api_app(web=True) sets it
    assistant = loader.load_agent(ADK_ASSISTANT)
    names = {getattr(t, "name", None) for t in assistant.tools}
    assert names >= PETRI_TOOLS
    assert "write_config_files" in names  # ADK's own tools stay
    assert loader.load_agent(ADK_ASSISTANT) is assistant  # ADK's cache still applies


async def test_the_instruction_gains_the_petri_loop(tmp_path: Path) -> None:
    assistant = create_petri_builder_assistant()
    session = SimpleNamespace(state={"root_directory": str(tmp_path)})
    ctx = SimpleNamespace(_invocation_context=SimpleNamespace(session=session))
    text = await assistant.instruction(ctx)
    assert text.endswith(INSTRUCTION_ADDENDUM)
    assert "verify_petri_blueprint" in text
    assert "Click Save to apply it" in text  # the reply ends with the net's state
    assert "`picture`" in text and "exactly as given" in text  # the counterexample, shown
    assert len(text) > len(INSTRUCTION_ADDENDUM) * 3  # ADK's own instruction is kept


def test_the_guide_reads_by_section() -> None:
    whole = petri_authoring_guide()
    assert "the loop" in whole["sections"]
    race = petri_authoring_guide("permit race")
    assert race["text"].startswith("### Permit race")
    assert "error" in petri_authoring_guide("no such heading")
    assert '"$defs"' in petri_schema()["schema"]


def make_app(root: Path) -> Path:
    root.mkdir()
    (root / "agent.py").write_text((HERO / "agent.py").read_text())
    (root / "__init__.py").write_text("")
    return root


async def test_a_broken_net_is_not_written(tmp_path: Path) -> None:
    app = make_app(tmp_path / "app")
    bad = (
        (HERO / "race.yaml")
        .read_text()
        .replace("inhibit: [won], node: fast", "inhibt: [won], node: fast")
    )
    r = await write_petri_blueprints({"root_agent.yaml": bad}, tool_context(app))
    assert r["success"] is False
    assert r["summary"].startswith("root_agent.yaml: does not load: ")
    assert r["summary"].endswith("Nothing was written.")
    report = r["files"]["root_agent.yaml"]
    assert report["key_path"] == "transitions.Race_RunA.inhibt"
    assert report["hint"] == "did you mean 'inhibit'?"
    assert str(tmp_path) not in report["error"]
    assert not (app / "root_agent.yaml").exists()


async def test_a_good_net_is_written_then_checked(tmp_path: Path) -> None:
    """Each write checks its own staged copy: an earlier copy (deleted) is not cached."""
    app = make_app(tmp_path / "app")
    r = await write_petri_blueprints(
        {"root_agent.yaml": (HERO / "race.yaml").read_text()}, tool_context(app)
    )
    assert r["success"] is True, r
    assert r["summary"] == (
        "root_agent.yaml: net 'race' loads (8 places, 5 transitions); 2 claims, not verified yet"
    )
    assert (app / "root_agent.yaml").read_text() == (HERO / "race.yaml").read_text()
    check = await check_petri_blueprint("root_agent.yaml", tool_context(app))
    assert check["ok"] is True
    assert check["summary"].startswith("net 'race' loads (8 places")


async def test_paths_stay_inside_the_root(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    r = await write_petri_blueprints({"../escape.yaml": "a: 1"}, tool_context(app))
    assert r["success"] is False
    assert not (tmp_path / "escape.yaml").exists()
    r = await write_petri_blueprints({"agent.py": "x = 1"}, tool_context(app))
    assert r["success"] is False


@requires_z3
async def test_verify_hands_the_model_the_counterexample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proof_view, "_BASE", [""])  # as under `adk-libpetri web`
    app = tmp_path / "hero_app"
    app.mkdir()
    (app / "agent.py").write_text((HERO / "agent.py").read_text())
    (app / "__init__.py").write_text("")
    (app / "root_agent.yaml").write_text((HERO / "race_naive.yaml").read_text())
    r = await verify_petri_blueprint("root_agent.yaml", tool_context(app))
    assert r["ok"] is False
    bound = next(c for c in r["claims"] if c["kind"] == "place_bound")
    assert bound["verdict"] == "violated"
    # Compact for the model: the steps as changes, and a picture line to copy.
    assert "fires" not in bound and "markings" not in bound
    assert bound["places"] == ["eventOut"] and bound["bound"] == 1
    assert bound["steps"][0] == "0. initial marking: userIn"
    assert bound["steps"][-1].endswith("<- eventOut=2 exceeds the bound 1")
    assert bound["picture"].startswith('<a href="/dev/petri/counterexamples/')
    proven = next(c for c in r["claims"] if c["kind"] == "deadlock_free")
    assert "steps" not in proven and "picture" not in proven and "report" not in proven
    assert "picture" in r["next"]
    assert r["summary"] == f"1 of 2 claims proven; violated: {bound['label']}."
    assert "Save" in r["next"]
    # The builder canvas reads the verdicts back, while the net's files are unchanged.
    known = known_verdicts(app / "root_agent.yaml")
    assert known is not None and known.violated == 1
    assert not known.graphs  # kept without the nets
    # A node's code changed since: no longer this net's verdicts.
    (app / "agent.py").write_text((app / "agent.py").read_text() + "\n# changed\n")
    assert known_verdicts(app / "root_agent.yaml") is None


@requires_z3
async def test_without_a_server_for_pictures_the_steps_still_come(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proof_view, "_BASE", [])  # stock `adk web`
    app = tmp_path / "hero_app"
    app.mkdir()
    (app / "agent.py").write_text((HERO / "agent.py").read_text())
    (app / "__init__.py").write_text("")
    (app / "root_agent.yaml").write_text((HERO / "race_naive.yaml").read_text())
    r = await verify_petri_blueprint("root_agent.yaml", tool_context(app))
    bound = next(c for c in r["claims"] if c["verdict"] == "violated")
    assert bound["steps"] and "picture" not in bound


def test_the_verdict_line_says_only_what_was_proven() -> None:
    def claim(label: str, verdict: str) -> ClaimResult:
        return ClaimResult("race", label, "place_bound", verdict)  # type: ignore[arg-type]

    proven = VerifyReport("x.yaml", ("race",), (claim("a", "proven"), claim("b", "proven")))
    assert verdict_line(proven) == "2 of 2 claims proven."
    mixed = VerifyReport(
        "x.yaml", ("race",), (claim("a", "proven"), claim("b", "unknown")), z3=False
    )
    assert verdict_line(mixed) == "1 of 2 claims proven; unknown: b (no z3 binary was found)."
    assert verdict_line(VerifyReport("x.yaml")).startswith("Nothing to verify")
    assert "does not load" in verdict_line(VerifyReport("x.yaml", error="boom"))


@pytest.mark.parametrize("k", [0, 2])
async def test_verify_takes_k(tmp_path: Path, k: int) -> None:
    app = tmp_path / "hero_k"
    app.mkdir()
    (app / "agent.py").write_text((HERO / "agent.py").read_text())
    (app / "__init__.py").write_text("")
    (app / "root_agent.yaml").write_text((HERO / "race.yaml").read_text())
    r = await verify_petri_blueprint("root_agent.yaml", tool_context(app), k=k)
    assert r["error"] is None
    assert len(r["claims"]) == 2
