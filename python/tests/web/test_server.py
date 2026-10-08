"""``adk-libpetri web``: ADK's dev server, Petri-aware, plus the unlisted ``/petri`` tool."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adk_libpetri.web import PetriAgentLoader, build_app, server
from adk_libpetri.web.loader import net_nodes
from support.smt_proofs import requires_z3


@pytest.fixture
def served(agents: Path) -> Iterator[tuple[TestClient, PetriAgentLoader]]:
    loader = PetriAgentLoader(str(agents))
    app = build_app(str(agents), loader=loader)
    with TestClient(app) as client:
        yield client, loader
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()


def test_the_page_and_adks_dev_ui_are_both_served(
    served: tuple[TestClient, PetriAgentLoader],
) -> None:
    client, _ = served
    page = client.get("/petri")
    assert page.status_code == 200
    assert "Petri View" in page.text
    assert client.get("/petri/static/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/petri/static/../server.py").status_code == 404
    assert client.get("/list-apps").json() == ["race", "race_naive"]


def test_the_petri_page_is_unlisted(served: tuple[TestClient, PetriAgentLoader]) -> None:
    client, _ = served
    paths = client.get("/openapi.json").json()["paths"]
    assert "/run" in paths
    assert not [p for p in paths if p.startswith("/petri")]


def test_the_petri_page_is_optional(agents: Path) -> None:
    loader = PetriAgentLoader(str(agents))
    with TestClient(build_app(str(agents), loader=loader, petri_page=False)) as client:
        assert client.get("/petri").status_code == 404
        assert client.get("/list-apps").json() == ["race", "race_naive"]


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


def test_apps_list_their_blueprints(served: tuple[TestClient, PetriAgentLoader]) -> None:
    client, _ = served
    apps = {a["name"]: a for a in client.get("/petri/api/apps").json()}
    assert apps["race"]["files"] == [{"path": "root_agent.yaml", "blueprint": True}]


def test_files_stay_inside_the_app(served: tuple[TestClient, PetriAgentLoader]) -> None:
    client, _ = served
    assert client.get("/petri/api/apps/race/files/root_agent.yaml").status_code == 200
    assert client.get("/petri/api/apps/race/files/agent.py").status_code == 400
    assert (
        client.get("/petri/api/apps/race/files/..%2Frace_naive%2Froot_agent.yaml").status_code
        == 400
    )
    assert client.get("/petri/api/apps/nope/files/root_agent.yaml").status_code == 404
    special = "/petri/api/apps/__adk_agent_builder_assistant/files/x.yaml"
    assert client.get(special).status_code == 404
    assert client.put("/petri/api/apps/race/files/x.py", json={"content": ""}).status_code == 400


def test_a_save_checks_the_blueprint_and_names_the_key(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    good = (agents / "race" / "root_agent.yaml").read_text()
    bad = good.replace("inhibit: [won], node: fast", "inhibt: [won], node: fast")
    r = client.put("/petri/api/apps/race/files/root_agent.yaml", json={"content": bad}).json()
    assert r["written"] is True
    check = r["check"]
    assert check["ok"] is False
    assert check["key_path"] == "transitions.Race_RunA.inhibt"
    assert check["hint"] == "did you mean 'inhibit'?"
    assert str(agents) not in check["error"]  # the page sees app-relative paths
    r = client.put("/petri/api/apps/race/files/root_agent.yaml", json={"content": good}).json()
    assert r["check"]["ok"] is True
    assert r["check"]["net"]["transitions"] == 5


def test_the_net_route_draws_a_marking(served: tuple[TestClient, PetriAgentLoader]) -> None:
    client, _ = served
    r = client.get(
        "/petri/api/apps/race/net",
        params={"file": "root_agent.yaml", "marking": '{"won": 1}', "fired": "Race_Commit"},
    ).json()
    assert r["graph"]["name"] == "race"
    assert "won<BR/>" in r["dot"] and "●</FONT>" in r["dot"]
    without_file = client.get("/petri/api/apps/race/net").json()
    assert without_file["graph"]["name"] == "race"
    assert client.get("/petri/api/apps/race/net", params={"marking": "[1]"}).status_code == 400


@requires_z3
def test_verify_returns_each_claim_and_the_counterexample(
    served: tuple[TestClient, PetriAgentLoader],
) -> None:
    client, _ = served
    fixed = client.post("/petri/api/apps/race/verify", json={"file": "root_agent.yaml"}).json()
    assert fixed["ok"] is True
    assert [c["verdict"] for c in fixed["claims"]] == ["proven", "proven"]
    naive = client.post(
        "/petri/api/apps/race_naive/verify", json={"file": "root_agent.yaml"}
    ).json()
    assert naive["ok"] is False
    bound = next(c for c in naive["claims"] if c["kind"] == "place_bound")
    assert bound["verdict"] == "violated"
    assert len(bound["markings"]) == len(bound["fires"]) + 1
    assert bound["markings"][-1]["eventOut"] == 2


def test_a_run_through_adks_api_is_traced(served: tuple[TestClient, PetriAgentLoader]) -> None:
    client, _ = served
    sid = client.post("/apps/race/users/u/sessions", json={}).json()["id"]
    events = client.post(
        "/run",
        json={
            "appName": "race",
            "userId": "u",
            "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text": "go"}]},
        },
    ).json()
    assert any(e.get("output") in ("fast answer", "slow answer") for e in events)
    sessions = client.get("/petri/api/apps/race/sessions").json()
    assert [s["session"] for s in sessions] == [sid]
    trace = client.get("/petri/api/apps/race/trace", params={"user": "u", "session": sid}).json()
    (steps,) = [t["steps"] for t in trace.values()]
    assert steps[0]["kind"] == "turn"
    assert "Race_Commit" in [s["transition"] for s in steps]
    missing = client.get("/petri/api/apps/race/trace", params={"user": "u", "session": "x"})
    assert missing.status_code == 404


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


def test_a_write_meets_adks_upload_check(served: tuple[TestClient, PetriAgentLoader]) -> None:
    client, _ = served
    url = "/petri/api/apps/race/files/root_agent.yaml"
    net = client.get(url).json()["content"]
    # The blueprint's own class (adk_libpetri.net.PetriNet) is not a reference outside the app.
    assert client.put(url, json={"content": net}).status_code == 200
    outside = net + "before_agent_callbacks: [{name: other.hooks.evil}]\n"
    r = client.put(url, json={"content": outside})
    assert r.status_code == 400 and "other.hooks.evil" in r.json()["detail"]
    r = client.put("/petri/api/apps/race/files/x.yaml", json={"content": "tools: [{args: {}}]\n"})
    assert r.status_code == 400 and "args" in r.json()["detail"]
    inside = "agent_class: LlmAgent\nname: x\ntools: [race.agent.fast]\n"
    r = client.put("/petri/api/apps/race/files/x.yaml", json={"content": inside})
    assert r.status_code == 200
    assert client.get(url).json()["content"] == net


def test_a_nested_app_is_listed_and_served(agents: Path) -> None:
    group = agents / "group"
    group.mkdir()
    (agents / "race").rename(group / "race")
    loader = PetriAgentLoader(str(agents))
    with TestClient(build_app(str(agents), loader=loader)) as client:
        assert "group.race" in client.get("/list-apps").json()
        dot = client.get("/dev/apps/group.race/build_graph_image").json()[""]["dotSrc"]
        assert "Race_Commit" in dot
        apps = {a["name"]: a for a in client.get("/petri/api/apps").json()}
        assert apps["group.race"]["files"] == [{"path": "root_agent.yaml", "blueprint": True}]
        net = client.get("/petri/api/apps/group.race/net").json()
        assert "Race_Commit" in net["dot"]
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
    from adk_libpetri.web import create_petri_builder_assistant, petri_router, serve

    for obj in (PetriAgentLoader, build_app, create_petri_builder_assistant, petri_router, serve):
        assert is_experimental(obj), obj
