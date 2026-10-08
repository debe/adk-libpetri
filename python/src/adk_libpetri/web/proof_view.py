"""Counterexamples as pictures inside ADK's dev UI (``@experimental``).

ADK's chat panels (the builder assistant's included) render a reply as
Markdown through ngx-markdown and Angular's HTML sanitizer: an ``<img>``
from the same server shows inline (``width`` and an ``<a target>`` around it
survive the sanitizer), a raw ``<svg>`` or ``<iframe>`` does not, and ADK's
CSS puts no width limit on such an image. So a violated claim's
counterexample reaches the user as an image line the assistant copies into
its reply:

* :func:`remember_counterexample` keeps a violated claim with the graph of
  its net (at most 64, oldest dropped) under a short id derived from both,
  so verifying the same net again gives the same id;
* :func:`install_proof_view` adds ``GET /dev/petri/counterexamples/{id}.svg``
  answered by :func:`~adk_libpetri.net.counterexample.counterexample_svg`:
  the claim and its steps, at most 480 px wide, for the panel; with
  ``?full=1`` (the new tab a click opens) unscaled and with the net drawn at
  a step (``?step=``, the last by default). Inside the panel a whole net
  shrinks past reading, so the inline picture leaves it out;
* :func:`picture` is the line the builder's ``verify_petri_blueprint`` hands
  the model for each violated claim: the image at the panel's width, a
  click opening it full size in a new tab. Only once the route is installed:
  under stock ``adk web`` nothing serves it, and the claim has its text
  ``steps`` only.

The SVG follows ADK's light or dark theme by itself (``prefers-color-scheme``
inside an ``<img>`` follows the page's ``color-scheme``, which ADK's theme
toggle sets). An id the server no longer holds (a restart, or 64 newer ones)
answers 404 with a small picture saying to verify again, and a drawing that
fails answers 500 with a picture saying so: the reply never shows a broken
image.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.routing import APIRoute

from .._experimental import experimental
from ..net.counterexample import counterexample_svg, missing_svg
from ..net.graph import NetGraph

logger = logging.getLogger(__name__)

PICTURE_PATH = "/dev/petri/counterexamples/{cex_id}.svg"

_KEPT = 64
_MAX_WIDTH = 480
"""The inline picture's widest: ADK's builder panel is about 360 px wide, and a
wider picture shrinks its steps past reading. The new-tab view (``full=1``)
is unscaled and draws the net."""


@dataclass(frozen=True)
class _Kept:
    graph: NetGraph | None
    claim: Any


_STORE: OrderedDict[str, _Kept] = OrderedDict()
_LOCK = threading.Lock()
_BASE: list[str] = []
"""The URL path the route is served under (``root_path``) by each server installed in
this process, once each; the last one installed names the pictures."""


def _id(graph: NetGraph | None, claim: Any) -> str:
    """Derived from the counterexample and the net it is drawn on: same input, same id."""
    key = json.dumps(
        [
            claim.net,
            claim.label,
            list(claim.fires),
            [sorted(m.items()) for m in claim.markings],
            graph.to_dict() if graph is not None else None,
        ],
        default=str,
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@experimental
def remember_counterexample(graph: NetGraph | None, claim: Any) -> str:
    """Keep a violated ``claim`` (a ``ClaimResult``) and ``graph``; return its id."""
    cid = _id(graph, claim)
    with _LOCK:
        _STORE[cid] = _Kept(graph, claim)
        _STORE.move_to_end(cid)
        while len(_STORE) > _KEPT:
            _STORE.popitem(last=False)
    return cid


def _kept(cid: str) -> _Kept | None:
    with _LOCK:
        return _STORE.get(cid)


def forget_counterexamples() -> None:
    """Drop every kept counterexample (tests)."""
    with _LOCK:
        _STORE.clear()


def installed() -> bool:
    """Whether a server in this process serves the pictures."""
    return bool(_BASE)


def picture_url(cid: str, step: int | None = None, *, full: bool = False) -> str:
    """The picture's URL; ``full`` draws a wide net unscaled (the new-tab view)."""
    base = _BASE[-1] if _BASE else ""
    query = "&".join(
        q for q in (None if step is None else f"step={step}", "full=1" if full else None) if q
    )
    return base + PICTURE_PATH.format(cex_id=cid) + (f"?{query}" if query else "")


def _alt(label: str) -> str:
    return "".join(ch for ch in label if ch not in "[]\n\r") or "claim"


@experimental
def picture(graph: NetGraph | None, claim: Any) -> str | None:
    """The image line (HTML) of a violated claim's counterexample, or None (no route serves it)."""
    if not installed() or not claim.markings:
        return None
    cid = remember_counterexample(graph, claim)
    src = html.escape(picture_url(cid), quote=True)
    href = html.escape(picture_url(cid, full=True), quote=True)
    alt = html.escape(f"Counterexample: {_alt(claim.label)}", quote=True)
    return f'<a href="{href}" target="_blank"><img src="{src}" alt="{alt}" width="100%"></a>'


@experimental
def install_proof_view(app: FastAPI) -> None:
    """Serve the counterexample pictures from ``app`` (under its ``root_path``)."""

    async def counterexample_picture(
        cex_id: str, step: int | None = None, full: bool = False
    ) -> Response:
        kept = _kept(cex_id)
        headers = {"Cache-Control": "no-cache"}
        if kept is None:
            body = missing_svg("This counterexample is no longer on the server: verify again.")
            return Response(
                body, 404, media_type="image/svg+xml", headers={"Cache-Control": "no-store"}
            )
        try:
            svg = await asyncio.to_thread(
                counterexample_svg,
                kept.graph,
                kept.claim,
                step=step,
                max_width=None if full else _MAX_WIDTH,
                draw_net=full,
            )
        except Exception as err:
            # A drawing bug: still a picture (the reply shows it, not a broken
            # image), saying what failed; the claim's text steps stand.
            logger.exception("counterexample %s: cannot draw it", cex_id)
            body = missing_svg(f"Cannot draw this counterexample ({type(err).__name__}).")
            return Response(
                body, 500, media_type="image/svg+xml", headers={"Cache-Control": "no-store"}
            )
        return Response(svg, media_type="image/svg+xml", headers=headers)

    app.router.routes.insert(
        0,
        APIRoute(PICTURE_PATH, counterexample_picture, methods=["GET"], include_in_schema=False),
    )
    base = str(getattr(app, "root_path", "") or "").rstrip("/")
    with _LOCK:
        if base in _BASE:
            _BASE.remove(base)
        _BASE.append(base)


__all__ = [
    "PICTURE_PATH",
    "forget_counterexamples",
    "install_proof_view",
    "installed",
    "picture",
    "picture_url",
    "remember_counterexample",
]
