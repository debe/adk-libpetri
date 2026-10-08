"""ADK's builder canvas cannot write over a net: the guarded ``builder/save``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from adk_libpetri.net import PetriNet
from adk_libpetri.web import PetriAgentLoader, build_app
from adk_libpetri.web.builder_guard import take_note
from adk_libpetri.web.loader import net_nodes
from adk_libpetri.workflow import PetriWorkflow

from .conftest import HERO, add_workflow_app

SAVE = "/dev/apps/{app}/builder/save"

# What ADK's canvas (generateYamlFile) sends for a root it loaded from a net:
# agent_class kept, model/instruction/tools dropped (not an LlmAgent), an
# empty description left out.
CANVAS_NET = """name: race
agent_class: adk_libpetri.net.PetriNet
{description}sub_agents: []
"""

# ... and when the agent type was changed to LlmAgent in the canvas.
CANVAS_LLM = """name: race
model: gemini-2.5-flash
agent_class: LlmAgent
instruction: You are the root agent.
sub_agents: []
tools: []
"""

PLAIN = """name: plain
model: gemini-2.5-flash
agent_class: LlmAgent
instruction: Be brief.
"""

PLAIN_EDITED = """name: plain
model: gemini-2.5-flash
agent_class: LlmAgent
description: Edited in the canvas
instruction: Be very brief.
sub_agents: []
tools:
  - name: google_search
"""


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


def _save(client: TestClient, app: str, content: str, *, tmp: bool, name: str = "") -> object:
    url = SAVE.format(app=app) + ("?tmp=true" if tmp else "")
    upload = ("files", (name or f"{app}/root_agent.yaml", content.encode(), "application/x-yaml"))
    r = client.post(url, files=[upload])
    return r.status_code, r.json()


def _canvas(description: str = "") -> str:
    line = f"description: {json.dumps(description)}\n" if description else ""
    return CANVAS_NET.format(description=line)


def test_the_canvas_draft_save_keeps_the_net(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    net = (agents / "race" / "root_agent.yaml").read_text()
    for canvas in (_canvas(), CANVAS_LLM):
        assert _save(client, "race", canvas, tmp=True) == (200, True)
        draft = agents / "race" / "tmp" / "race"
        assert (draft / "root_agent.yaml").read_text() == net
        # The draft is the whole app, as ADK's GET builder?tmp=true makes it.
        assert (draft / "agent.py").is_file()
    assert (agents / "race" / "root_agent.yaml").read_text() == net


def test_save_keeps_the_net_and_the_app_still_loads(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, loader = served
    net = (agents / "race" / "root_agent.yaml").read_text()
    loader.load_agent("race")
    # The canvas's Save: the tmp save, then the real one, the same form.
    assert _save(client, "race", _canvas(), tmp=True) == (200, True)
    assert _save(client, "race", _canvas(), tmp=False) == (200, True)
    assert (agents / "race" / "root_agent.yaml").read_text() == net
    assert not (agents / "race" / "tmp").exists()
    assert "race" not in loader._agent_cache
    assert isinstance(loader.load_agent("race"), PetriNet)


def test_the_assistants_net_survives_the_draft_save_and_save_ships_it(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, loader = served
    # The builder assistant (write_petri_blueprints) writes into the draft.
    assert client.get("/dev/apps/race/builder?tmp=true").status_code == 200
    draft = agents / "race" / "tmp" / "race" / "root_agent.yaml"
    written = (HERO / "race_naive.yaml").read_text()
    draft.write_text(written)
    # The canvas reloads from the draft, then sends its YAML before the next message.
    loaded = yaml.safe_load(client.get("/dev/apps/race/builder?tmp=true").text)
    canvas = _canvas(loaded.get("description", ""))
    assert _save(client, "race", canvas, tmp=True) == (200, True)
    assert draft.read_text() == written
    assert _save(client, "race", canvas, tmp=True) == (200, True)
    assert _save(client, "race", canvas, tmp=False) == (200, True)
    assert (agents / "race" / "root_agent.yaml").read_text() == written
    assert isinstance(loader.load_agent("race"), PetriNet)


def test_nothing_of_the_canvas_yaml_reaches_a_net(
    served: tuple[TestClient, PetriAgentLoader], agents: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = served
    path = agents / "race" / "root_agent.yaml"
    net = path.read_text()
    # A stale description (the canvas's echo of an older draft), and a
    # sub-agent and a callback added to the net root in the canvas.
    canvas = _canvas("An older description").replace(
        "sub_agents: []\n",
        "sub_agents:\n  - config_path: ./helper.yaml\nbefore_agent_callbacks:\n  - name: cb\n",
    )
    assert _save(client, "race", canvas, tmp=True) == (200, True)
    assert path.read_text() == net
    assert "dropped the canvas's ['before_agent_callbacks', 'sub_agents']" in caplog.text
    # The builder assistant tells the user, once.
    note = take_note("race")
    assert note is not None
    assert "before_agent_callbacks (cb); sub_agents (helper)" in note
    assert "renamed on the canvas are not saved" in note
    assert take_note("race") is None
    # Save with the addition still on the canvas: nothing written, and false, so
    # the builder stays open instead of closing as if it were saved. The canvas
    # sends its tmp save first, and again before the next message.
    assert _save(client, "race", canvas, tmp=True) == (200, True)
    assert _save(client, "race", canvas, tmp=False) == (200, False)
    assert _save(client, "race", canvas, tmp=True) == (200, True)
    assert path.read_text() == net
    note = take_note("race")
    assert note is not None and "\n\nSave stopped, nothing was written" in note
    assert "Delete it on the canvas" in note and note.count("The builder canvas cannot") == 1
    # Once the canvas no longer holds it, Save goes through.
    assert _save(client, "race", _canvas("An older description"), tmp=False) == (200, True)
    assert path.read_text() == net


def test_an_app_without_a_net_is_adks_as_before(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    assert _save(client, "plain", PLAIN_EDITED, tmp=True) == (200, True)
    assert (agents / "plain" / "tmp" / "plain" / "root_agent.yaml").read_text() == PLAIN_EDITED
    assert _save(client, "plain", PLAIN_EDITED, tmp=False) == (200, True)
    assert (agents / "plain" / "root_agent.yaml").read_text() == PLAIN_EDITED
    assert not (agents / "plain" / "tmp").exists()


def test_other_files_and_bad_uploads_go_to_adk(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    # A new sub-agent file beside a net is written as ADK writes it.
    status = _save(client, "race", PLAIN, tmp=True, name="race/helper.yaml")
    assert status == (200, True)
    assert (agents / "race" / "tmp" / "race" / "helper.yaml").read_text() == PLAIN
    # An upload with a net of its own meets ADK's upload checks.
    full = (agents / "race" / "root_agent.yaml").read_text()
    code, body = _save(client, "race", full, tmp=True)
    assert code == 400 and "Blocked code reference" in body["detail"]
    code, body = _save(client, "race", _canvas(), tmp=True, name="race/../x.yaml")
    assert code == 400 and "traversal" in body["detail"]
    code, _ = _save(client, "race", _canvas(), tmp=True, name="race/root_agent.py")
    assert code == 400


def test_the_canvas_agent_files_for_a_net_root_are_not_written(
    served: tuple[TestClient, PetriAgentLoader], agents: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = served
    # "Add sub agent" on a net root: the canvas sends the root naming the new
    # agent's file, that file, and the file of an agent tool it holds.
    root = _canvas().replace(
        "sub_agents: []\n", "sub_agents:\n  - config_path: ./sub_agent_1.yaml\n"
    )
    sub = PLAIN.replace("name: plain", "name: sub_agent_1") + (
        "tools:\n  - name: AgentTool\n    args: {agent: {config_path: ./helper.yaml}}\n"
    )
    files = [
        ("files", ("race/root_agent.yaml", root.encode(), "application/x-yaml")),
        ("files", ("race/sub_agent_1.yaml", sub.encode(), "application/x-yaml")),
        ("files", ("race/helper.yaml", PLAIN.encode(), "application/x-yaml")),
        ("files", ("race/plugins.yaml", b"bigquery_agent_analytics: {}\n", "application/x-yaml")),
    ]
    for tmp, ok in (("?tmp=true", True), ("", False)):  # Save: stopped, see the note
        r = client.post(SAVE.format(app="race") + tmp, files=files)
        assert r.status_code == 200 and r.json() is ok, r.text
    for name in ("sub_agent_1.yaml", "helper.yaml"):
        assert not (agents / "race" / name).exists()
    # Not an agent: ADK's to write (in the draft; the stopped Save wrote nothing).
    assert (agents / "race" / "tmp" / "race" / "plugins.yaml").is_file()
    assert not (agents / "race" / "plugins.yaml").exists()
    assert "sub_agent_1.yaml is the canvas's agent under a net" in caplog.text


def test_a_workflow_app_is_kept_too(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    """``agent_class: adk_libpetri.workflow.PetriWorkflow``: its edges are the net."""
    client, loader = served
    app = add_workflow_app(agents)
    path = app / "root_agent.yaml"
    workflow = path.read_text()
    kept = "name: root_agent\nagent_class: adk_libpetri.workflow.PetriWorkflow\n"
    changed = CANVAS_LLM.replace("name: race", "name: root_agent")
    for canvas in (kept, changed):
        assert _save(client, "loop_config", canvas, tmp=True) == (200, True)
        assert (app / "tmp" / "loop_config" / "root_agent.yaml").read_text() == workflow
    assert _save(client, "loop_config", changed, tmp=False) == (200, True)
    assert path.read_text() == workflow
    assert isinstance(loader.load_agent("loop_config"), PetriWorkflow)


def test_a_net_that_does_not_parse_is_still_kept(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    """A half-finished edit of a net (one brace missing) fails closed."""
    client, _ = served
    path = agents / "race" / "root_agent.yaml"
    broken = path.read_text().replace("{in: [triggerA]", "{in: [triggerA", 1)
    assert broken != path.read_text()
    path.write_text(broken)
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(broken)
    for tmp in (True, False):
        assert _save(client, "race", CANVAS_LLM, tmp=tmp) == (200, True)
    assert path.read_text() == broken


def test_a_net_file_that_is_not_utf8_is_kept(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    """A net file that cannot be decoded fails closed too: the canvas's YAML is not written."""
    client, _ = served
    path = agents / "race" / "root_agent.yaml"
    raw = path.read_bytes() + b"# caf\xe9\n"
    path.write_bytes(raw)
    for tmp in (True, False):
        assert _save(client, "race", CANVAS_LLM, tmp=tmp) == (200, True)
    assert path.read_bytes() == raw


# ----------------------------------------------------------------------------
#  The draft in step with the app: Save never puts back an older file
# ----------------------------------------------------------------------------


def test_an_app_file_edited_after_the_draft_survives_save(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    app = agents / "race"
    assert client.get("/dev/apps/race/builder?tmp=true").status_code == 200
    draft = app / "tmp" / "race"
    assert (draft / "root_agent.yaml").is_file()
    # Edited in an editor while the builder is open.
    edited = (app / "root_agent.yaml").read_text() + "# EDITED\n"
    (app / "root_agent.yaml").write_text(edited)
    code = (app / "agent.py").read_text() + "# EDITOR CHANGE\n"
    (app / "agent.py").write_text(code)
    assert _save(client, "race", _canvas(), tmp=True) == (200, True)
    assert _save(client, "race", _canvas(), tmp=False) == (200, True)
    assert (app / "root_agent.yaml").read_text() == edited
    assert (app / "agent.py").read_text() == code
    assert not (app / ".adk_libpetri_draft.json").exists()  # the marker stays in the draft
    assert not (app / "tmp").exists()


def test_a_draft_left_from_before_is_refreshed_by_time(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    """A draft with no baseline (ADK made it, or an earlier server): the newer file wins."""
    import os
    import shutil

    client, _ = served
    app = agents / "race"
    draft = app / "tmp" / "race"
    draft.mkdir(parents=True)
    for name in ("__init__.py", "agent.py", "root_agent.yaml"):
        shutil.copy2(app / name, draft / name)
    code = (app / "agent.py").read_text() + "# EDITOR CHANGE\n"
    (app / "agent.py").write_text(code)
    old = (draft / "agent.py").stat().st_mtime_ns - 10**9
    os.utime(draft / "agent.py", ns=(old, old))
    assert _save(client, "race", _canvas(), tmp=True) == (200, True)
    assert _save(client, "race", _canvas(), tmp=False) == (200, True)
    assert (app / "agent.py").read_text() == code


def test_a_draft_left_from_before_with_work_in_it_is_a_conflict(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    """No baseline, the draft file written in the draft, the app's edited later: stop."""
    import shutil
    import time

    client, _ = served
    app = agents / "race"
    draft = app / "tmp" / "race"
    draft.mkdir(parents=True)
    for name in ("__init__.py", "agent.py", "root_agent.yaml"):
        shutil.copy2(app / name, draft / name)
    work = (draft / "agent.py").read_text() + "# BUILDER WORK\n"
    (draft / "agent.py").write_text(work)
    time.sleep(0.01)
    (app / "agent.py").write_text((app / "agent.py").read_text() + "# EDITOR CHANGE\n")
    assert _save(client, "race", _canvas(), tmp=True) == (200, True)
    assert _save(client, "race", _canvas(), tmp=False) == (200, False)
    assert "EDITOR CHANGE" in (app / "agent.py").read_text()
    assert (draft / "agent.py").read_text() == work
    note = take_note("race")
    assert note is not None and "agent.py changed in the app" in note


def test_a_file_changed_in_both_stops_save(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    app = agents / "race"
    assert client.get("/dev/apps/race/builder?tmp=true").status_code == 200
    draft = app / "tmp" / "race" / "root_agent.yaml"
    mine = (HERO / "race_naive.yaml").read_text()
    draft.write_text(mine)  # the builder assistant's net
    theirs = (app / "root_agent.yaml").read_text() + "# EDITOR CHANGE\n"
    (app / "root_agent.yaml").write_text(theirs)  # and an editor's change
    assert _save(client, "race", _canvas(), tmp=True) == (200, True)
    assert _save(client, "race", _canvas(), tmp=False) == (200, False)
    assert (app / "root_agent.yaml").read_text() == theirs
    assert draft.read_text() == mine
    note = take_note("race")
    assert note is not None and "root_agent.yaml changed in the app" in note


def test_a_net_saved_as_an_llm_agent_leaves_the_graph_and_the_runner(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    from adk_libpetri.web.server import adk_web_server

    client, loader = served
    graph = "/dev/apps/race/build_graph_image?dark_mode=false"
    assert "Race_Start" in client.get(graph).json()[""]["dotSrc"]
    assert client.get("/dev/apps/race/builder?tmp=true").status_code == 200
    (agents / "race" / "tmp" / "race" / "root_agent.yaml").write_text(
        PLAIN.replace("name: plain", "name: race")
    )
    assert _save(client, "race", CANVAS_LLM, tmp=True) == (200, True)
    assert _save(client, "race", CANVAS_LLM, tmp=False) == (200, True)
    assert "LlmAgent" in (agents / "race" / "root_agent.yaml").read_text()
    assert "race" not in loader._agent_cache
    server = adk_web_server(client.app)  # type: ignore[arg-type]
    assert server is not None and "race" in server.runners_to_clean
    assert "Race_Start" not in json.dumps(client.get(graph).json())  # ADK's own drawing


def test_create_new_app_never_writes_into_a_helper_package(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, loader = served
    helper = agents / "helpers"
    helper.mkdir()
    (helper / "__init__.py").write_text("")
    (helper / "agent.py").write_text("def fast(x):\n    return x\n")
    assert "helpers" not in loader.list_agents()
    # The canvas of a new app of that name: nothing to load, no draft made.
    r = client.get("/dev/apps/helpers/builder?tmp=true")
    assert r.status_code == 200 and r.text == ""
    assert not (helper / "tmp").exists()
    assert _save(client, "helpers", PLAIN, tmp=True) == (200, True)
    assert _save(client, "helpers", PLAIN, tmp=False) == (200, False)
    assert sorted(p.name for p in helper.iterdir()) == ["__init__.py", "agent.py"]
    note = take_note("helpers")
    assert note is not None and "Pick another app name" in note


def test_single_agent_mode_guards_the_net_and_shows_the_card(
    agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``adk-libpetri web <one app's folder>``: ADK serves the builder from the parent,
    and so do the guard and the canvas card."""
    app = agents / "race"
    net = (app / "root_agent.yaml").read_text()
    loader = PetriAgentLoader(str(app))
    monkeypatch.chdir(loader.agents_dir)  # as serve() does
    with TestClient(build_app(str(app), loader=loader)) as client:
        card = yaml.safe_load(client.get("/dev/apps/race/builder?tmp=true").text)
        assert card["agent_class"] == "adk_libpetri.net.PetriNet" and "tools" in card
        assert "places" not in card
        for tmp in (True, False):
            assert _save(client, "race", CANVAS_LLM, tmp=tmp) == (200, True)
            assert _save(client, "race", _canvas(), tmp=tmp) == (200, True)
        assert (app / "root_agent.yaml").read_text() == net
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()


def test_a_file_deleted_in_the_app_stays_deleted_after_save(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    app = agents / "race"
    (app / "notes.yaml").write_text("a: 1\n")
    assert client.get("/dev/apps/race/builder?tmp=true").status_code == 200
    draft = app / "tmp" / "race"
    assert (draft / "notes.yaml").is_file()
    (app / "notes.yaml").unlink()  # deleted in an editor after the draft was made
    assert _save(client, "race", _canvas(), tmp=True) == (200, True)
    assert _save(client, "race", _canvas(), tmp=False) == (200, True)
    assert not (app / "notes.yaml").exists()


def test_a_file_deleted_in_the_app_and_changed_in_the_draft_is_a_conflict(
    served: tuple[TestClient, PetriAgentLoader], agents: Path
) -> None:
    client, _ = served
    app = agents / "race"
    (app / "notes.yaml").write_text("a: 1\n")
    assert client.get("/dev/apps/race/builder?tmp=true").status_code == 200
    (app / "tmp" / "race" / "notes.yaml").write_text("a: 2\n")  # the assistant's change
    (app / "notes.yaml").unlink()
    assert _save(client, "race", _canvas(), tmp=True) == (200, True)
    assert _save(client, "race", _canvas(), tmp=False) == (200, False)
    assert not (app / "notes.yaml").exists()
    note = take_note("race")
    assert note is not None and "notes.yaml" in note
