"""``adk-libpetri web``: ADK's dev server, Petri-aware."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adk_libpetri.bridge import MarkingTraces
from adk_libpetri.web import PetriAgentLoader, build_app, server
from adk_libpetri.web.loader import net_nodes


@pytest.fixture
def served(agents: Path) -> Iterator[tuple[TestClient, PetriAgentLoader]]:
    loader = PetriAgentLoader(str(agents))
    app = build_app(str(agents), loader=loader)
    with TestClient(app) as client:
        yield client, loader
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()


def test_adks_dev_ui_is_served_and_nothing_else(
    served: tuple[TestClient, PetriAgentLoader],
) -> None:
    client, _ = served
    assert client.get("/list-apps").json() == ["race", "race_naive"]
    assert client.get("/petri").status_code == 404


def test_serve_points_at_adks_dev_ui_only(
    agents: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    ran: list[int] = []
    monkeypatch.chdir(agents)  # serve() changes directory; this restores it
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: ran.append(port))
    server.serve(str(agents), port=8123)
    assert ran == [8123]
    assert capsys.readouterr().out == "ADK dev UI: http://127.0.0.1:8123/dev-ui/\n"


def test_a_loader_given_traces_traces_runs_through_adks_api(agents: Path) -> None:
    traces = MarkingTraces()
    loader = PetriAgentLoader(str(agents), traces=traces)
    with TestClient(build_app(str(agents), loader=loader)) as client:
        sid = client.post("/apps/race/users/u/sessions", json={}).json()["id"]
        client.post(
            "/run",
            json={
                "appName": "race",
                "userId": "u",
                "sessionId": sid,
                "newMessage": {"role": "user", "parts": [{"text": "go"}]},
            },
        )
        trace = traces.trace("race", "u", sid)
        assert trace is not None
        (steps,) = [t["steps"] for t in trace.values()]
        assert steps[0]["kind"] == "turn"
        assert "Race_Commit" in [s["transition"] for s in steps]
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()


def test_a_loader_traces_nothing_by_default(agents: Path) -> None:
    assert PetriAgentLoader(str(agents)).traces is None


def _turn(client: TestClient, app: str) -> list[object]:
    sid = client.post(f"/apps/{app}/users/u/sessions", json={}).json()["id"]
    events = client.post(
        "/run",
        json={
            "appName": app,
            "userId": "u",
            "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text": "go"}]},
        },
    ).json()
    return [e.get("output") for e in events if e.get("output") is not None]


def test_after_save_chat_runs_the_saved_net(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    """ADK keeps one runner per app; Save drops it, so chat and the graph panel agree."""
    client, _ = served
    assert "fast answer" in _turn(client, "race")
    # The builder assistant changes the draft; the canvas saves (tmp, then real).
    assert client.get("/dev/apps/race/builder?tmp=true").status_code == 200
    draft = agents / "race" / "tmp" / "race"
    code = (draft / "agent.py").read_text().replace('"fast answer"', '"NEW fast answer"')
    (draft / "agent.py").write_text(code)
    net = (draft / "root_agent.yaml").read_text().replace("Race_Commit", "Race_Answer")
    (draft / "root_agent.yaml").write_text(net)
    yaml_text = b"name: race\nagent_class: LlmAgent\n"
    canvas = ("files", ("race/root_agent.yaml", yaml_text, "text/yaml"))
    for tmp in ("?tmp=true", ""):
        assert client.post(f"/dev/apps/race/builder/save{tmp}", files=[canvas]).json() is True
    graph = client.get("/dev/apps/race/build_graph_image").json()[""]["dotSrc"]
    assert "Race_Answer" in graph
    outputs = _turn(client, "race")
    assert "NEW fast answer" in outputs and "fast answer" not in outputs


def test_the_server_finds_adks_runner_cache(agents: Path) -> None:
    app = build_app(str(agents))
    adk = server.adk_web_server(app)
    assert adk is not None and isinstance(adk.runners_to_clean, set)


def test_a_helper_package_is_not_an_app(agents: Path) -> None:
    helper = agents / "helpers"
    helper.mkdir()
    (helper / "__init__.py").write_text('"""Functions the nets refer to."""\n')
    (helper / "agent.py").write_text("def brief(x: object) -> str:\n    return 'b'\n")
    py_app = agents / "py_app"
    py_app.mkdir()
    (py_app / "__init__.py").write_text("from . import agent\n")
    (py_app / "agent.py").write_text("root_agent = None\n")
    # An app whose agent module is a package, the agent in a module beside it.
    pkg_app = agents / "pkg_app"
    (pkg_app / "agent").mkdir(parents=True)
    (pkg_app / "__init__.py").write_text("from . import agent\n")
    (pkg_app / "agent" / "__init__.py").write_text("from .core import *  # noqa\n")
    (pkg_app / "agent" / "core.py").write_text("root_agent = None\n")
    names = PetriAgentLoader(str(agents)).list_agents()
    assert "helpers" not in names
    assert {"race", "race_naive", "py_app", "pkg_app"} <= set(names)


def test_a_nested_app_is_listed_and_served(agents: Path) -> None:
    group = agents / "group"
    group.mkdir()
    (agents / "race").rename(group / "race")
    loader = PetriAgentLoader(str(agents))
    with TestClient(build_app(str(agents), loader=loader)) as client:
        assert "group.race" in client.get("/list-apps").json()
        dot = client.get("/dev/apps/group.race/build_graph_image").json()[""]["dotSrc"]
        assert "Race_Commit" in dot
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()


def test_a_missing_runner_cache_is_logged(
    agents: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(server, "adk_web_server", lambda app: None)
    with caplog.at_level("WARNING", logger=server.__name__):
        build_app(str(agents))
    assert "AdkWebServer was not found" in caplog.text


def test_the_public_surface_is_experimental() -> None:
    from adk_libpetri._experimental import is_experimental
    from adk_libpetri.web import create_petri_builder_assistant, serve

    for obj in (PetriAgentLoader, build_app, create_petri_builder_assistant, serve):
        assert is_experimental(obj), obj
