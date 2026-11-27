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
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: ran.append(port))
    server.serve(str(agents), port=8123)
    assert ran == [8123]
    assert capsys.readouterr().out == "ADK dev UI: http://127.0.0.1:8123/dev-ui/\n"


def test_a_loopback_host_turns_on_adks_dns_rebinding_guard(agents: Path) -> None:
    """``build_app`` passes ``host`` to ADK as its ``bind_host``, as ``adk web`` does:
    on a loopback bind, a request addressed to another host is refused."""
    with TestClient(build_app(str(agents), host="127.0.0.1")) as client:
        assert client.get("/list-apps", headers={"host": "127.0.0.1:8000"}).status_code == 200
        assert client.get("/list-apps", headers={"host": "evil.example"}).status_code == 403


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


def test_the_public_surface_is_experimental() -> None:
    from adk_libpetri._experimental import is_experimental
    from adk_libpetri.web import serve

    for obj in (PetriAgentLoader, build_app, serve):
        assert is_experimental(obj), obj
