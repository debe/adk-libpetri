"""ADK's builder canvas shows a net's root with what it runs: the overridden ``GET builder``."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from adk_libpetri.net import PetriNet
from adk_libpetri.net.report import ClaimResult, VerifyReport
from adk_libpetri.web import PetriAgentLoader, build_app, builder
from adk_libpetri.web.builder import remember_verdicts
from adk_libpetri.web.loader import net_nodes

from .conftest import add_composed_app, add_workflow_app

BUILDER = "/dev/apps/{app}/builder"
SAVE = "/dev/apps/{app}/builder/save"

PLAIN = """name: plain
model: gemini-2.5-flash
agent_class: LlmAgent
instruction: Be brief.
"""


@pytest.fixture(autouse=True)
def _no_verdicts() -> Iterator[None]:
    """Verdicts are kept per process, by text: other tests verify the same hero net."""
    with builder._VERDICTS_LOCK:
        builder._VERDICTS.clear()
    yield


@pytest.fixture
def served(agents: Path) -> Iterator[tuple[TestClient, PetriAgentLoader]]:
    plain = agents / "plain"
    plain.mkdir()
    (plain / "root_agent.yaml").write_text(PLAIN)
    loader = PetriAgentLoader(str(agents))
    with TestClient(build_app(str(agents), loader=loader)) as client:
        yield client, loader
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()


def _card(client: TestClient, app: str, **params: str) -> dict[str, object]:
    r = client.get(BUILDER.format(app=app), params=params)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return yaml.safe_load(r.text)


def test_a_net_root_shows_what_it_runs(served: tuple[TestClient, PetriAgentLoader]) -> None:
    client, _ = served
    for params in ({}, {"tmp": "true"}, {"file_path": "root_agent.yaml", "tmp": "true"}):
        card = _card(client, "race", **params)
        assert card == {
            "name": "race",
            "agent_class": "adk_libpetri.net.PetriNet",
            "description": "Petri net: 8 places, 5 transitions; 2 claims, not verified yet",
            "tools": [{"name": "race.agent.fast"}, {"name": "race.agent.slow"}],
        }


def test_the_draft_is_what_the_canvas_shows(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, loader = served
    loader.load_agent("race")  # the app is served: its package is imported
    app_module = sys.modules["race.agent"]
    _card(client, "race", tmp="true")
    # The builder assistant rewrites the draft: one branch, its own function.
    draft = agents / "race" / "tmp" / "race"
    (draft / "agent.py").write_text(
        (draft / "agent.py").read_text() + "\n\ndef only(node_input: object) -> str:\n"
        "    return 'only'\n"
    )
    text = (draft / "root_agent.yaml").read_text()
    text = text.replace("nodes: [[.agent.fast], [.agent.slow]]", "nodes: [[.agent.only]]")
    text = text.replace("node: fast", "node: only").replace("node: slow", "node: only")
    (draft / "root_agent.yaml").write_text(text)
    card = _card(client, "race", tmp="true")
    assert card["tools"] == [{"name": "race.agent.only"}]
    # The app itself is untouched, on disk and in sys.modules.
    assert _card(client, "race")["tools"] == [
        {"name": "race.agent.fast"},
        {"name": "race.agent.slow"},
    ]
    assert sys.modules["race.agent"] is app_module
    assert not [k for k in sys.modules if k.startswith("_petri_stage_")]
    assert isinstance(loader.load_agent("race"), PetriNet)


def test_a_draft_that_does_not_load_says_why(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    _card(client, "race", tmp="true")
    draft = agents / "race" / "tmp" / "race" / "root_agent.yaml"
    draft.write_text(draft.read_text().replace("inhibit: [won], node: fast", "inhibt: [won]"))
    card = _card(client, "race", tmp="true")
    assert card["agent_class"] == "adk_libpetri.net.PetriNet"
    assert "tools" not in card
    assert str(card["description"]).startswith("Petri net that does not load yet: ")
    assert str(agents) not in str(card["description"])
    # Half-typed YAML (a bracket missing): still the net's class, not null.
    draft.write_text(draft.read_text().replace("{in: [triggerA]", "{in: [triggerA", 1))
    assert "{in: [triggerA, out" in draft.read_text()
    broken = _card(client, "race", tmp="true")
    assert broken["agent_class"] == "adk_libpetri.net.PetriNet"
    assert str(broken["description"]).startswith("Petri net that does not load yet: ")


def test_the_last_verdicts_show(served: tuple[TestClient, PetriAgentLoader], agents: Path) -> None:
    client, _ = served
    path = agents / "race" / "root_agent.yaml"
    claims = (
        ClaimResult("race", "one answer", "place_bound", "proven"),
        ClaimResult("race", "deadlock_free", "deadlock_free", "violated"),
    )
    remember_verdicts(str(path), VerifyReport(str(path), ("race",), claims))
    card = _card(client, "race")
    assert (
        card["description"]
        == "Petri net: 8 places, 5 transitions; 1 of 2 claims proven; violated: deadlock_free"
    )
    # The draft holds the same files until the assistant changes one.
    assert _card(client, "race", tmp="true")["description"] == card["description"]
    # Any file the net loads, not only its root: a node's code changed in the draft.
    code = agents / "race" / "tmp" / "race" / "agent.py"
    code.write_text(code.read_text() + "\n# changed by the assistant\n")
    assert _card(client, "race", tmp="true")["description"].endswith("2 claims, not verified yet")
    assert _card(client, "race")["description"] == card["description"]


def test_the_card_round_trips_through_the_canvas_save(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    net = (agents / "race" / "root_agent.yaml").read_text()
    card = _card(client, "race", tmp="true")
    # What the canvas's generateYamlFile sends for that root: tools are dropped
    # for any agent_class but LlmAgent; the description is echoed.
    echo = {k: card[k] for k in ("name", "agent_class", "description")} | {"sub_agents": []}
    upload = ("files", ("race/root_agent.yaml", yaml.safe_dump(echo).encode(), "text/yaml"))
    for tmp in ("?tmp=true", "?tmp=true", ""):
        r = client.post(SAVE.format(app="race") + tmp, files=[upload])
        assert r.status_code == 200 and r.json() is True
    assert (agents / "race" / "root_agent.yaml").read_text() == net


def test_everything_else_is_adks(served: tuple[TestClient, PetriAgentLoader]) -> None:
    client, _ = served
    assert client.get(BUILDER.format(app="plain")).text == PLAIN
    assert client.get(BUILDER.format(app="plain"), params={"tmp": "true"}).text == PLAIN
    # Another file of a net app, a missing file, a missing or invalid app.
    r = client.get(BUILDER.format(app="race"), params={"file_path": "agent.py"})
    assert r.status_code == 200 and r.text == ""  # ADK: only .yaml/.yml
    r = client.get(BUILDER.format(app="race"), params={"file_path": "plugins.yaml", "tmp": "true"})
    assert r.text == ""
    assert client.get(BUILDER.format(app="missing")).text == ""
    assert client.get(BUILDER.format(app="..")).status_code in (200, 404)
    # Only the app's own root file: not one a path walks out to.
    for path in ("../root_agent.yaml", "sub/../../root_agent.yaml", "x/root_agent.yaml"):
        r = client.get(BUILDER.format(app="race"), params={"file_path": path})
        assert "Petri net" not in r.text, path
    assert (
        "Petri net"
        in client.get(BUILDER.format(app="race"), params={"file_path": "./root_agent.yaml"}).text
    )


def test_a_workflow_root_shows_what_it_runs(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    add_workflow_app(agents)
    for params in ({}, {"tmp": "true"}):
        card = _card(client, "loop_config", **params)
        assert card["agent_class"] == "adk_libpetri.workflow.PetriWorkflow"
        assert str(card["description"]).startswith("Petri workflow: ")
        names = [t["name"] for t in card["tools"]]
        # Its functions by ref, its agents by the YAML file the workflow loads.
        assert "loop_config.agent.process_input" in names
        assert "generate_headline.yaml" in names and "evaluate_headline.yaml" in names
        assert all("." in n for n in names)


def test_a_stock_subnets_agent_is_named_by_its_file(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    add_composed_app(agents)
    loader = PetriAgentLoader(str(agents))
    with TestClient(build_app(str(agents), loader=loader)) as client:
        names = [t["name"] for t in _card(client, "composed")["tools"]]
    assert "yaml_composed/helper.yaml" in names  # the summarizer, where the net loads it
    assert "yaml_race.agent.fast" in names
    assert not [n for n in names if n.startswith("LlmAgent.")]
