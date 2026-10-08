"""A violated claim's counterexample as steps and as a picture (``@experimental``).

A proof that fails returns the firing sequence that breaks the claim and the
marking before the first firing and after each. :func:`steps` reads that
into :class:`Step` values (a transition the verifier split in two shows as
``starts`` and ``completes``, its ``inflight:<T>`` place as ``T`` in
flight); :func:`step_lines` is one plain line per step, for a model or a
terminal; :func:`counterexample_svg` is one self-contained SVG:

* a header (the claim, the net, the step drawn);
* the net at that step (:meth:`~adk_libpetri.net.graph.NetGraph.to_dot`
  with the marking, the transition that fired and the offending places in
  red), when a Graphviz ``dot`` binary is found (:func:`dot_svg`);
* the steps as a filmstrip: each firing with the marking after it, new
  tokens in blue, the tokens that break the claim in red.

The SVG carries ADK's light and dark palettes and picks one with
``prefers-color-scheme``: in an ``<img>`` that follows the embedding page's
``color-scheme``, which ADK's dev UI sets from its theme toggle. Without
``dot`` the picture is the header and the filmstrip, with a line saying so.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .._experimental import experimental
from .graph import _PALETTES, NetGraph, Theme

_INFLIGHT = "inflight:"
_COMPLETE = "complete:"
_HIDDEN = ("env:",)
"""The verifier's own bookkeeping places (environment arrivals), not the net's."""


def _shown(m: Mapping[str, int]) -> dict[str, int]:
    return {k: v for k, v in m.items() if v and not k.startswith(_HIDDEN)}


@experimental
@dataclass(frozen=True)
class Step:
    """One step of a counterexample: what fired and the marking after it.

    Step 0 is the initial marking (``transition`` is empty).
    """

    index: int
    transition: str
    verb: str
    """``fires``, ``starts`` (an action the verifier split) or ``completes``."""
    marking: Mapping[str, int]
    new: frozenset[str] = frozenset()
    """Places (and ``inflight:`` entries) holding more tokens than before this step."""
    fewer: Mapping[str, int] = field(default_factory=dict)
    """Places holding fewer tokens than before this step, with their count now (0: empty)."""
    bad: frozenset[str] = frozenset()
    """Places whose tokens break the claim at this step."""


def _bad(claim: Any, marking: Mapping[str, int], last: bool) -> frozenset[str]:
    places: Sequence[str] = getattr(claim, "places", ())
    match getattr(claim, "kind", ""):
        case "place_bound":
            bound = getattr(claim, "bound", 0)
            return frozenset(p for p in places if marking.get(p, 0) > bound)
        case "unreachable" | "mutual_exclusion" if last:
            return frozenset(p for p in places if marking.get(p, 0))
        case _:
            return frozenset()


@experimental
def steps(claim: Any) -> list[Step]:
    """A violated claim's (a :class:`~adk_libpetri.net.report.ClaimResult`) steps, initial first."""
    fires: Sequence[str] = claim.fires
    markings: Sequence[Mapping[str, int]] = claim.markings
    if not markings:
        return []
    out: list[Step] = []
    n = min(len(fires), len(markings) - 1)
    for i in range(n + 1):
        m = _shown(markings[i])
        before = _shown(markings[i - 1]) if i else {}
        new = frozenset(k for k, v in m.items() if i and v > before.get(k, 0))
        fewer = {k: m.get(k, 0) for k, v in before.items() if m.get(k, 0) < v}
        t, verb = "", ""
        if i:
            f = fires[i - 1]
            if f.startswith(_COMPLETE):
                t, verb = f.removeprefix(_COMPLETE), "completes"
            elif m.get(_INFLIGHT + f, 0) > before.get(_INFLIGHT + f, 0):
                t, verb = f, "starts"
            else:
                t, verb = f, "fires"
        out.append(Step(i, t, verb, m, new, fewer, _bad(claim, m, i == n)))
    return out


def _place(name: str) -> str:
    return f"{name.removeprefix(_INFLIGHT)} in flight" if name.startswith(_INFLIGHT) else name


def _order(item: tuple[str, int]) -> tuple[bool, str]:
    return item[0].startswith(_INFLIGHT), item[0]


def _marking_text(m: Mapping[str, int]) -> str:
    if not m:
        return "(empty)"
    return ", ".join(
        _place(k) + (f"={v}" if v != 1 else "") for k, v in sorted(m.items(), key=_order)
    )


def _change_text(s: Step) -> str:
    """``+a1, -q2, won=2``: what the step changed (a count when it is not 0 or 1)."""
    out = []
    for k, v in sorted({**{k: s.marking[k] for k in s.new}, **s.fewer}.items(), key=_order):
        sign = "+" if k in s.new else "-"
        out.append(f"{sign}{_place(k)}" + (f" (now {v})" if v > 1 or (sign == "-" and v) else ""))
    return ", ".join(out) or "no change"


def violation(claim: Any, last: Step | None) -> str:
    """What is wrong at the last step, in a few words."""
    kind = getattr(claim, "kind", "")
    m = last.marking if last else {}
    places: Sequence[str] = getattr(claim, "places", ())
    match kind:
        case "deadlock_free":
            return "deadlock: no transition can fire"
        case "place_bound":
            over = [f"{p}={m.get(p, 0)}" for p in places if last and p in last.bad]
            return f"{', '.join(over) or 'a place'} exceeds the bound {claim.bound}"
        case "mutual_exclusion":
            return "marked together: " + ", ".join(places)
        case "unreachable":
            return "reached: " + ", ".join(p for p in places if m.get(p, 0))
        case _:
            return "the claim fails here"


@experimental
def step_lines(claim: Any) -> list[str]:
    """One line per step, compact: the initial marking, then what each firing changed.

    ``3. Race_Commit fires: +eventOut, +won, -done``. The last line also has
    the whole marking and what breaks the claim there.
    """
    ss = steps(claim)
    lines = []
    for s in ss:
        if not s.index:
            lines.append(f"0. initial marking: {_marking_text(s.marking)}")
        else:
            lines.append(f"{s.index}. {s.transition} {s.verb}: {_change_text(s)}")
    if len(ss) > 1:
        lines[-1] += f"; marking now: {_marking_text(ss[-1].marking)}"
    if ss:
        lines[-1] += f"  <- {violation(claim, ss[-1])}"
    return lines


# ----------------------------------------------------------------------------
#  Graphviz
# ----------------------------------------------------------------------------

DOT_ENV = "ADK_LIBPETRI_DOT"
"""Names the ``dot`` binary to use; unset, ``dot`` on ``PATH``."""

_DOT_CACHE: OrderedDict[str, str] = OrderedDict()
_DOT_KEPT = 128
_DOT_LOCK = threading.Lock()


def dot_binary() -> str | None:
    """The ``dot`` binary :func:`dot_svg` runs, or None."""
    return os.environ.get(DOT_ENV) or shutil.which("dot")


def dot_svg(src: str, *, timeout: float = 10.0) -> str | None:
    """``src`` rendered by Graphviz ``dot -Tsvg``, or None (no binary, an error, too slow).

    Drawings are cached by source text; a failure is not, so it is tried again.
    """
    with _DOT_LOCK:
        if src in _DOT_CACHE:
            _DOT_CACHE.move_to_end(src)
            return _DOT_CACHE[src]
    exe = dot_binary()
    if exe is None:
        return None
    try:
        done = subprocess.run(
            [exe, "-Tsvg"], input=src.encode("utf-8"), capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.decode("utf-8", "replace") if done.returncode == 0 else ""
    if "<svg" not in out:
        return None
    svg = out
    with _DOT_LOCK:
        _DOT_CACHE[src] = svg
        while len(_DOT_CACHE) > _DOT_KEPT:
            _DOT_CACHE.popitem(last=False)
    return svg


_SIZE = re.compile(r'<svg\b([^>]*?)\swidth="([\d.]+)(?:pt|px)?"\s+height="([\d.]+)(?:pt|px)?"')


def _nested(svg: str, x: float, y: float, scale: float) -> tuple[str, float, float] | None:
    """Graphviz's SVG as an ``<svg>`` element placed at ``x, y``; with its size."""
    start = svg.find("<svg")
    if start < 0:
        return None
    body = svg[start:]
    m = _SIZE.match(body)
    if m is None:
        return None
    w, h = float(m.group(2)) * scale, float(m.group(3)) * scale
    head = f'<svg{m.group(1)} x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"'
    body = re.sub(r"<!--.*?-->", "", head + body[m.end() :], flags=re.S)
    return body.strip(), w, h


# ----------------------------------------------------------------------------
#  The picture
# ----------------------------------------------------------------------------

_FONT = "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
_PAD = 16
_MIN_W = 400
_MAX_W = 760
"""The picture's width; a wider net drawing is scaled down to fit."""


def _w(text: str, size: float, bold: bool = False) -> float:
    return len(text) * size * (0.6 if bold else 0.54)


def _x(s: str) -> str:
    return html.escape(s, quote=True)


def _style() -> str:
    def rules(t: Theme) -> str:
        c = _PALETTES[t]
        return (
            f".bg{{fill:{c.bg}}}.tx{{fill:{c.text}}}.mu{{fill:{c.muted}}}"
            f".chip{{fill:{c.place};stroke:{c.line}}}"
            f".new{{fill:{c.marked};stroke:{c.node_line}}}"
            f".old{{fill:{c.bg};stroke:{c.line};stroke-dasharray:3 2}}"
            f".bad{{fill:{c.alarm};stroke:{c.inhibit}}}.badtx{{fill:{c.inhibit}}}"
            f".sel{{fill:{c.cluster};stroke:{c.fired_line}}}"
            f".dot{{fill:{c.transition};stroke:{c.line}}}"
            f".hot{{fill:{c.fired};stroke:{c.fired_line}}}"
            f".rule{{stroke:{c.cluster_line}}}"
        )

    return (
        f"<style>text{{font-family:{_FONT}}}{rules('light')}"
        ".theme-dark{display:none}"
        f"@media (prefers-color-scheme: dark){{{rules('dark')}"
        ".theme-light{display:none}.theme-dark{display:inline}}</style>"
    )


@experimental
def counterexample_svg(
    graph: NetGraph | None,
    claim: Any,
    *,
    step: int | None = None,
    render: Any = dot_svg,
    max_width: float | None = _MAX_W,
    draw_net: bool = True,
) -> str:
    """The picture of a violated claim's counterexample (see the module docstring).

    ``step`` picks the marking the net is drawn at (default: the last, where
    the claim breaks). ``render`` turns DOT into SVG (None when it cannot):
    :func:`dot_svg` by default. A net drawing wider than ``max_width`` is
    scaled down to it (None: never). Without ``draw_net`` (a picture inside a
    narrow chat panel, where a whole net shrinks past reading) the net is left
    out, and a line says the full-size picture has it.
    """
    ss = steps(claim)
    last = ss[-1] if ss else None
    at = ss[-1 if step is None else max(0, min(step, len(ss) - 1))] if ss else None
    parts: list[str] = []
    y = _PAD

    # -- the net at the step -----------------------------------------------------
    nets: list[tuple[str, float, float]] = []
    if draw_net and graph is not None and at is not None and render is not None:
        fired = None
        if at.transition:
            fired = _COMPLETE + at.transition if at.verb == "completes" else at.transition
        for theme in ("light", "dark"):
            src = graph.to_dot(
                dict(at.marking), fired=fired, theme=theme, alarm=at.bad, collapse_mounts=False
            )
            svg = render(src)
            if svg is None:
                nets = []
                break
            placed = _nested(svg, 0, 0, 1.0)
            if placed is None:
                nets = []
                break
            nets.append(placed)

    net_w = max((w for _, w, _ in nets), default=0.0)
    width = max(_MIN_W, net_w + 2 * _PAD)
    if max_width is not None:
        width = min(max_width, width)
    inner = width - 2 * _PAD

    # -- header ------------------------------------------------------------------
    label = getattr(claim, "label", "claim")
    net = getattr(claim, "net", "")
    parts.append(
        f'<text class="badtx" x="{_PAD}" y="{y + 14}" font-size="15" font-weight="700">'
        f"✗ {_x(label)}</text>"
    )
    y += 22
    where = "no counterexample steps"
    if at is not None and last is not None:
        what = "initial marking" if not at.index else f"{at.transition} {at.verb}"
        where = f"step {at.index} of {last.index}: {what}"
    parts.append(
        f'<text class="mu" x="{_PAD}" y="{y + 12}" font-size="12">'
        f"net {_x(net)} · violated · {_x(where)}</text>"
    )
    y += 20
    if last is not None:
        parts.append(
            f'<text class="badtx" x="{_PAD}" y="{y + 12}" font-size="12" font-weight="600">'
            f"{_x(violation(claim, last))}</text>"
        )
        y += 22

    # -- the net -----------------------------------------------------------------
    if nets:
        scale = min(1.0, inner / net_w) if net_w else 1.0
        h = 0.0
        for theme, (svg, w, nh) in zip(("light", "dark"), nets, strict=True):
            x = _PAD + (inner - w * scale) / 2
            parts.append(
                f'<g class="theme-{theme}" transform="translate({x:.1f} {y + 4:.1f}) '
                f'scale({scale:.4f})">{svg}</g>'
            )
            h = max(h, nh * scale)
        y += h + 14
    elif graph is not None and not draw_net:
        parts.append(
            f'<text class="mu" x="{_PAD}" y="{y + 12}" font-size="11" font-style="italic">'
            "Open the picture full size for the net drawn at this step.</text>"
        )
        y += 22
    elif graph is not None and render is not None:
        parts.append(
            f'<text class="mu" x="{_PAD}" y="{y + 12}" font-size="11" font-style="italic">'
            "No Graphviz dot binary was found, so the net is not drawn here; "
            "the steps are below.</text>"
        )
        y += 22

    # -- the filmstrip -----------------------------------------------------------
    parts.append(f'<line class="rule" x1="{_PAD}" x2="{width - _PAD}" y1="{y}" y2="{y}"/>')
    y += 8
    if len(ss) > 2:
        for line in (
            "Each step shows what it changed: blue tokens added, dashed taken.",
            "Step 0 and the framed rows show every token.",
        ):
            parts.append(
                f'<text class="mu" x="{_PAD}" y="{y + 11}" font-size="10.5">{_x(line)}</text>'
            )
            y += 15
        y += 5
    y += 4
    text_x = _PAD + 30

    def head_of(s: Step) -> tuple[str, str]:
        return ("initial marking", "") if not s.index else (s.transition, s.verb)

    # The marking beside its step when the names leave room, else below it.
    col = max((_w(h, 12.5, True) + _w(v, 12) + 18 for h, v in map(head_of, ss)), default=0)
    beside = col <= (inner - 30) * 0.5
    chips_x0 = text_x + col if beside else text_x
    for s in ss:
        row_top = y
        rows: list[str] = []
        head, verb = head_of(s)
        rows.append(
            f'<circle class="{"hot" if s is at else "dot"}" cx="{_PAD + 11}" cy="{y + 10}" r="10"/>'
            f'<text class="tx" x="{_PAD + 11}" y="{y + 14}" font-size="11" font-weight="700" '
            f'text-anchor="middle">{s.index}</text>'
            f'<text class="tx" x="{text_x}" y="{y + 14}" font-size="12.5" font-weight="700">'
            f"{_x(head)}"
            + (
                f'<tspan class="mu" font-size="12" font-weight="400"> {_x(verb)}</tspan>'
                if verb
                else ""
            )
            + "</text>"
        )
        if not beside:
            y += 25
        x = chips_x0
        # The whole marking where it matters, elsewhere what the step changed.
        whole = not s.index or s is last or s is at
        shown = dict(s.marking) if whole else {k: s.marking[k] for k in s.new}
        if not whole:
            shown.update(s.fewer)
        chips = sorted(shown.items(), key=_order)
        if not chips:
            empty = "no tokens" if whole else "no change"
            rows.append(f'<text class="mu" x="{x:.1f}" y="{y + 14}" font-size="11">{empty}</text>')
        for name, n in chips:
            text = ("▶ " if name.startswith(_INFLIGHT) else "") + name.removeprefix(_INFLIGHT)
            count = "○" if not n else "●" * n if n <= 3 else f"×{n}"
            cw = _w(text, 11) + _w(count, 11) + 22
            if x + cw > width - _PAD and x > chips_x0:
                x, y = chips_x0, y + 24
            taken = not whole and name in s.fewer
            cls = "bad" if name in s.bad else "new" if name in s.new else "old" if taken else "chip"
            tcls = "badtx" if name in s.bad else "mu" if taken else "tx"
            rows.append(
                f'<rect class="{cls}" x="{x:.1f}" y="{y}" width="{cw:.1f}" height="20" rx="10"/>'
                f'<text class="{tcls}" x="{x + 9:.1f}" y="{y + 14}" font-size="11">'
                f'{_x(text)} <tspan font-weight="700">{_x(count)}</tspan></text>'
            )
            x += cw + 6
        y += 24
        if s is last:
            why = f"↳ {violation(claim, s)}"
            wx = chips_x0 if chips_x0 + _w(why, 11.5, True) <= width - _PAD else text_x
            rows.append(
                f'<text class="badtx" x="{wx:.1f}" y="{y + 9}" font-size="11.5" '
                f'font-weight="600">{_x(why)}</text>'
            )
            y += 18
        if s is at:
            parts.append(
                f'<rect class="sel" x="{_PAD - 6}" y="{row_top - 5}" width="{inner + 12:.1f}" '
                f'height="{y - row_top + 4}" rx="8" stroke-width="1"/>'
            )
        parts.extend(rows)
        y += 8
    height = y + _PAD - 6
    title = f"Counterexample: {label}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{_x(title)}">'
        f"<title>{_x(title)}</title>{_style()}"
        f'<rect class="bg" width="100%" height="100%" rx="10"/>' + "".join(parts) + "</svg>"
    )


@experimental
def missing_svg(message: str) -> str:
    """A small picture saying ``message`` (a counterexample the server no longer holds)."""
    w = max(_MIN_W, int(_w(message, 12)) + 2 * _PAD)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="48" '
        f'viewBox="0 0 {w} 48" role="img" aria-label="{_x(message)}">{_style()}'
        f'<rect class="bg" width="100%" height="100%" rx="10"/>'
        f'<text class="mu" x="{_PAD}" y="29" font-size="12">{_x(message)}</text></svg>'
    )


__all__ = [
    "DOT_ENV",
    "Step",
    "counterexample_svg",
    "dot_binary",
    "dot_svg",
    "missing_svg",
    "step_lines",
    "steps",
    "violation",
]
