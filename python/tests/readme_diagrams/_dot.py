"""Post-processing for the README's Python-net diagrams.

libpetri-py exports a net as DOT text only (no graph model, unlike Java's
``PetriNetGraphMapper``), so this module parses that text, rewrites it the way
``java/src/test/java/org/libpetri/adk/docs/ReadmeDiagramsTest.java`` rewrites
its graph, and renders it again:

* **Names inside places.** A place is an ellipse that carries its name instead
  of a fixed circle with the name as an ``xlabel``. An end place keeps its
  double outline (``peripheries=2``). The ``read`` label goes (the grey dashed
  style says it), and so does an XOR branch label that repeats its target
  place's name.
* **Seed suffix.** A seeded place's label gains ``" ●"`` or ``" ●×K"``.
* **Cut place.** In a view, a place that a transition outside the view
  consumes, reads or produces is drawn dotted grey ("continues outside this
  view"), so it does not pass for a start or end place.
* **No-op resets.** A reset arc to a place that no transition of the whole
  net produces (``wf/parked`` in a workflow without ``RequestInput`` nodes)
  clears a place that is always empty, so it is dropped, and the place with it
  when that was its only arc.
* **No clusters.** libpetri-py groups places into nested ``cluster_*``
  subgraphs by their ``/``-separated names. The README views are drawn flat,
  as the Java views are (``ClusterSource.NONE``), so the subgraphs and their
  invisible ``ltail``/``lhead`` layout edges go.

Every diagram also gets ``bgcolor=white``, ``pad=0.15``, ``nodesep=0.3`` and
12 pt edge labels.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import libpetri as lp

from adk_libpetri._spec import NetSpec, TransitionSpec, _out_places

CUT_FILL = "#f6f8fa"
CUT_STROKE = "#8c959f"
PLACE_SIZE = "0.3"
EDGE_FONT_SIZE = "12"

_NODE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*) \[(.*)\];$")
_EDGE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*) -> ([A-Za-z_][A-Za-z0-9_]*) \[(.*)\];$")
_ATTR = re.compile(r'([A-Za-z_]+)=("(?:[^"\\]|\\.)*"|[^,\s]+)')
_SANITIZE = re.compile(r"[^A-Za-z0-9_]")

Attrs = dict[str, str]


def place_id(name: str) -> str:
    """The node id libpetri-py gives a place (``wf/a->b`` becomes ``p_wf_a__b``)."""
    return "p_" + _SANITIZE.sub("_", name)


def _attrs(text: str) -> Attrs:
    return {k: v for k, v in _ATTR.findall(text)}


def _unquote(v: str) -> str:
    return v[1:-1].replace('\\"', '"') if v.startswith('"') else v


def _quote(v: str) -> str:
    return '"' + v.replace('"', '\\"') + '"'


def _fmt(attrs: Mapping[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in attrs.items())


@dataclass(frozen=True)
class Diagram:
    """One README diagram, a view of a compiled workflow's net.

    ``view`` lists the transitions to draw (all must exist in ``spec``);
    ``env`` names environment places; ``seeds`` maps a place name to its seed
    suffix (``"●"``, ``"●×K"``).
    """

    name: str
    source: str
    spec: NetSpec
    view: tuple[str, ...]
    env: frozenset[str]
    seeds: Mapping[str, str]


def _touched(t: TransitionSpec) -> list[str]:
    ps = [i.place.name for i in t.inputs] + [p.name for p in t.reads]
    if t.output is not None:
        ps += [p.name for p in _out_places(t.output)]
    return ps


def render(d: Diagram) -> str:
    """The post-processed DOT for ``d``, with the generated-file header."""
    names = d.spec.transition_names
    missing = [n for n in d.view if n not in names]
    assert not missing, f"{d.name}: view names not in {d.spec.name}: {missing}; it has {names}"
    view = NetSpec(d.name, tuple(d.spec.transition(n) for n in d.view))
    drawn = {p.name for p in view.places}
    for p in [*d.env, *d.seeds]:
        assert p in drawn, f"{d.name}: place {p} is not drawn; drawn: {sorted(drawn)}"

    cut: set[str] = set()
    for t in d.spec.transitions:
        if t.name in d.view:
            continue
        for p in _touched(t):
            if p in drawn and p not in d.env and p not in d.seeds:
                cut.add(place_id(p))
    seeds = {place_id(p): s for p, s in d.seeds.items()}
    produced = {
        p.name for t in d.spec.transitions if t.output is not None for p in _out_places(t.output)
    }
    noop = {place_id(p) for n in d.view for p in (r.name for r in d.spec.transition(n).resets)}
    noop -= {place_id(p) for p in produced}

    raw = lp.dot_export(
        view.build(),
        lp.DotConfig(
            show_types=False,
            show_intervals=False,
            environment_places=sorted(d.env),
        ),
    )
    return _rewrite(d, raw, cut, seeds, noop)


def _rewrite(d: Diagram, raw: str, cut: set[str], seeds: Mapping[str, str], noop: set[str]) -> str:
    graph_attrs: list[str] = []
    defaults: list[str] = []
    nodes: list[tuple[str, Attrs]] = []
    edges: list[tuple[str, str, Attrs]] = []
    header = ""
    for line in raw.splitlines():
        s = line.strip()
        if not s or s == "}" or s.startswith("subgraph "):
            continue
        if s.startswith("digraph "):
            header = s
        elif m := _EDGE.match(line):
            a = _attrs(m.group(3))
            if "ltail" in a or "lhead" in a:
                continue  # cluster layout edge
            if m.group(2) in noop and _unquote(a.get("label", '""')) == "reset":
                continue  # reset of a place nothing produces
            edges.append((m.group(1), m.group(2), a))
        elif s.startswith(("node [", "edge [")):
            defaults.append(s)
        elif m := _NODE.match(line):
            nodes.append((m.group(1), _attrs(m.group(2))))
        elif line.startswith("    ") and not line.startswith("        "):
            graph_attrs.append(s)  # top-level graph attribute; cluster attrs are deeper
        # deeper non-node lines are cluster attributes (label, style, ...): dropped

    out_edges = [(f, t, _edge(f, t, a)) for f, t, a in edges]
    order: dict[str, int] = {}
    for f, t, _ in out_edges:
        order.setdefault(f, len(order))
        order.setdefault(t, len(order))
    places = sorted(
        (n for n in nodes if n[0].startswith("p_") and n[0] in order),
        key=lambda n: (order[n[0]], n[0]),
    )
    others = [n for n in nodes if not n[0].startswith("p_")]

    for pid in seeds:
        assert any(n[0] == pid for n in places), f"{d.name}: seed place {pid} not drawn"

    lines = [
        "// GENERATED by python/tests/readme_diagrams/test_readme_diagrams.py",
        f"// from {d.source}. Do not edit: rerun the test with",
        "// READMEDIAGRAMS_WRITE=1, then `npm run build` in docs/diagrams.",
        header,
    ]
    for a in graph_attrs:
        if a.startswith("nodesep="):
            a = "nodesep=0.3;"
        lines.append(f"    {a}")
        if a == 'compound="true";':
            lines.append('    bgcolor="white";')
            lines.append('    pad="0.15";')
    for s in defaults:
        if s.startswith("edge ["):
            s = re.sub(r"fontsize=\d+", f"fontsize={EDGE_FONT_SIZE}", s)
        lines.append(f"    {s}")
    lines.append("")
    for pid, a in places:
        lines.append(f"    {pid} [{_fmt(_place(pid, a, cut, seeds))}];")
    for nid, a in others:
        lines.append(f"    {nid} [{_fmt(a)}];")
    lines.append("")
    for f, t, a in out_edges:
        lines.append(f"    {f} -> {t} [{_fmt(a)}];")
    lines.append("}")
    dot = "\n".join(lines) + "\n"
    for pid, s in seeds.items():
        assert f' {s}"' in dot, f"{d.name}: seed suffix missing on {pid}"
    return dot


def _place(pid: str, a: Attrs, cut: set[str], seeds: Mapping[str, str]) -> Attrs:
    name = _unquote(a.get("xlabel", '""'))
    assert name, f"place {pid} has no xlabel"
    if pid in seeds:
        name = f"{name} {seeds[pid]}"
    out: Attrs = {"label": _quote(name), "shape": '"ellipse"'}
    if pid in cut:
        out |= {
            "style": '"filled,dotted"',
            "fillcolor": _quote(CUT_FILL),
            "color": _quote(CUT_STROKE),
            "penwidth": "2",
        }
    else:
        out |= {k: a[k] for k in ("style", "fillcolor", "color", "penwidth") if k in a}
    out |= {
        "height": PLACE_SIZE,
        "width": PLACE_SIZE,
        "fixedsize": '"false"',
        "margin": '"0.08,0.03"',
    }
    if pid not in cut and _unquote(a.get("shape", "")) == "doublecircle":
        out["peripheries"] = "2"
    return out


def _edge(f: str, t: str, a: Attrs) -> Attrs:
    label = _unquote(a["label"]) if "label" in a else None
    if label is None:
        return a
    redundant = label == "read" or (f.startswith("j_") and t == place_id(label))
    return {k: v for k, v in a.items() if k != "label"} if redundant else a
