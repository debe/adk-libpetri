"""ADK's graph panel draws a net as a Petri net: the overridden ``build_graph_image``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adk_libpetri.net import BlueprintError
from adk_libpetri.web import PetriAgentLoader, build_app
from adk_libpetri.web.loader import net_nodes

from .conftest import add_composed_app, add_workflow_app

GRAPH = "/dev/apps/{app}/build_graph_image"

PLAIN = """name: plain
model: gemini-2.5-flash
agent_class: LlmAgent
instruction: Be brief.
"""


@pytest.fixture
def client(agents: Path) -> Iterator[TestClient]:
    plain = agents / "plain"
    plain.mkdir()
    (plain / "root_agent.yaml").write_text(PLAIN)
    add_workflow_app(agents)
    broken = agents / "broken"
    broken.mkdir()
    (broken / "root_agent.yaml").write_text(BROKEN)
    loader = PetriAgentLoader(str(agents))
    with TestClient(build_app(str(agents), loader=loader)) as c:
        yield c
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()


BROKEN = """agent_class: adk_libpetri.net.PetriNet
name: broken
transitions:
  B_Go: {in: [nowhere], out: eventOut, action: emit}
"""


def _dot(client: TestClient, app: str, **params: str) -> dict[str, str]:
    r = client.get(GRAPH.format(app=app), params=params)
    assert r.status_code == 200, r.text
    return {path: v["dotSrc"] for path, v in r.json().items()}


def test_a_net_app_gets_the_petri_drawing_in_the_uis_theme(client: TestClient) -> None:
    light = _dot(client, "race", dark_mode="false")
    assert list(light) == [""]
    assert 'id="place:won"' in light[""]
    assert 'bgcolor="#F8FAFC"' in light[""]
    assert "node:fast" in light[""]  # the UI lights Race_RunA up when `fast` answers
    dark = _dot(client, "race", dark_mode="true")
    assert 'bgcolor="#0F172A"' in dark[""]
    assert _dot(client, "race") == light  # dark_mode defaults to false, as ADK's


def test_the_nets_name_lights_nothing(client: TestClient) -> None:
    """Every event under the net (its answer, its nodes' events, an error) is authored
    by the net: a node it matched would light on each of them. Only an invisible
    decoy is titled with it (the titles ``Race_*`` hold it, and stay exact)."""
    dot = _dot(client, "race")[""]
    assert '"eventOut" [id="place:eventOut"' in dot
    [named] = [line for line in dot.splitlines() if line.strip().startswith('"race" [')]
    assert 'id="net:race"' in named and "style=invis" in named


def test_a_workflow_app_gets_the_petri_drawing(client: TestClient) -> None:
    dot = _dot(client, "loop_config", dark_mode="true")[""]
    assert 'id="transition:Wf_generate_headline_Run"' in dot
    assert "node:generate_headline" in dot
    assert 'bgcolor="#0F172A"' in dot


def test_other_apps_and_apps_that_do_not_load_get_adks_own_answer(client: TestClient) -> None:
    plain = _dot(client, "plain", dark_mode="true")
    assert "plain" in plain[""]
    assert "place:" not in plain[""]
    r = client.get(GRAPH.format(app="race"), params={"node": "nope"})
    assert r.status_code == 404  # not a subnet: ADK's "Node not found"
    assert client.get(GRAPH.format(app="missing")).status_code == 404  # ADK's error
    # A net that does not load: ADK's handler runs, and its loader raises (a 500).
    with pytest.raises(BlueprintError, match="unknown place 'nowhere'"):
        client.get(GRAPH.format(app="broken"))


def test_each_subnet_is_drawn_and_opens_on_its_own(tmp_path: Path) -> None:
    """ADK's "Agent Structure" view preloads every path's drawing, or asks for one by node=."""
    agents = tmp_path / "agents"
    agents.mkdir()
    add_composed_app(agents)
    loader = PetriAgentLoader(str(agents))
    with TestClient(build_app(str(agents), loader=loader)) as c:
        drawn = _dot(c, "composed", dark_mode="true")
        assert set(drawn) == {"", "first", "second", "assistant"}
        assert '"first" [id="subnet:first"' in drawn[""]  # collapsed: click it to open
        assert 'id="transition:Race_RunBranchA"' in drawn["first"]
        r = c.get(GRAPH.format(app="composed"), params={"node": "first", "dark_mode": "true"})
        assert r.status_code == 200
        assert r.json()["dotSrc"] == drawn["first"]
        named = c.get(GRAPH.format(app="composed"), params={"node": "composed_agent/assistant"})
        assert "LlmStep_LlmCall" in named.json()["dotSrc"]
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()
