"""A violated claim's counterexample as compact steps and as a picture in ADK's dev UI.

The claim here is the README hero's naive race, written out by hand (the
verifier's own trace, ``complete:``/``inflight:`` split included), so no Z3
is needed. Tests that run Graphviz skip without a working ``dot`` binary.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adk_libpetri.net.counterexample import (
    counterexample_svg,
    dot_binary,
    dot_svg,
    step_lines,
    steps,
    violation,
)
from adk_libpetri.net.graph import NetGraph
from adk_libpetri.net.report import ClaimResult, VerifyReport, load_net, verify_file
from adk_libpetri.web import PetriAgentLoader, build_app, proof_view
from adk_libpetri.web.loader import net_nodes
from support.smt_proofs import requires_z3

FIRES = (
    "Race_Start",
    "Race_RunA",
    "Race_Commit",
    "Race_RunB",
    "Race_Commit",
    "complete:Race_Commit",
    "complete:Race_Commit",
)
MARKINGS = (
    {"userIn": 1, "env:optional[0]": 1},
    {"triggerA": 1, "triggerB": 1, "env:optional[0]": 1},
    {"done": 1, "triggerB": 1},
    {"inflight:Race_Commit": 1, "triggerB": 1},
    {"done": 1, "inflight:Race_Commit": 1},
    {"inflight:Race_Commit": 2},
    {"eventOut": 1, "inflight:Race_Commit": 1, "won": 1},
    {"eventOut": 2, "won": 2},
)


def naive_claim(**changes: object) -> ClaimResult:
    fields: dict[str, object] = dict(
        net="race_naive",
        label="one answer per turn",
        kind="place_bound",
        verdict="violated",
        fires=FIRES,
        markings=MARKINGS,
        places=("eventOut",),
        bound=1,
    )
    fields.update(changes)
    return ClaimResult(**fields)  # type: ignore[arg-type]


@pytest.fixture
def graph(agents: Path) -> NetGraph:
    return load_net(str(agents / "race_naive" / "root_agent.yaml")).graph


@pytest.fixture
def keep_base() -> Iterator[None]:
    """``build_app`` records where the pictures are served: undo it after the test."""
    saved = list(proof_view._BASE)
    proof_view.forget_counterexamples()
    yield
    proof_view._BASE[:] = saved
    proof_view.forget_counterexamples()


def _xml(svg: str) -> ET.Element:
    return ET.fromstring(svg)  # the picture is one well-formed SVG document


def _has_dot() -> bool:
    return dot_binary() is not None and dot_svg("digraph { a -> b }") is not None


requires_dot = pytest.mark.skipif(not _has_dot(), reason="needs a working Graphviz dot binary")


# ----------------------------------------------------------------------------
#  Steps
# ----------------------------------------------------------------------------


def test_steps_read_the_verifiers_split_actions_and_the_offending_tokens() -> None:
    ss = steps(naive_claim())
    assert [(s.transition, s.verb) for s in ss[1:]] == [
        ("Race_Start", "fires"),
        ("Race_RunA", "fires"),
        ("Race_Commit", "starts"),
        ("Race_RunB", "fires"),
        ("Race_Commit", "starts"),
        ("Race_Commit", "completes"),
        ("Race_Commit", "completes"),
    ]
    assert ss[1].new == {"triggerA", "triggerB"} and ss[1].fewer == {"userIn": 0}
    assert ss[-1].bad == {"eventOut"} and not ss[-2].bad
    assert all(not k.startswith("env:") for s in ss for k in s.marking)


@requires_z3
def test_the_verifiers_own_counterexample_reads_the_same(agents: Path) -> None:
    """The steps above are the verifier's: pins its ``complete:``/``inflight:`` naming."""
    report = verify_file(str(agents / "race_naive" / "root_agent.yaml"))
    [claim] = [c for c in report.claims if c.verdict == "violated"]
    ss = steps(claim)
    assert [s.transition for s in ss[1:]].count("Race_Commit") >= 2
    assert {s.verb for s in ss[1:]} >= {"fires", "starts", "completes"}
    assert ss[-1].bad == {"eventOut"}


def test_step_lines_are_compact_changes_and_end_with_what_breaks() -> None:
    lines = step_lines(naive_claim())
    assert lines[0] == "0. initial marking: userIn"
    assert lines[3] == "3. Race_Commit starts: -done, +Race_Commit in flight"
    assert lines[5] == "5. Race_Commit starts: -done, +Race_Commit in flight (now 2)"
    assert lines[-1] == (
        "7. Race_Commit completes: +eventOut (now 2), +won (now 2), -Race_Commit in flight; "
        "marking now: eventOut=2, won=2  <- eventOut=2 exceeds the bound 1"
    )
    assert not any("env:" in line for line in lines)


def test_each_claim_kind_says_what_breaks() -> None:
    dead = naive_claim(kind="deadlock_free", places=(), bound=0)
    assert violation(dead, steps(dead)[-1]) == "deadlock: no transition can fire"
    both = naive_claim(kind="unreachable", places=("eventOut", "won"))
    assert violation(both, steps(both)[-1]) == "reached: eventOut, won"
    assert steps(both)[-1].bad == {"eventOut", "won"}
    assert not steps(both)[-2].bad  # unreachable breaks at the last step only
    mutex = naive_claim(kind="mutual_exclusion", places=("eventOut", "won"))
    assert violation(mutex, steps(mutex)[-1]) == "marked together: eventOut, won"
    assert step_lines(naive_claim(fires=(), markings=())) == []


# ----------------------------------------------------------------------------
#  The picture
# ----------------------------------------------------------------------------


def test_without_dot_the_picture_is_the_steps_and_says_so(graph: NetGraph) -> None:
    svg = counterexample_svg(graph, naive_claim(), render=lambda src: None)
    _xml(svg)
    assert "No Graphviz dot binary was found" in svg
    assert "one answer per turn" in svg and "eventOut=2 exceeds the bound 1" in svg
    assert "step 7 of 7: Race_Commit completes" in svg
    # Both of ADK's palettes, the dark one under the media query an <img> follows.
    assert "#F8FAFC" in svg and "@media (prefers-color-scheme: dark)" in svg
    assert "#0F172A" in svg.split("@media", 1)[1]


def test_the_net_is_drawn_at_the_step_in_both_themes(graph: NetGraph) -> None:
    seen: list[str] = []

    def render(src: str) -> str:
        seen.append(src)
        return (
            '<?xml version="1.0"?>\n<!DOCTYPE svg>\n<!-- Generated by graphviz -->\n'
            '<svg width="200pt" height="100pt" viewBox="0 0 200 100" '
            'xmlns="http://www.w3.org/2000/svg"><g id="graph0"/></svg>\n'
        )

    svg = counterexample_svg(graph, naive_claim(), render=render)
    root = _xml(svg)
    assert len(seen) == 2  # light and dark
    light, dark = seen
    assert 'bgcolor="#F8FAFC"' in light and 'bgcolor="#0F172A"' in dark
    # The offending place in red, the transition that completed highlighted.
    assert re.search(r'"eventOut" \[[^\]]*fillcolor="#FEE2E2"', light)
    assert re.search(r'"Race_Commit" \[[^\]]*fillcolor="#FCD34D"', light)
    groups = [g.get("class") for g in root.iter("{http://www.w3.org/2000/svg}g")]
    assert "theme-light" in groups and "theme-dark" in groups
    assert "<?xml" not in svg and "DOCTYPE" not in svg and "graphviz" not in svg

    counterexample_svg(graph, naive_claim(), render=render, step=2)
    assert re.search(r'"Race_RunA" \[[^\]]*fillcolor="#FCD34D"', seen[-2])
    assert 'fillcolor="#FEE2E2"' not in seen[-2]


def test_a_wide_net_is_scaled_to_fit_unless_full(graph: NetGraph) -> None:
    def wide(src: str) -> str:
        return '<svg width="2000pt" height="400pt" viewBox="0 0 2000 400"></svg>'

    fit = _xml(counterexample_svg(graph, naive_claim(), render=wide))
    assert fit.get("width") == "760"
    full = _xml(counterexample_svg(graph, naive_claim(), render=wide, max_width=None))
    assert full.get("width") == "2032"


@requires_dot
def test_graphviz_draws_the_net_into_the_picture(graph: NetGraph) -> None:
    svg = counterexample_svg(graph, naive_claim())
    _xml(svg)
    assert "No Graphviz" not in svg
    assert svg.count("<title>eventOut</title>") == 2  # one drawing per theme


# ----------------------------------------------------------------------------
#  The route, and what the builder hands the model
# ----------------------------------------------------------------------------


@pytest.fixture
def client(agents: Path, keep_base: None) -> Iterator[TestClient]:
    loader = PetriAgentLoader(str(agents))
    with TestClient(build_app(str(agents), loader=loader)) as c:
        yield c
    for root in list(loader._agent_cache.values()):
        for node in net_nodes(root):
            node.registry.close_all()


def test_the_server_serves_a_kept_counterexample(client: TestClient, graph: NetGraph) -> None:
    line = proof_view.picture(graph, naive_claim())
    assert line is not None
    m = re.fullmatch(
        r'<a href="([^"]+)\?full=1" target="_blank"><img src="([^"]+)" '
        r'alt="Counterexample: one answer per turn" width="100%"></a>',
        line,
    )
    assert m is not None and m.group(1) == m.group(2)
    url = m.group(2)
    assert url.startswith("/dev/petri/counterexamples/") and url.endswith(".svg")
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["cache-control"] == "no-cache"
    assert "eventOut=2 exceeds the bound 1" in r.text
    # In the panel, the steps only: a whole net shrinks past reading there.
    assert "<svg" not in r.text.split("<svg", 1)[1] and "Open the picture full size" in r.text
    full = client.get(url, params={"full": 1, "step": 2})
    assert full.status_code == 200 and "step 2 of 7: Race_RunA fires" in full.text
    assert "Open the picture full size" not in full.text
    # Same counterexample on the same net, same picture.
    assert proof_view.picture(graph, naive_claim()) == line


def test_an_unknown_counterexample_answers_with_a_picture_saying_so(client: TestClient) -> None:
    r = client.get("/dev/petri/counterexamples/0123456789abcdef.svg")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "verify again" in r.text


def test_no_picture_without_a_server_to_show_it(graph: NetGraph, keep_base: None) -> None:
    proof_view._BASE.clear()  # stock `adk web`: nothing serves the route
    assert proof_view.picture(graph, naive_claim()) is None


def test_pictures_follow_the_servers_url_prefix(agents: Path, keep_base: None) -> None:
    build_app(str(agents), loader=PetriAgentLoader(str(agents)), url_prefix="/adk")
    assert proof_view.picture_url("abc").startswith("/adk/dev/petri/counterexamples/abc.svg")


def test_a_report_carries_its_graphs_but_not_into_json(graph: NetGraph) -> None:
    report = VerifyReport("x.yaml", ("race_naive",), (naive_claim(),), graphs={"race_naive": graph})
    d = report.to_dict()
    assert "graphs" not in d
    assert list(d["claims"][0]["places"]) == ["eventOut"] and d["claims"][0]["bound"] == 1
