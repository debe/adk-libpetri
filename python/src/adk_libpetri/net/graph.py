"""A net's structure as plain data: for ADK's graph panel, DOT and ``/petri``.

:func:`net_graph` reads a :class:`~adk_libpetri.net.blueprint.Blueprint` (or a
bare :class:`~adk_libpetri._spec.NetSpec`) into a :class:`NetGraph`: places,
transitions and every arc with its kind. Three views come out of it:

* :meth:`NetGraph.to_dict`, the JSON the ``/petri`` routes return;
* :meth:`NetGraph.to_dot`, a Petri drawing (ellipse places with their token
  counts, box transitions, inhibitor and read arcs drawn as such), optionally
  with a marking and the transition that just fired highlighted;
* :attr:`NetGraph.nodes` and :attr:`NetGraph.edges`, the duck-typed shape
  ADK's dev UI draws a node's ``graph`` field with
  (``google.adk.cli.utils.graph_serialization``). That view has one node
  shape and no arc kinds, so it carries the token flow only (input, output
  and read arcs) plus, beside each ``node:`` transition, the ADK node it
  runs: the dev UI lights a node up when one of its events is selected.
  Each top-level subnet is also a node carrying a graph of its own
  (:meth:`NetGraph.sub`), which the dev UI's "Agent Structure" view opens.
"""

from __future__ import annotations

import html
import re
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, ClassVar, Literal

from .._experimental import experimental
from .._spec import And, Forward, NetSpec, Out, OutPlace, Timeout, Timing, Xor

ArcKind = Literal["in", "out", "read", "inhibit", "reset"]
Theme = Literal["light", "dark"]


@dataclass(frozen=True)
class _Palette:
    bg: str
    text: str
    muted: str
    meta: str
    line: str
    edge: str
    edge_text: str
    place: str
    marked: str
    port: str
    transition: str
    node_line: str
    running: str
    fired: str
    fired_line: str
    stock: str
    stock_line: str
    cluster: str
    cluster_line: str
    cluster_text: str
    inhibit: str
    reset: str
    alarm: str


# ADK's dev UI graph colours (google.adk.cli.utils.graph_visualization), plus
# the few a Petri net needs beyond them.
_PALETTES: dict[str, _Palette] = {
    "light": _Palette(
        bg="#F8FAFC",
        text="#0F172A",
        muted="#64748B",
        meta="#334155",
        line="#94A3B8",
        edge="#64748B",
        edge_text="#475569",
        place="#FFFFFF",
        marked="#DBEAFE",
        port="#475569",
        transition="#F1F5F9",
        node_line="#42A5F5",
        running="#FDE68A",
        fired="#FCD34D",
        fired_line="#D97706",
        stock="#F3E8FF",
        stock_line="#9333EA",
        cluster="#FFFFFF",
        cluster_line="#CBD5E1",
        cluster_text="#475569",
        inhibit="#DC2626",
        reset="#CBD5E1",
        alarm="#FEE2E2",
    ),
    "dark": _Palette(
        bg="#0F172A",
        text="#F8FAFC",
        muted="#94A3B8",
        meta="#CBD5E1",
        line="#475569",
        edge="#94A3B8",
        edge_text="#CBD5E1",
        place="#1E293B",
        marked="#1E3A8A",
        port="#94A3B8",
        transition="#334155",
        node_line="#42A5F5",
        running="#78350F",
        fired="#B45309",
        fired_line="#F59E0B",
        stock="#3B0764",
        stock_line="#A855F7",
        cluster="#131C31",
        cluster_line="#334155",
        cluster_text="#CBD5E1",
        inhibit="#F87171",
        reset="#475569",
        alarm="#7F1D1D",
    ),
}


@dataclass(frozen=True)
class PlaceInfo:
    name: str
    type: str
    seed: int = 0
    env: bool = False
    port: str | None = None
    """The port this place is (``in``/``out``/``inout``), if any."""


@dataclass(frozen=True)
class TransitionInfo:
    name: str
    action: str = "move"
    """``move``, ``emit``, ``node:<name>``; a mounted blueprint's transition has its
    own (``node:`` names the child's node), a stock subnet's is
    ``subnet:stock:<kind>``."""
    priority: int = 0
    timing: str | None = None
    subnet: str | None = None
    """The mount prefix of a transition that came from a subnet."""


@dataclass(frozen=True)
class Arc:
    src: str
    dst: str
    kind: ArcKind
    count: str = "1"
    """``1``, ``n``, ``all`` or ``>=n`` (input arcs)."""
    route: str | None = None
    """The xor branch label an output arc belongs to."""
    timeout_ms: int | None = None
    """Set on the output arc of a ``timeout`` branch."""


@dataclass(frozen=True)
class SubnetInfo:
    prefix: str
    """The mount prefix (``first``, or ``first/inner`` for a nested mount)."""
    net: str
    """The child blueprint's name, or ``stock:<kind>``."""
    agent: str | None = None
    """The ``from:`` LlmAgent of a stock subnet."""

    @property
    def stock(self) -> bool:
        return self.net.startswith("stock:")


@dataclass(frozen=True, eq=False)
class _Stub:
    """A graph node ADK's serializer can read: a name, nothing to rerun."""

    name: str
    rerun_on_resume: bool = False


class _SubnetStub:
    """A subnet in ADK's duck-typed graph: a name and its own graph.

    ADK serializes a graph node with ``model_fields`` field by field (its
    ``graph`` included), and its dev UI makes a node with a ``graph``
    expandable in the "Agent Structure" view.
    """

    model_fields: ClassVar[dict[str, Any]] = {"name": None, "graph": None}
    rerun_on_resume = False

    def __init__(self, name: str, graph: NetGraph) -> None:
        self.name = name
        self.graph = graph


@dataclass(frozen=True)
class _Edge:
    from_node: Any
    to_node: Any
    route: Any = None


@experimental
@dataclass(frozen=True)
class NetGraph:
    name: str
    places: tuple[PlaceInfo, ...]
    transitions: tuple[TransitionInfo, ...]
    arcs: tuple[Arc, ...]
    adk_nodes: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    """``node:`` transition -> the ADK node it runs (for ADK's view only)."""
    subnets: tuple[SubnetInfo, ...] = ()
    """The mounted subnets, nested ones under their full prefix."""

    # -- ADK's duck-typed graph -------------------------------------------------

    @property
    def nodes(self) -> list[Any]:
        stubs = _stubs(self)
        out: list[Any] = list(stubs.values())
        seen: set[int] = set()
        for node in self.adk_nodes.values():
            if id(node) not in seen and node.name not in stubs:
                seen.add(id(node))
                out.append(node)
        # Each top-level subnet, with its own graph: the dev UI's "Agent
        # Structure" view opens it when its drawn node (titled by the prefix)
        # is clicked.
        taken = set(stubs) | {getattr(n, "name", None) for n in self.adk_nodes.values()}
        for pre in self.top_subnets():
            if pre not in taken:
                out.append(_SubnetStub(pre, self.sub(pre)))
        return out

    @property
    def edges(self) -> list[Any]:
        stubs = _stubs(self)
        edges = [
            _Edge(stubs[a.src], stubs[a.dst])
            for a in self.arcs
            if a.kind in ("in", "out", "read") and a.src in stubs and a.dst in stubs
        ]
        for t, node in self.adk_nodes.items():
            if t in stubs and node.name not in stubs:
                edges.append(_Edge(stubs[t], node))
        return edges

    # -- subnets ----------------------------------------------------------------

    def top_subnets(self) -> list[str]:
        """The prefixes of the subnets mounted directly in this net (not nested ones)."""
        prefixes = [x.prefix for x in self.subnets]
        return [p for p in prefixes if not any(p.startswith(o + "/") for o in prefixes)]

    def sub(self, prefix: str) -> NetGraph:
        """The subnet mounted at ``prefix`` as a net of its own, names relative to it.

        The parent places its transitions use (the places a mount binds) stay
        under their parent names, drawn as its ports.
        """
        head = prefix + "/"
        info = next((x for x in self.subnets if x.prefix == prefix), None)
        if info is None:
            raise KeyError(prefix)
        inner_t = {t.name for t in self.transitions if t.name.startswith(head)}
        arcs = [a for a in self.arcs if (a.dst if a.kind != "out" else a.src) in inner_t]
        consumed = {a.src for a in arcs if a.kind != "out"}
        produced = {a.dst for a in arcs if a.kind == "out"}

        def rel(name: str) -> str:
            return name.removeprefix(head)

        places: list[PlaceInfo] = []
        for p in self.places:
            if p.name.startswith(head):
                places.append(replace(p, name=rel(p.name)))
            elif p.name in consumed or p.name in produced:
                port = (
                    "inout"
                    if p.name in consumed and p.name in produced
                    else "in"
                    if p.name in consumed
                    else "out"
                )
                places.append(replace(p, port=port, env=False))
        transitions = tuple(
            replace(
                t,
                name=rel(t.name),
                subnet=None if t.subnet == prefix else t.subnet and rel(t.subnet),
            )
            for t in self.transitions
            if t.name in inner_t
        )
        name = info.net.removeprefix("stock:")
        return NetGraph(
            info.agent or name if info.stock else name,
            tuple(places),
            transitions,
            tuple(replace(a, src=rel(a.src), dst=rel(a.dst)) for a in arcs),
            {rel(k): v for k, v in self.adk_nodes.items() if k in inner_t},
            tuple(
                replace(x, prefix=rel(x.prefix)) for x in self.subnets if x.prefix.startswith(head)
            ),
        )

    # -- JSON and DOT -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "places": [asdict(p) for p in self.places],
            "transitions": [asdict(t) for t in self.transitions],
            "arcs": [asdict(a) for a in self.arcs],
            "subnets": [asdict(x) for x in self.subnets],
        }

    def to_dot(
        self,
        marking: Mapping[str, int] | None = None,
        *,
        fired: str | None = None,
        rankdir: str = "TB",
        theme: Theme = "light",
        collapse_stock: bool = True,
        collapse_mounts: bool | None = None,
        alarm: Collection[str] = (),
        compact: bool = False,
        agent: str | None = None,
        key: bool = False,
        author: str | None = None,
    ) -> str:
        """A Petri drawing; ``marking`` replaces the seeds, ``fired`` is highlighted.

        ``theme`` picks the palette of ADK's dev UI (``light`` or ``dark``);
        ``alarm`` places are drawn in red (a counterexample's offending places).
        Mounted blueprints are labelled clusters whose members are labelled
        by their short names. A collapsed subnet is one node (``assistant`` /
        ``llm_agent · summarizer``) wired to the parent places it binds,
        carrying the count of the tokens inside it: a stock subnet with
        ``collapse_stock``, a mounted blueprint with ``collapse_mounts`` (by
        default, when the net has more than 15 places). A collapsed mount
        lists the runs of its function nodes (``second·fast``, as
        :func:`~adk_libpetri.net.blueprint.run_name` names them).

        Read, inhibitor and reset arcs do not shape the layout, and a place
        that inhibits or is read by three or more transitions (a transition
        with more than two reset arcs) is named on those transitions
        (``unless raceWon``, ``resets trigger{A,B,C}``) instead of drawn to
        each. A transition's xor branches that all put a token on one place
        are one arc; competing transitions (sharing an input, of different
        priorities) are lined up by priority.

        ``compact`` (a stock subnet's own drawing) drops the ``LlmStep_``-style
        prefixes from labels, boxes each prefix's part (``LlmStep``), names
        every inhibitor and read on its transition, and tightens the spacing;
        ``agent`` marks the transition that calls that agent's model.
        ``key`` adds a legend of what the drawing's shapes mean, three entries
        per line (a legend wider than the net would shrink the drawing).
        ``author`` is the name the net's events carry (default: this net's; a
        subnet's drawing passes its root net's).

        Node ids (SVG ``<title>``) are the exact place and transition names (a
        transition sharing a place's name is ``t:<name>``), a collapsed
        subnet's is its prefix. SVG element ids are ``place:<name>``,
        ``transition:<name>`` and ``subnet:<prefix>``. ADK's dev UI lights a
        drawn node when its title is an event's author or the last segment of
        its node path, or its label holds that name: a ``node:`` transition's
        label names the node (run) it runs, and the net's answer is emitted
        under the transition that answered. No label holds the net's own name,
        which every event of the net carries (a word joiner, U+2060, breaks it);
        when a title holds it (``Race_Commit`` of net ``race``), an invisible
        decoy titled exactly with it takes the UI's match. The dev UI walks
        back from a lit node through its single predecessors: read, inhibitor
        and reset arcs attach through a port (``"p" -> "t":_``), so their SVG
        titles name no node and only consuming arcs count, and so do a join's
        control inputs (a ``Void`` permit, a place nothing produces) when it
        has a data input, so the walk goes on through the data. A place
        nothing produces (not an environment input, not seeded) is dotted.

        A proof's counterexample splits a transition whose action takes time
        into its start and a ``complete:<T>`` step, with an ``inflight:<T>``
        place between: ``fired`` may name either, and ``inflight:<T>``
        counts show on ``T`` as runs in flight.
        """
        c = _PALETTES[theme]
        tokens = {p.name: p.seed for p in self.places} if marking is None else dict(marking)
        names = {t.name for t in self.transitions}
        if fired is not None and fired not in names and ":" in fired:
            fired = fired.split(":", 1)[1]
        in_flight = {
            k.split(":", 1)[1]: n for k, n in tokens.items() if k.startswith("inflight:") and n
        }
        subnets = {s.prefix: s for s in self.subnets}
        for t in self.transitions:  # graphs built without SubnetInfo
            if t.subnet and t.subnet not in subnets:
                subnets[t.subnet] = SubnetInfo(t.subnet, t.action.removeprefix("subnet:"))
        prefixes = sorted(subnets, key=len, reverse=True)

        def group_of(name: str) -> str | None:
            return next((pre for pre in prefixes if name.startswith(pre + "/")), None)

        if collapse_mounts is None:
            collapse_mounts = len(self.places) > _EXPAND_PLACES
        folded = [
            pre for pre in subnets if (collapse_stock if subnets[pre].stock else collapse_mounts)
        ]
        collapsed = [pre for pre in folded if not any(pre.startswith(o + "/") for o in folded)]

        def hidden_in(name: str) -> str | None:
            return next((pre for pre in collapsed if name.startswith(pre + "/")), None)

        own = author if author is not None else self.name
        # What ADK's dev UI matches labels against: an event's author (the net's
        # name) and the last segment of its node path (a run: fast, second·fast).
        # A label holds a run's name only where that run happens.
        runs_all = {r for t in self.transitions if (r := _runs(t, self.adk_nodes.get(t.name)))}
        if compact and agent:
            runs_all.add(agent)

        def safe(text: str, mine: Collection[str] = (), *, title: bool = False) -> str:
            """``text`` with the net's name, and the runs not ``mine``, broken for ADK's
            substring match (a word joiner: drawn the same). A ``title`` keeps the
            net's name: it must equal the path segment of the answer emitted under
            it (``Race_Commit`` of net ``race``); a decoy titled with the net's name
            takes the UI's match for the name instead."""
            if not title:
                text = _guard(text, own)
            for r in runs_all:
                if r not in mine and not any(r.lower() in m.lower() for m in mine):
                    text = _guard(text, r)
            return text

        place_names = {p.name for p in self.places}
        taken = place_names | names
        ids: dict[str, str] = {p.name: p.name for p in self.places}
        for t in self.transitions:
            ids[t.name] = f"t:{t.name}" if t.name in place_names else t.name
        box_id = {pre: pre if pre not in taken else pre + "/" for pre in collapsed}

        def owns(t: TransitionInfo) -> set[str]:
            """The runs a transition's drawn node stands for (its own, or the model call's)."""
            calls = compact and agent is not None and t.name.endswith(_MODEL_CALL)
            run = _runs(t, self.adk_nodes.get(t.name))
            return {x for x in (run, agent if calls else None) if x}

        def box_owns(pre: str) -> set[str]:
            info = subnets[pre]
            if info.stock:
                return {info.agent} if info.agent else set()
            inner = (t for t in self.transitions if t.name.startswith(pre + "/"))
            return {r for t in inner if (r := _runs(t, self.adk_nodes.get(t.name)))}

        # The UI matches titles too, as text (whole, then any part): guard them alike.
        by_name = {t.name: t for t in self.transitions}
        for k, v in list(ids.items()):
            mine_k = owns(by_name[k]) if k in by_name and k not in place_names else ()
            ids[k] = safe(v, mine_k, title=True)
        for t in self.transitions:
            ids[t.name] = safe(ids[t.name], owns(t), title=True)
        for k, v in list(box_id.items()):
            box_id[k] = safe(v, box_owns(k), title=True)
        # The UI matches a name exactly first, then as a part of any title: a title
        # holding the net's name (Race_Commit, net race) would light up for every
        # event of the net itself (its path's last segment is the net). A decoy
        # titled exactly with the net's name takes that match.
        titles = {*ids.values(), *box_id.values()}
        decoy = (
            own
            if len(own) >= 2 and own not in titles and any(own.lower() in x.lower() for x in titles)
            else None
        )

        def node_id(name: str) -> str:
            box = hidden_in(name)
            if box is not None:
                return box_id[box]
            return ids[name]

        # compact: a stock subnet's parts by name prefix (LlmStep_*, Router_*).
        family: dict[str, str] = {}
        if compact:
            counts: dict[str, int] = {}
            for t in self.transitions:
                fam = _family(t.name)
                if fam:
                    counts[fam] = counts.get(fam, 0) + 1
            main = max(counts, key=lambda f: counts[f]) if counts else None
            for n in [*(p.name for p in self.places), *names]:
                fam = _family(n)
                if fam and fam != main and counts.get(fam, 0) >= 2:
                    family[n] = fam

        def short(name: str, group: str | None) -> str:
            s = name.removeprefix(group + "/") if group else name
            if compact:
                fam = _family(s)
                if fam:
                    s = s.removeprefix(fam + "_")
            return s

        sep, rsep = (
            ("0.25", "0.2") if compact else ("0.3", "0.3") if collapsed else ("0.35", "0.45")
        )
        lines = [
            f"digraph {_q(self.name)} {{",
            f'  graph [rankdir={rankdir}, bgcolor="{c.bg}", nodesep={sep}, ranksep={rsep}, '
            'pad=0.4, fontname="Helvetica", newrank=true, splines=true, ordering=out];',
            f'  node [fontname="Helvetica", fontsize=11, color="{c.line}", '
            f'fontcolor="{c.text}", penwidth=1.2];',
            f'  edge [color="{c.edge}", fontcolor="{c.edge_text}", fontname="Helvetica", '
            "fontsize=9, arrowsize=0.6, penwidth=1.1, arrowhead=vee];",
        ]
        groups: dict[str | None, list[str]] = {None: []}
        fam_lines: dict[str, list[str]] = {}

        def put(name: str, group: str | None, line: str) -> None:
            fam = family.get(name) if group is None else None
            if fam is not None:
                fam_lines.setdefault(fam, []).append(line)
            else:
                groups.setdefault(group, []).append(line)

        produced = {a.dst for a in self.arcs if a.kind == "out"}
        dead = {
            p.name
            for p in self.places
            if p.name not in produced and not p.env and not p.seed and p.port != "in"
        }
        for p in self.places:
            if hidden_in(p.name):
                continue
            group = group_of(p.name)
            n = tokens.get(p.name, 0)
            bad = p.name in alarm
            idle = p.name in dead and not n
            label = _html_label(
                safe(short(p.name, group)), [(_dots(n), None)] if n else [], c, meta_size=12
            )
            attrs: dict[str, str] = {
                "id": f"place:{p.name}",
                "label": label,
                "shape": "ellipse",
                "style": "filled" + (",dashed" if p.env else ",dotted" if idle else ""),
                "fillcolor": c.alarm if bad else c.marked if n else c.place,
                "color": c.inhibit if bad else c.port if p.port else c.line,
                "margin": "0.06,0.03",
                "tooltip": f"{p.name}: {p.type}"
                + (" (environment)" if p.env else "")
                + (f" ({p.port} port)" if p.port else "")
                + (f", {n} token{'s' * (n != 1)}" if n else "")
                + ("; nothing in this net produces it" if p.name in dead else ""),
            }
            if idle:
                attrs["fontcolor"] = c.muted
            if p.port:
                attrs["peripheries"] = "2"
            if compact:
                attrs["height"] = "0.3"  # short rows: the UI fits the drawing to its height
            if bad:
                attrs["penwidth"] = "2"
            put(p.name, group, f"{_q(ids[p.name])} [{_attrs(attrs)}];")

        # Side arcs (reset, inhibitor, read) many transitions share would
        # cross the whole drawing: each such transition names the place instead.
        visible = [a for a in self.arcs if not (hidden_in(a.src) and hidden_in(a.dst))]
        resets: dict[str, list[str]] = {}
        for a in visible:
            if a.kind == "reset":
                resets.setdefault(a.dst, []).append(a.src)
        summed = {t for t, ps in resets.items() if len(ps) > (0 if compact else _RESET_ARCS)}
        sharers: dict[tuple[str, str], set[str]] = {}
        for a in visible:
            if a.kind in ("inhibit", "read") and not hidden_in(a.dst):
                sharers.setdefault((a.src, a.kind), set()).add(a.dst)
        named: dict[str, list[tuple[str, str]]] = {}
        shared_arcs = 1 if compact else _SHARED_ARCS
        for (place, kind), ts in sharers.items():
            if len(ts) >= shared_arcs:
                for t in ts:
                    named.setdefault(t, []).append((kind, place))
        skipped = {(place, t) for t, ps in named.items() for _, place in ps}
        runs_in: dict[str, set[str]] = {}
        for t in self.transitions:
            run = _runs(t, self.adk_nodes.get(t.name))
            if run:
                runs_in.setdefault(run, set()).add(group_of(t.name) or "")

        for t in self.transitions:
            if hidden_in(t.name):
                continue
            group = t.subnet or group_of(t.name)
            node = self.adk_nodes.get(t.name)
            run = _runs(t, node)
            meta: list[tuple[str, str | None]] = []
            calls = compact and agent is not None and t.name.endswith(_MODEL_CALL)
            mine = {x for x in (run, agent if calls else None) if x}
            if run:
                meta.append((f"node:{run}", None))
            elif calls:
                meta.append((f"✦ {agent}", None))
            elif t.action == "emit":
                meta.append(("emit", None))
            prio = f"prio {t.priority}" if t.priority else None
            meta += [(x, None) for x in (t.timing, prio) if x]
            running = in_flight.get(t.name, 0)
            extra: list[str] = []
            if t.name in summed:
                cleared = [short(p, group) for p in resets[t.name]]
                meta.append((f"resets {len(cleared)} place{'s' * (len(cleared) != 1)}", None))
                extra = _wrap(_brace(cleared))
            for kind, place in sorted(named.get(t.name, ())):
                if kind == "inhibit":
                    meta.append((f"unless {short(place, group)}", c.inhibit))
                else:
                    meta.append((f"reads {short(place, group)}", None))
            if running:
                meta.append((f"▶ {running} in flight", None))
            hot = t.name == fired
            marked = bool(run) or calls
            tip = [f"{t.name}: {t.action}"]
            if t.name in summed:
                tip.append(f"resets {', '.join(short(p, group) for p in resets[t.name])}")
            for kind, place in sorted(named.get(t.name, ())):
                verb = "inhibited by" if kind == "inhibit" else "reads"
                tip.append(f"{verb} {short(place, group)}")
            shared = runs_in.get(run, set()) if run else set()
            if len(shared) > 1:
                others = sorted(x or "the root" for x in shared - {group or ""})
                tip.append(
                    f"{run} also runs in {', '.join(others)}: "
                    "ADK's graph lights up one of them only"
                )
            attrs = {
                "id": f"transition:{t.name}",
                "label": _html_label(
                    safe(short(t.name, group), mine),
                    [(safe(x, mine), col) for x, col in meta],
                    c,
                    bold=True,
                    extra=[safe(x, mine) for x in extra],
                ),
                "shape": "box",
                "style": "filled",
                "fillcolor": c.fired if hot else c.running if running else c.transition,
                "color": c.fired_line if hot else c.node_line if marked else c.line,
                "penwidth": "2" if hot else "1.5" if marked else "1.2",
                "margin": "0.12,0.05",
                "height": "0.3",
                "tooltip": "; ".join(tip),
            }
            put(t.name, group, f"{_q(ids[t.name])} [{_attrs(attrs)}];")
        for pre in collapsed:
            info = subnets[pre]
            # The seed marking inside (a stock subnet's permit) is the subnet's own.
            inside = marking is not None and sum(
                n for k, n in tokens.items() if n and k.startswith(pre + "/") and ":" not in k
            )
            inner = [t for t in self.transitions if t.name.startswith(pre + "/")]
            hot = fired in {t.name for t in inner}
            running = sum(in_flight.get(t.name, 0) for t in inner)
            group = group_of(pre)
            if info.stock:
                kind = info.net.removeprefix("stock:")
                what = [kind, info.agent]
                tip = f"{pre}: stock {kind} subnet" + (f" from {info.agent}" if info.agent else "")
                fill, line = c.stock, c.stock_line
                runs_line = None
                mine = {info.agent} if info.agent else set()
            else:
                n_places = sum(1 for p in self.places if p.name.startswith(pre + "/"))
                found = (_runs(t, self.adk_nodes.get(t.name)) for t in inner)
                ran = list(dict.fromkeys(r for r in found if r))
                what = [info.net, f"{n_places} places"]
                runs_line = "runs " + ", ".join(ran) if ran else None
                tip = f"{pre}: blueprint {info.net}"
                if any(len(runs_in.get(r, ())) > 1 for r in ran):
                    tip += "; another mount runs the same nodes: ADK's graph lights up one only"
                fill, line = c.cluster, c.node_line
                mine = set(ran)
            meta = [
                (safe(x, mine), None)
                for x in (
                    *what,
                    f"{_dots(inside)} inside" if inside else None,
                    f"▶ {running}" if running else None,
                )
                if x
            ]
            attrs = {
                "id": f"subnet:{pre}",
                "label": _html_label(
                    safe(short(pre, group), mine),
                    meta,
                    c,
                    bold=True,
                    extra=[safe(x, mine) for x in _wrap(runs_line, 44)] if runs_line else [],
                ),
                "shape": "box",
                "style": "rounded,filled",
                "fillcolor": c.fired if hot else c.running if running else fill,
                "color": c.fired_line if hot else line,
                "penwidth": "2" if hot else "1.5",
                "margin": "0.2,0.1",
                "tooltip": f"{tip}, {len(inner)} transitions (collapsed; open it in "
                "Agent Structure)",
            }
            groups.setdefault(group, []).append(f"{_q(box_id[pre])} [{_attrs(attrs)}];")

        def emit_group(group: str | None, depth: int) -> None:
            pad = "  " * depth
            if group is not None:
                info = subnets.get(group)
                parent = group_of(group)
                title = short(group, parent)
                label = f"{title} · {info.net}" if info and info.net else title
                lines.append(f"{pad}subgraph {_q('cluster_' + group)} {{")
                lines.append(
                    f'{pad}  label={_q(label)}; labeljust="l"; fontsize=11; '
                    f'fontcolor="{c.cluster_text}"; style="rounded,filled"; '
                    f'fillcolor="{c.cluster}"; color="{c.cluster_line}"; penwidth=1; margin=12;'
                )
            lines.extend(f"{pad}  {m}" for m in groups.get(group, []))
            if group is None:
                for fam, members in fam_lines.items():
                    lines.append(f"{pad}  subgraph {_q('cluster_part_' + fam)} {{")
                    lines.append(
                        f'{pad}    label={_q(fam)}; labeljust="l"; fontsize=10; '
                        f'fontcolor="{c.cluster_text}"; style="rounded,dashed"; '
                        f'color="{c.cluster_line}"; penwidth=1; margin=8;'
                    )
                    lines.extend(f"{pad}    {m}" for m in members)
                    lines.append(f"{pad}  }}")
            for child in sorted(subnets, key=len):
                if group_of(child) == group and child not in collapsed and not hidden_in(child):
                    emit_group(child, depth + 1)
            if group is not None:
                lines.append(f"{pad}}}")

        emit_group(None, 0)
        if decoy is not None:
            lines.append(
                f"  {_q(decoy)} [{_attrs({'id': 'net:' + decoy, 'label': '', 'shape': 'point'})}"
                ", style=invis, width=0, height=0];"
            )

        incoming = {a.dst for a in self.arcs if a.kind == "out"}
        sources = [
            ids[p.name]
            for p in self.places
            if (p.env or p.port == "in") and p.name not in incoming and group_of(p.name) is None
        ]
        sinks = [ids[p.name] for p in self.places if p.port == "out" and group_of(p.name) is None]
        if sources:
            lines.append(f"  {{rank=min; {'; '.join(_q(s) for s in sources)};}}")
        if sinks:
            lines.append(f"  {{rank=max; {'; '.join(_q(s) for s in sinks)};}}")
        if not compact:
            for row in self._rivals(hidden_in, group_of):
                chain = " -> ".join(_q(ids[t]) for t in row)
                lines.append(f"  {{rank=same; {chain} [style=invis];}}")

        # A transition's xor branches that all put a token on one place: one arc.
        routes_of: dict[str, set[str]] = {}
        pair_routes: dict[tuple[str, str], list[str]] = {}
        for a in self.arcs:
            if a.kind == "out" and a.route is not None:
                routes_of.setdefault(a.src, set()).add(a.route)
                pair_routes.setdefault((a.src, a.dst), []).append(a.route)

        # A join (a drawn node consuming from two places or more) stops the dev UI's
        # walk back from a lit node: it follows a node's only predecessor. Inputs
        # that carry no data (a Void permit) or that nothing in the net produces
        # (an env signal, a dead place) attach through a port, so the data input
        # is the one predecessor and the walk goes on through it.
        unit = {p.name for p in self.places if p.type in _UNIT_TYPES}
        joins: dict[str, set[str]] = {}
        for a in self.arcs:
            if a.kind == "in" and node_id(a.src) != node_id(a.dst):
                joins.setdefault(node_id(a.dst), set()).add(a.src)
        side_inputs: set[tuple[str, str]] = set()
        for dst, srcs in joins.items():
            if len(srcs) < 2:
                continue
            control = {p for p in srcs if p in unit or p not in produced}
            if control and control != srcs:
                side_inputs |= {(p, dst) for p in control}
        seeded = {p.name for p in self.places if p.seed}

        seen: set[tuple[str, str, str, str]] = set()
        for a in self.arcs:
            src = node_id(a.src)
            dst = node_id(a.dst)
            if src == dst or (a.kind == "reset" and a.dst in summed):
                continue  # inside one collapsed subnet, or summed up on the transition
            if a.kind in ("inhibit", "read") and (a.src, a.dst) in skipped:
                continue  # named on the transition
            attrs = {}
            label = []
            if a.count != "1":
                label.append(a.count)
            if a.route is not None:
                routes = list(dict.fromkeys(pair_routes.get((a.src, a.dst), [a.route])))
                if len(routes) == 1:
                    label.append(a.route)
                elif set(routes) != routes_of.get(a.src, set()):
                    label.append(" | ".join(routes))
            if a.timeout_ms is not None:
                label.append(f"timeout {a.timeout_ms}ms")
            match a.kind:
                case "read":
                    attrs.update(arrowhead="none", style="dashed", tooltip="read")
                case "inhibit":
                    attrs.update(arrowhead="odot", color=c.inhibit, tooltip="inhibitor")
                case "reset":
                    attrs.update(
                        arrowhead="normalnormal", color=c.reset, style="dashed", tooltip="reset"
                    )
                case _:
                    pass
            side = a.kind in ("read", "inhibit", "reset")
            if side:
                # Out of the layout, and out of the dev UI's path walk: an arc
                # through a port is titled "p:_->t:_", naming no node.
                attrs.update(constraint="false", tailport="_", headport="_")
            elif a.kind == "in" and (a.src, dst) in side_inputs:
                attrs.update(headport="_", tooltip="control input")
            elif a.kind == "out" and a.dst in seeded and not hidden_in(a.dst):
                # A permit given back: its place stays up by its consumer.
                attrs.update(constraint="false")
            if label:
                attrs["label"] = " " + safe(" ".join(label)) + " "
            merged = a.kind == "out" and a.route is not None and a.count == "1"
            mark = "" if merged and a.timeout_ms is None else attrs.get("label", "")
            k = (src, dst, a.kind, mark)
            if k in seen:
                continue
            seen.add(k)
            lines.append(
                f"  {_q(src)} -> {_q(dst)}" + (f" [{_attrs(attrs)}]" if attrs else "") + ";"
            )
        if key:
            calls = agent if compact else None
            marked = any(n for k, n in tokens.items() if not hidden_in(k) and ":" not in k)
            lines.append(
                f"  {_key_label(self, c, collapsed, subnets, bool(named), calls, marked, dead)}"
            )
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _rivals(self, hidden_in: Any, group_of: Any) -> list[list[str]]:
        """Top-level transitions that compete for an input, of different priorities, in a
        row by priority (``Race_CommitA``, ``B``, ``C`` before ``Race_DiscardA``, ...).

        Left out: rows of one priority, and a row where one member feeds another.
        """
        ts = {
            t.name: t
            for t in self.transitions
            if not hidden_in(t.name) and group_of(t.name) is None and not t.subnet
        }
        inputs: dict[str, set[str]] = {n: set() for n in ts}
        outputs: dict[str, set[str]] = {n: set() for n in ts}
        for a in self.arcs:
            if a.kind == "in" and a.dst in ts:
                inputs[a.dst].add(a.src)
            elif a.kind == "out" and a.src in ts:
                outputs[a.src].add(a.dst)
        parent = {n: n for n in ts}

        def find(n: str) -> str:
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n

        by_place: dict[str, list[str]] = {}
        for n, ps in inputs.items():
            for p in ps:
                by_place.setdefault(p, []).append(n)
        for members in by_place.values():
            for other in members[1:]:
                parent[find(other)] = find(members[0])
        rows: dict[str, list[str]] = {}
        for n in ts:
            rows.setdefault(find(n), []).append(n)
        out: list[list[str]] = []
        for row in rows.values():
            if len(row) < 2 or len({ts[n].priority for n in row}) < 2:
                continue
            if any(outputs[a] & inputs[b] for a in row for b in row):
                continue
            out.append(sorted(row, key=lambda n: (-ts[n].priority, _natural(n))))
        return out


_RESET_ARCS = 2
"""A transition with more reset arcs than this lists them in its label instead."""
_SHARED_ARCS = 3
"""A place inhibiting (or read by) this many transitions is named on each instead."""
_EXPAND_PLACES = 15
"""A net with more places than this draws its mounted blueprints collapsed."""


_MODEL_CALL = ("_LlmCall", "_LlmCallStream")
"""A stock subnet's transitions that call its agent's model."""

_FAMILY = re.compile(r"^([A-Z][A-Za-z0-9]*)_(?=[A-Za-z])")
_JOINER = "\u2060"
"""WORD JOINER: invisible, and not whitespace to the dev UI's text match."""


def _family(name: str) -> str | None:
    """The ``LlmStep`` of ``LlmStep_LlmCall`` (stock names are ``<Part>_<Name>``)."""
    m = _FAMILY.match(name.rsplit("/", 1)[-1])
    return m.group(1) if m else None


def _guard(text: str, name: str) -> str:
    """``text`` with every occurrence of ``name`` (any case) broken by a word joiner.

    The dev UI lights every drawn node whose label holds an event's author
    (lower-cased, spaces as ``_``); the net's own name is every event's.
    """
    if len(name) < 2 or name.lower() not in text.lower():
        return text
    pattern = re.compile(re.escape(name), re.IGNORECASE)
    return pattern.sub(lambda m: m.group(0)[0] + _JOINER + m.group(0)[1:], text)


def _runs(t: TransitionInfo, node: Any) -> str | None:
    """What a transition runs, as its event path names it (``fast``, ``second·fast``)."""
    if t.action.startswith("node:"):
        return t.action.removeprefix("node:")
    name = getattr(node, "name", None) if node is not None else None
    return name if isinstance(name, str) and name else None


def _natural(name: str) -> list[Any]:
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name)]


def _brace(names: list[str]) -> str:
    """Place names, shortened: ``trigger{A,B,C}``, ``branch{A,B,C}Done``, ``race{Won,Lost}``."""
    left = list(dict.fromkeys(names))
    parts: list[str] = []
    # Names that differ in one character: trigger{A,B,C}, branch{A,B,C}Done.
    buckets: dict[tuple[str, str], list[str]] = {}
    for n in left:
        for i, ch in enumerate(n):
            if ch.isupper() or ch.isdigit():
                buckets.setdefault((n[:i], n[i + 1 :]), []).append(n)
    used: set[str] = set()
    for (head, tail), members in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        members = [m for m in members if m not in used]
        if len(members) < 2:
            continue
        used.update(members)
        letters = ",".join(m[len(head)] for m in members)
        parts.append(f"{head}{{{letters}}}{tail}")
    # Then names that share a first word: race{Won,Discarded}.
    words: dict[str, list[str]] = {}
    for n in left:
        if n not in used:
            m = re.match(r"^([a-z][a-z0-9]*)([A-Z].*)$", n)
            words.setdefault(m.group(1) if m else n, []).append(n)
    for head, members in words.items():
        if len(members) >= 2 and all(m.startswith(head) and m != head for m in members):
            parts.append(f"{head}{{{','.join(m[len(head) :] for m in members)}}}")
        else:
            parts.extend(members)
    return ", ".join(parts)


def _wrap(text: str, width: int = 36) -> list[str]:
    """``text`` in lines of at most ``width`` characters, broken after commas."""
    out: list[str] = []
    line = ""
    for word in text.split(" "):
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        out.append(line)
    return out


_KEY_PER_LINE = 3
"""Legend entries per line: a legend wider than the net would shrink the whole drawing."""

_UNIT_TYPES = frozenset({"Void", "None", "NoneType"})
"""Place types that carry no data (a permit, a signal)."""


def _key_label(
    g: NetGraph,
    c: _Palette,
    collapsed: list[str],
    subnets: Mapping[str, SubnetInfo],
    named: bool,
    agent: str | None = None,
    marked: bool = False,
    dead: Collection[str] = (),
) -> str:
    """A legend of the shapes the drawing uses, as the graph's label (a few per line)."""

    def item(sample: str, colour: str, text: str) -> str:
        return f'<FONT COLOR="{colour}">{sample}</FONT> {html.escape(text, quote=False)}'

    items = [item("⬭", c.line, "place"), item("▭", c.line, "transition")]
    if marked:
        items.append(item("●", c.text, "token"))
    if any(p.env for p in g.places):
        items.append(item("⬭", c.port, "dashed: environment input"))
    if any(p.port for p in g.places):
        items.append(item("◎", c.port, "port"))
    if any(not p.seed for p in g.places if p.name in dead):
        items.append(item("⬭", c.muted, "dotted: nothing produces it"))
    if any(t.action.startswith("node:") or t.name in g.adk_nodes for t in g.transitions):
        items.append(item("▭", c.node_line, "runs an ADK node"))
    if agent:
        items.append(item("▭", c.node_line, f"calls {agent}'s model"))
    if any(subnets[p].stock for p in collapsed) or any(x.stock for x in g.subnets if not collapsed):
        items.append(item("▢", c.stock_line, "stock subnet"))
    if any(not subnets[p].stock for p in collapsed):
        items.append(item("▢", c.node_line, "mounted net"))
    kinds = {a.kind for a in g.arcs}
    if "inhibit" in kinds or named:
        items.append(item("⊸", c.inhibit, "inhibitor (unless)"))
    if "read" in kinds:
        items.append(item("┄", c.edge, "read"))
    rows = [items[i : i + _KEY_PER_LINE] for i in range(0, len(items), _KEY_PER_LINE)]
    body = '<BR ALIGN="LEFT"/>'.join("  ·  ".join(row) for row in rows)
    return (
        f'label=<<FONT POINT-SIZE="9" COLOR="{c.meta}">{body}<BR ALIGN="LEFT"/></FONT>>; '
        'labelloc="b"; labeljust="l";'
    )


def _stubs(g: NetGraph) -> dict[str, _Stub]:
    stubs = {p.name: _Stub(p.name) for p in g.places}
    for t in g.transitions:
        stubs.setdefault(t.name, _Stub(t.name))
    return stubs


def _dots(n: int) -> str:
    return "●" * n if n <= 4 else f"● × {n}"


def _q(s: str) -> str:
    if isinstance(s, _Html):
        return f"<{s}>"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _attrs(attrs: Mapping[str, str]) -> str:
    return ", ".join(f"{k}={_q(v)}" for k, v in attrs.items())


class _Html(str):
    """A graphviz HTML-like label: written as ``<...>``, not quoted."""


def _html_label(
    title: str,
    meta: list[tuple[str, str | None]] | str | None,
    c: _Palette,
    *,
    bold: bool = False,
    meta_size: int = 10,
    extra: list[str] | None = None,
) -> _Html:
    """A bold-able title over a line of ``(text, colour)`` parts joined by `` · ``, and
    ``extra`` lines under it."""
    head = html.escape(title, quote=False)
    if bold:
        head = f"<B>{head}</B>"
    parts = [(meta, None)] if isinstance(meta, str) else list(meta or ())
    rows = [head]
    if parts:
        line = " · ".join(
            html.escape(text, quote=False)
            if colour is None
            else f'<FONT COLOR="{colour}">{html.escape(text, quote=False)}</FONT>'
            for text, colour in parts
        )
        rows.append(f'<FONT POINT-SIZE="{meta_size}" COLOR="{c.meta}">{line}</FONT>')
    for x in extra or ():
        rows.append(f'<FONT POINT-SIZE="9" COLOR="{c.meta}">{html.escape(x, quote=False)}</FONT>')
    return _Html("<BR/>".join(rows))


# ----------------------------------------------------------------------------
#  Building
# ----------------------------------------------------------------------------


def _timing(t: Timing) -> str | None:
    match t.kind:
        case "immediate":
            return None
        case "delayed":
            return f"after {t.earliest_ms}ms"
        case "deadline":
            return f"by {t.latest_ms}ms"
        case "exact":
            return f"at {t.earliest_ms}ms"
        case "window":
            return f"[{t.earliest_ms},{t.latest_ms}]ms"


def _out_arcs(
    t: str, o: Out, route: str | None, timeout: int | None, labels: Mapping[int, str] | None
) -> list[Arc]:
    match o:
        case OutPlace(p):
            return [Arc(t, p.name, "out", route=route, timeout_ms=timeout)]
        case And(cs):
            return [a for c in cs for a in _out_arcs(t, c, route, timeout, None)]
        case Xor(cs):
            arcs: list[Arc] = []
            for i, c in enumerate(cs):
                label = (labels or {}).get(i, route)
                arcs.extend(_out_arcs(t, c, label, timeout, None))
            return arcs
        case Timeout(ms, c):
            return _out_arcs(t, c, route, ms, None)
        case Forward(_, to):
            return [Arc(t, to.name, "out", route=route, timeout_ms=timeout)]


def _count(kind: str, n: int) -> str:
    if kind == "all":
        return "all"
    if kind == "at_least":
        return f">={n}"
    return str(n) if kind == "exactly" else "1"


@experimental
def net_graph(
    spec: NetSpec,
    *,
    seeds: Mapping[str, int] | None = None,
    env: tuple[str, ...] | list[str] = (),
    plans: Mapping[str, Any] | None = None,
    mounts: tuple[Any, ...] = (),
) -> NetGraph:
    """The graph of ``spec``. ``plans`` (a blueprint's) name actions and xor routes."""
    seeds = seeds or {}
    plans = plans or {}
    ports = {p.place.name: p.direction for p in spec.ports}
    places = tuple(
        PlaceInfo(
            p.name,
            p.type_name,
            seed=int(seeds.get(p.name, 0)),
            env=p.name in env,
            port=ports.get(p.name),
        )
        for p in spec.places
    )
    prefixes = sorted((m.prefix for m in mounts), key=len, reverse=True)
    by_prefix = {m.prefix: m for m in mounts}
    transitions: list[TransitionInfo] = []
    arcs: list[Arc] = []
    adk_nodes: dict[str, Any] = {}
    subnets: list[SubnetInfo] = []
    inner: dict[str, TransitionInfo] = {}
    for m in mounts:
        subnets.append(SubnetInfo(m.prefix, m.net, getattr(m, "agent", None)))
        child = getattr(m, "child", None)
        if child is None:
            continue
        cg = blueprint_graph(child)
        inner.update((f"{m.prefix}/{ct.name}", ct) for ct in cg.transitions)
        subnets.extend(replace(x, prefix=f"{m.prefix}/{x.prefix}") for x in cg.subnets)
        adk_nodes.update((f"{m.prefix}/{k}", v) for k, v in cg.adk_nodes.items())
    for t in spec.transitions:
        plan = plans.get(t.name)
        subnet = next((pre for pre in prefixes if t.name.startswith(pre + "/")), None)
        if plan is not None:
            action = f"node:{plan.node.name}" if plan.kind == "node" else str(plan.kind)
            if plan.kind == "node" and plan.node is not None:
                adk_nodes[t.name] = plan.node
        elif subnet is not None and t.name in inner:
            ct = inner[t.name]
            action = ct.action
            if ct.subnet:
                subnet = f"{subnet}/{ct.subnet}"
            node = adk_nodes.get(t.name)
            if node is not None and action.startswith("node:"):
                # As the run is named: second·fast (a function node), else its own name.
                from .blueprint import run_name

                action = f"node:{run_name(t.name.rsplit('/', 1)[0], node)}"
        elif subnet is not None:
            action = f"subnet:{by_prefix[subnet].net}"
        else:
            action = "move"
        transitions.append(TransitionInfo(t.name, action, t.priority, _timing(t.timing), subnet))
        for i in t.inputs:
            arcs.append(Arc(i.place.name, t.name, "in", _count(i.kind, i.count)))
        for p in t.reads:
            arcs.append(Arc(p.name, t.name, "read"))
        for p in t.inhibitors:
            arcs.append(Arc(p.name, t.name, "inhibit"))
        for p in t.resets:
            arcs.append(Arc(p.name, t.name, "reset"))
        if t.output is not None:
            labels: dict[int, str] | None = None
            if plan is not None and isinstance(t.output, Xor):
                labels = {i: label for label, i in plan.routes.items()}
                if plan.default is not None:
                    labels.setdefault(plan.default, "default")
                if plan.error is not None:
                    labels.setdefault(plan.error, "error")
            arcs.extend(_out_arcs(t.name, t.output, None, None, labels))
    return NetGraph(spec.name, places, tuple(transitions), tuple(arcs), adk_nodes, tuple(subnets))


@experimental
def blueprint_graph(bp: Any) -> NetGraph:
    """The graph of a parsed :class:`~adk_libpetri.net.blueprint.Blueprint`."""
    g = net_graph(
        bp.spec,
        seeds=bp.initial_counts(),
        env=bp.env,
        plans=bp.plans,
        mounts=bp.mounts,
    )
    return replace(g, name=bp.name)


__all__ = [
    "Arc",
    "NetGraph",
    "PlaceInfo",
    "SubnetInfo",
    "Theme",
    "TransitionInfo",
    "blueprint_graph",
    "net_graph",
]
