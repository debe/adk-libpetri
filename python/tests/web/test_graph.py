"""``NetGraph``: the net as data, as a Petri drawing, and as ADK's dev UI draws it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from google.adk.agents.config_agent_utils import from_config
from google.adk.apps import App
from google.adk.cli.utils.graph_serialization import serialize_app_info
from google.adk.cli.utils.graph_visualization import plot_workflow_graph
from google.adk.cli.utils.state import create_empty_state

from adk_libpetri.net import BlueprintError, PetriNet
from adk_libpetri.net.graph import NetGraph
from adk_libpetri.net.report import LoadError, load_net
from adk_libpetri.workflow import PetriWorkflow

from .conftest import HERO

LOOP_CONFIG = Path(__file__).parents[1] / "workflow" / "adk_samples" / "loop_config"
PATTERNS = Path(__file__).parents[1] / "demos" / "patterns" / "yaml"
BLUEPRINTS = Path(__file__).parents[1] / "net" / "blueprints"


def arcs(g: NetGraph, kind: str) -> set[tuple[str, str]]:
    return {(a.src, a.dst) for a in g.arcs if a.kind == kind}


def test_the_graph_has_every_place_transition_and_arc_kind() -> None:
    g = load_net(str(HERO / "race.yaml")).graph
    assert isinstance(g, NetGraph)
    assert {p.name for p in g.places} >= {"userIn", "eventOut", "permit", "won", "done"}
    assert [t.name for t in g.transitions] == [
        "Race_Start",
        "Race_RunA",
        "Race_RunB",
        "Race_Commit",
        "Race_Drop",
    ]
    assert ("won", "Race_RunA") in arcs(g, "inhibit")
    assert ("won", "Race_Drop") in arcs(g, "read")
    assert ("done", "Race_Commit") in arcs(g, "in")
    assert ("Race_Commit", "eventOut") in arcs(g, "out")
    actions = {t.name: t.action for t in g.transitions}
    assert actions["Race_RunA"] == "node:fast"
    assert actions["Race_Commit"] == "emit"
    assert {t.name: t.priority for t in g.transitions}["Race_Drop"] == -10
    assert next(p for p in g.places if p.name == "userIn").env


def test_xor_arcs_carry_their_route_labels() -> None:
    g = load_net(
        str(Path(__file__).parents[1] / "net" / "blueprints" / "bp_basic" / "triage.yaml")
    ).graph
    routes = {(a.src, a.dst): a.route for a in g.arcs if a.kind == "out" and a.route}
    assert routes[("Triage_Route", "urgent")] == "urgent"
    assert routes[("Triage_Route", "later")] == "default"
    assert routes[("Triage_Handle", "failed")] == "error"


def _line(dot: str, element_id: str) -> str:
    return next(line for line in dot.splitlines() if f'id="{element_id}"' in line)


def test_the_dot_draws_tokens_inhibitors_and_the_fired_transition() -> None:
    g = load_net(str(HERO / "race_naive.yaml")).graph
    dot = g.to_dot({"eventOut": 2, "won": 2}, fired="Race_Commit")
    assert "eventOut<BR/>" in _line(dot, "place:eventOut")
    assert "●●</FONT>" in _line(dot, "place:eventOut")
    # `won` inhibits three transitions: each names it (in red) instead of an arc.
    for t in ("Race_RunA", "Race_RunB", "Race_Commit"):
        assert '<FONT COLOR="#DC2626">unless won</FONT>' in _line(dot, f"transition:{t}")
        assert "inhibited by won" in _line(dot, f"transition:{t}")
    assert 'arrowhead="odot"' not in dot
    assert "#FCD34D" in _line(dot, "transition:Race_Commit")


def test_a_place_two_transitions_inhibit_is_still_drawn() -> None:
    dot = _plain(load_net(str(HERO / "race.yaml")).graph.to_dot())
    assert '"won" -> "Race_RunA" [arrowhead="odot"' in dot
    assert "unless" not in dot


def _plain(dot: str) -> str:
    """``dot`` without the word joiners that keep ADK's text match off names."""
    return dot.replace("\u2060", "")


def _incoming(dot: str) -> dict[str, list[str]]:
    """Each node's predecessors as ADK's dev UI reads them: from the edge titles.

    Graphviz titles an edge ``tail->head``, with ``:port`` appended to an end
    drawn through a port; the UI splits the title on ``->`` and keeps the rest.
    """
    import re

    found: dict[str, list[str]] = {}
    for m in re.finditer(r'^\s*"([^"]+)" -> "([^"]+)"( \[[^\n]*\])?;$', dot, re.MULTILINE):
        attrs = m.group(3) or ""
        tail = m.group(1) + (":_" if 'tailport="_"' in attrs else "")
        head = m.group(2) + (":_" if 'headport="_"' in attrs else "")
        found.setdefault(head, []).append(tail)
    return found


def test_only_consuming_arcs_are_a_transitions_predecessors() -> None:
    """ADK's dev UI walks back from a lit node through its single predecessors."""
    g = load_net(str(PATTERNS / "yaml_race" / "root_agent.yaml")).graph
    dot = g.to_dot()
    incoming = _incoming(dot)
    for t in g.transitions:
        consumed = sorted(a.src for a in g.arcs if a.dst == t.name and a.kind == "in")
        # A join's control input (the Void permit) attaches through a port.
        data = [p for p in consumed if p != "racePermit"] if len(consumed) > 1 else consumed
        assert sorted(incoming.get(t.name, [])) == data, t.name
    assert incoming["Race_RunBranchA"] == ["triggerA"]  # the walk reaches userIn
    # From the commit too: through the winning branch, not stopped at the join.
    assert incoming["Race_CommitA"] == ["branchADone"]
    assert '"racePermit" -> "Race_CommitA" [headport="_", tooltip="control input"]' in dot
    for line in dot.splitlines():
        if 'tooltip="read"' in line or 'tooltip="reset"' in line or "odot" in line:
            assert 'headport="_"' in line and 'constraint="false"' in line


def _ui_match(dot: str, name: str) -> str | None:
    """The drawn node ADK's dev UI lights for an event path segment ``name``.

    As ``highlightExecutionPathInSvg`` does: titles and label texts, lower-cased
    with whitespace as ``_``; an exact match first, then the first that holds it.
    """
    import re

    entries: list[tuple[str, str]] = []
    for m in re.finditer(r'^\s*"([^"]+)" \[(.*)\];$', dot, re.MULTILINE):
        title, attrs = m.group(1), m.group(2)
        label = re.search(r"label=<(.*?)>(?:, \w+=|$)", attrs)
        text = re.sub(r"<[^>]+>", "", label.group(1)) if label else ""
        entries += [(text, title), (title, title)]
    want = name.lower()

    def key(x: str) -> str:
        return re.sub(r"\s+", "_", x.lower())

    for k, title in entries:
        if key(k) == want:
            return title
    for k, title in entries:
        if want in key(k):
            return title
    return None


def test_the_nets_own_name_lights_nothing_and_the_answering_transition_lights() -> None:
    """Every event of a net carries its name (as author, or as the path segment of
    an event at the net's own level): ADK's dev UI must not light a drawn node
    for it. The answer is emitted under the transition that put it on eventOut,
    whose title the UI must match exactly, even when it holds the net's name."""
    import re

    g = load_net(str(PATTERNS / "yaml_race" / "root_agent.yaml")).graph
    named = replace(g, name="Race")  # a name inside every Race_* transition
    dot = named.to_dot()
    assert _ui_match(dot, "race") == "Race"  # the invisible decoy
    assert 'id="net:Race"' in _line(dot, "net:Race") and "style=invis" in _line(dot, "net:Race")
    assert _ui_match(dot, "Race_CommitA") == "Race_CommitA"
    assert _ui_match(dot, "fast") == "Race_RunBranchA"  # node:fast
    labels = re.findall(r"label=<(.*?)>(?:,|\])", dot)
    text = [re.sub(r"<[^>]+>", "", x).lower() for x in labels]
    assert text and not any("race" in x for x in text)
    assert "R\u2060ace_Start" in dot  # labels broken by a word joiner, drawn the same
    assert '"eventOut" [' in dot  # the out port is titled by its own name
    # No title holds the name: no decoy.
    assert "net:" not in g.to_dot()


def test_a_counterexample_step_names_map_onto_the_transition() -> None:
    """A proof splits a timed action into its start and ``complete:T``, with ``inflight:T``."""
    g = load_net(str(HERO / "race_naive.yaml")).graph
    dot = g.to_dot({"inflight:Race_Commit": 1, "eventOut": 1}, fired="complete:Race_Commit")
    commit = _line(dot, "transition:Race_Commit")
    assert "#FCD34D" in commit
    assert "1 in flight" in commit


def test_node_titles_are_the_exact_names_and_side_arcs_do_not_rank() -> None:
    """ADK's dev UI matches a drawn node by its title (the DOT node id) or label text.

    The net here is ``race``, the last path segment of its own events: in a label
    the UI would match it as a substring, so it is broken by a word joiner. A
    title stays exact (the net's answer is emitted under ``Race_Commit``), and a
    decoy titled ``race`` takes the UI's exact match for the net's own events.
    """
    g = load_net(str(HERO / "race.yaml")).graph
    raw = g.to_dot()
    assert '"Race_RunA" [id="transition:Race_RunA"' in raw
    assert '"race" [id="net:race", label="", shape="point", style=invis' in raw
    assert "<B>R\u2060ace_RunA</B>" in raw
    dot = _plain(raw)
    assert '"Race_RunA" [id="transition:Race_RunA"' in dot
    assert "node:fast" in _line(dot, "transition:Race_RunA")
    assert '"won" -> "Race_RunA" [arrowhead="odot"' in dot
    for line in dot.splitlines():
        if "odot" in line or 'tooltip="read"' in line or 'tooltip="reset"' in line:
            assert 'constraint="false"' in line
    assert '{rank=min; "userIn";}' in dot
    assert '{rank=max; "eventOut";}' in dot


def test_both_themes_use_adks_palette() -> None:
    g = load_net(str(HERO / "race.yaml")).graph
    light, dark = g.to_dot(theme="light"), g.to_dot(theme="dark")
    assert 'bgcolor="#F8FAFC"' in light and 'fontcolor="#0F172A"' in light
    assert 'bgcolor="#0F172A"' in dark and 'fontcolor="#F8FAFC"' in dark
    assert 'fillcolor="#1E293B"' in _line(dark, "place:won")
    assert light.replace("#", "") != dark.replace("#", "")


def test_many_resets_are_one_line_on_the_transition() -> None:
    g = load_net(str(PATTERNS / "yaml_race" / "root_agent.yaml")).graph
    dot = g.to_dot()
    start = _line(dot, "transition:Race_Start")
    # Shortened on the transition, in full in its tooltip.
    for part in ("resets ", "trigger{A,B,C}", "branch{A,B,C}Done", "race{Permit,Won,Discarded}"):
        assert part in start, part
    assert "racePermit" in start
    assert 'tooltip="reset"' not in dot


@pytest.fixture
def composed(monkeypatch: pytest.MonkeyPatch) -> NetGraph:
    monkeypatch.syspath_prepend(str(PATTERNS))
    node = from_config(str(PATTERNS / "composed_agent.yaml"))
    assert isinstance(node, PetriNet)
    assert isinstance(node.graph, NetGraph)
    return node.graph


def test_mounted_transitions_keep_their_own_actions(composed: NetGraph) -> None:
    actions = {t.name: (t.action, t.subnet) for t in composed.transitions}
    # A mounted function node runs as <mount>·<node>; its label says so.
    assert actions["first/Race_RunBranchA"] == ("node:first·fast", "first")
    assert actions["second/Race_RunBranchA"] == ("node:second·fast", "second")
    assert actions["second/Race_CommitA"] == ("emit", "second")
    assert actions["assistant/LlmStep_LlmCall"] == ("subnet:stock:llm_agent", "assistant")
    assert composed.adk_nodes["first/Race_RunBranchA"].name == "fast"
    assert {(s.prefix, s.net, s.agent) for s in composed.subnets} == {
        ("first", "race_agent", None),
        ("second", "race_agent", None),
        ("assistant", "stock:llm_agent", "summarizer"),
    }


def test_a_stock_subnet_is_one_node_wired_to_its_bound_places(composed: NetGraph) -> None:
    dot = composed.to_dot(theme="dark")
    assert "assistant/" not in dot  # no internal place or transition
    box = _line(dot, "subnet:assistant")
    assert box.startswith('  "assistant" [')
    assert "<B>assistant</B>" in box and "llm_agent · summarizer" in box
    for arc in ('"question" -> "assistant"', '"assistant" -> "eventOut"'):
        assert dot.count(arc) == 1, arc
    assert '"assistant" -> "transfer"' in dot
    # Expanded on request.
    full = composed.to_dot(collapse_stock=False)
    assert '"assistant/LlmStep_LlmCall"' in full
    assert 'subgraph "cluster_assistant"' in full


def test_a_collapsed_subnet_carries_its_tokens_and_its_firing(composed: NetGraph) -> None:
    dot = composed.to_dot({"assistant/llmRequest": 2}, fired="assistant/LlmStep_LlmCall")
    box = _line(dot, "subnet:assistant")
    assert "●● inside" in box
    assert "#FCD34D" in box
    assert "inside" not in _line(composed.to_dot(), "subnet:assistant")  # its own seed permit


def test_mounted_blueprints_are_clusters_with_short_labels(composed: NetGraph) -> None:
    dot = composed.to_dot(collapse_mounts=False)
    assert 'subgraph "cluster_first"' in dot and 'label="first · race_agent"' in dot
    run = _line(dot, "transition:first/Race_RunBranchA")
    assert run.lstrip().startswith('"first/Race_RunBranchA" [')  # the title: the full name
    assert "<B>Race_RunBranchA</B>" in run and "node:first·fast" in run  # lit by its run
    assert "label=<triggerA>" in _line(dot, "place:first/triggerA")


def test_a_large_net_collapses_its_mounted_blueprints(composed: NetGraph) -> None:
    dot = composed.to_dot()
    assert len(composed.places) > 15 and "first/" not in dot and "second/" not in dot
    box = _line(dot, "subnet:first")
    assert box.lstrip().startswith('"first" [')
    assert "race_agent · 9 places" in box
    # Its runs, as their event paths name them: ADK lights this mount, not `second`.
    assert "runs first·fast, first·medium, first·slow" in box
    second = _line(dot, "subnet:second")
    assert "second·fast" in second and "first·" not in second
    assert "lights up one only" not in box
    for arc in ('"q1" -> "first"', '"first" -> "a1"'):
        assert dot.count(arc) == 1, arc


def test_a_subnet_is_a_net_of_its_own(composed: NetGraph) -> None:
    first = composed.sub("first")
    assert first.name == "race_agent"
    assert "Race_RunBranchA" in {t.name for t in first.transitions}
    ports = {p.name: p.port for p in first.places if p.port}
    assert ports == {"q1": "in", "a1": "out"}  # the parent places it binds
    assert first.adk_nodes["Race_RunBranchA"].name == "fast"
    assert composed.sub("assistant").name == "summarizer"
    assert "LlmStep_LlmCall" in {t.name for t in composed.sub("assistant").transitions}
    with pytest.raises(KeyError):
        composed.sub("nope")


def test_adks_structure_view_can_open_each_subnet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node with a graph of its own is expandable in ADK's "Agent Structure" view."""
    monkeypatch.syspath_prepend(str(PATTERNS))
    node = from_config(str(PATTERNS / "composed_agent.yaml"))
    root = next(n for n in node.graph.nodes if getattr(n, "name", None) == "first")
    info = serialize_app_info(App(name="c", root_agent=node))
    nodes = {n["name"]: n for n in info["root_agent"]["graph"]["nodes"]}
    assert set(nodes) >= {"first", "second", "assistant"}
    assert nodes["first"]["type"] == "workflow"
    inner = {n["name"] for n in nodes["first"]["graph"]["nodes"]}
    assert {"Race_RunBranchA", "triggerA", "q1", "fast"} <= inner
    assert root.graph.name == "race_agent"


def test_nested_mounts_nest_their_clusters(monkeypatch: pytest.MonkeyPatch) -> None:
    for d in (BLUEPRINTS, BLUEPRINTS / "bp_nested", BLUEPRINTS / "bp_nested" / "bp_mid"):
        monkeypatch.syspath_prepend(str(d))
    node = from_config(str(BLUEPRINTS / "bp_nested" / "top.yaml"))
    assert isinstance(node, PetriNet)
    dot = node.graph.to_dot()
    middle = dot.index('subgraph "cluster_middle" {')
    inner = dot.index('subgraph "cluster_middle/inner" {')
    assert middle < inner < dot.index("  }\n", inner) < dot.rindex("  }\n")
    assert 'label="inner · leaf"' in dot
    assert "node:middle·inner·wrap_leaf" in _line(dot, "transition:middle/inner/Leaf_Wrap")


def test_adks_dev_ui_draws_the_net_with_the_nodes_it_runs() -> None:
    node = load_net(str(HERO / "race.yaml"))
    info = serialize_app_info(App(name="race", root_agent=node))
    drawn = {n["name"]: n["type"] for n in info["root_agent"]["graph"]["nodes"]}
    assert drawn["Race_Commit"] == "node"
    assert drawn["won"] == "node"
    # The ADK nodes beside their transitions: the dev UI lights them up by event path.
    assert drawn["fast"] == "function"
    dot = plot_workflow_graph({"root_agent": info["root_agent"]}, format="dot")
    assert isinstance(dot, str) and "Race_Commit" in dot
    assert "NO DEFAULT" not in dot  # no routed edges: ADK would draw those as a router


def test_adk_walks_the_graph_without_tripping() -> None:
    node = load_net(str(HERO / "race.yaml"))
    assert create_empty_state(node) == {}


def test_a_file_cannot_set_the_graph(tmp_path: Path) -> None:
    f = tmp_path / "app" / "root_agent.yaml"
    f.parent.mkdir()
    f.write_text(
        "agent_class: adk_libpetri.net.PetriNet\nname: g\ngraph: {}\n"
        "transitions:\n  G_Emit: {in: [userIn], out: eventOut, action: emit}\n"
    )
    with pytest.raises(LoadError) as err:
        load_net(str(f))
    assert err.value.key_path == "graph"
    assert isinstance(err.value.__cause__, Exception)
    assert BlueprintError.__name__ in repr(err.value.__cause__) or "derived" in str(err.value)


def test_a_compiled_workflow_has_a_graph_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(LOOP_CONFIG.parent))
    wf = PetriWorkflow.from_config(str(LOOP_CONFIG / "petri_root_agent.yaml"))
    g = wf.graph
    assert isinstance(g, NetGraph)
    assert "Wf_generate_headline_Run" in {t.name for t in g.transitions}
    assert g.adk_nodes["Wf_generate_headline_Run"].name == "generate_headline"
    info = serialize_app_info(App(name="wf", root_agent=wf))
    assert "generate_headline" in {n["name"] for n in info["root_agent"]["graph"]["nodes"]}


def test_xor_branches_to_one_place_are_one_arc() -> None:
    """``Opt_Validate``: pass and fail both keep ``cheapPending``: one arc, no label."""
    g = load_net(str(PATTERNS / "yaml_optimistic" / "root_agent.yaml")).graph
    dot = _plain(g.to_dot())
    arcs = [line for line in dot.splitlines() if line.startswith('  "Opt_Validate" -> ')]
    assert arcs == [
        '  "Opt_Validate" -> "validationPassed" [label=" pass "];',
        '  "Opt_Validate" -> "cheapPending";',
        '  "Opt_Validate" -> "validationFailed" [label=" fail "];',
    ]


def test_competing_transitions_line_up_by_priority() -> None:
    g = load_net(str(PATTERNS / "yaml_race" / "root_agent.yaml")).graph
    row = (
        '"Race_CommitA" -> "Race_CommitB" -> "Race_CommitC" -> "Race_DiscardA" -> '
        '"Race_DiscardB" -> "Race_DiscardC" [style=invis];'
    )
    assert f"  {{rank=same; {row}}}" in g.to_dot()


def test_a_stock_subnets_drawing_is_compact(composed: NetGraph) -> None:
    from adk_libpetri.web.graph_view import subnet_dot

    dot = subnet_dot(composed, "assistant", "light")
    call = _line(dot, "transition:LlmStep_LlmCall")
    assert call.lstrip().startswith('"LlmStep_LlmCall" [')  # ids keep the full name
    assert "<B>LlmCall</B>" in call and "✦ summarizer" in call and "#42A5F5" in call
    assert 'subgraph "cluster_part_LlmStep"' in dot and 'label="LlmStep"' in dot
    assert "<B>BuildPrompt</B>" in _line(dot, "transition:LlmAgent_BuildPrompt")
    fallback = _line(dot, "transition:LlmAgent_ReAskExhaustedFallback")
    assert "unless reaskBudget" in fallback  # named, not a red arc across the drawing
    assert 'arrowhead="odot"' not in dot and 'tooltip="reset"' not in dot
    assert "nodesep=0.25, ranksep=0.2" in dot
    assert "calls summarizer&#x27;s model" in dot or "calls summarizer's model" in dot


def test_the_drawing_carries_a_key_of_its_shapes(composed: NetGraph) -> None:
    dot = composed.to_dot(key=True)
    key = dot.splitlines()[-2]
    assert key.startswith("  label=<") and 'labelloc="b"' in key
    for item in ("place", "transition", "runs an ADK node", "stock subnet", "mounted net"):
        assert item in key, item
    assert "label=<" not in composed.to_dot().splitlines()[-2]


def test_brace_shortens_place_lists() -> None:
    from adk_libpetri.net.graph import _brace

    assert _brace(["triggerA", "triggerB", "won"]) == "trigger{A,B}, won"
    assert _brace(["raceWon", "raceLost", "x"]) == "race{Won,Lost}, x"


def test_adks_serializer_reads_a_net_without_a_warning(
    composed: NetGraph, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``nodes`` holds edge items (tuples), which ADK's dev UI serializer cannot read."""
    node = from_config(str(PATTERNS / "composed_agent.yaml"))
    with caplog.at_level("WARNING"):
        info = serialize_app_info(App(name="c", root_agent=node))
    assert "Error serializing" not in caplog.text
    assert "nodes" not in info["root_agent"] and "graph" in info["root_agent"]


def _ui_lookup(dot: str, name: str) -> str | None:
    """The drawn node ADK's dev UI lights for an event path segment ``name``
    (``highlightExecutionPathInSvg``'s ``p``: a node whose text or title is the name,
    else the first whose text holds it; text is its label's text run together)."""
    import html as html_lib
    import re

    nodes: list[tuple[str, str]] = []
    for m in re.finditer(r'^\s*"((?:[^"\\]|\\.)*)" \[id="[^"]*", label=<(.*?)>, ', dot, re.M):
        text = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
        nodes.append((m.group(1), re.sub(r"\s+", "_", text.lower())))
    want = name.lower()
    entries = [(key, title) for title, text in nodes for key in (text, title.lower())]
    for key, title in entries:
        if key == want:
            return title.replace("\u2060", "")
    found = next((title for key, title in entries if want in key), None)
    return found.replace("\u2060", "") if found else None


def test_each_run_lights_the_transition_that_runs_it(composed: NetGraph) -> None:
    """Not a place whose name holds it (``slowTrigger`` for ``slow``), nor another mount."""
    opt = load_net(str(PATTERNS / "yaml_optimistic" / "root_agent.yaml")).graph.to_dot()
    assert _ui_lookup(opt, "slow") == "Opt_RunSlow"
    assert _ui_lookup(opt, "cheap") == "Opt_RunCheap"
    assert _ui_lookup(opt, "validate") == "Opt_Validate"
    dot = composed.to_dot()
    assert _ui_lookup(dot, "first·fast") == "first"
    assert _ui_lookup(dot, "second·fast") == "second"
    assert _ui_lookup(dot, "brief") == "Composed_Brief"
    assert _ui_lookup(dot, composed.name) is None  # every event's author: lights nothing
    expanded = composed.to_dot(collapse_mounts=False)
    assert _ui_lookup(expanded, "second·medium") == "second/Race_RunBranchB"


def test_a_place_nothing_produces_is_drawn_dim_and_is_not_a_predecessor() -> None:
    """``turnAbort`` of the composed pattern: bound to the stock subnet, never produced."""
    g = load_net(str(PATTERNS / "composed_agent.yaml")).graph
    dot = g.to_dot(key=True)
    abort = _line(dot, "place:turnAbort")
    assert "dotted" in abort and "nothing in this net produces it" in abort
    assert _incoming(dot)["assistant"] == ["question"]
    assert "dotted: nothing produces it" in dot


def test_the_key_wraps_and_names_tokens() -> None:
    g = load_net(str(PATTERNS / "composed_agent.yaml")).graph
    dot = g.to_dot({"userIn": 1}, key=True)
    key = next(line for line in dot.splitlines() if line.strip().startswith("label=<"))
    assert "token" in key
    rows = key.split('<BR ALIGN="LEFT"/>')
    assert len(rows) >= 3 and all(row.count("  ·  ") < 4 for row in rows)
