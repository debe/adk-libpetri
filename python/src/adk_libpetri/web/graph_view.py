"""The net in ADK's own graph panel (``@experimental``).

ADK's dev UI asks ``GET /dev/apps/{app}/build_graph_image?dark_mode=`` once
per app it shows, for ``{"<path>": {"dotSrc": "<DOT>"}}`` (``""`` is the
root), and draws that DOT itself, in its Info panel and its fullscreen
"Agent Structure" view. ADK's own answer for a net is generic boxes, one per
place and transition.

:func:`install_graph_view` puts a route ahead of ADK's that answers for an
app whose root is a ``PetriNet`` or ``PetriWorkflow`` with
:meth:`~adk_libpetri.net.graph.NetGraph.to_dot` in the UI's theme: places,
transitions, read, inhibitor and reset arcs, mounted blueprints as clusters
(collapsed to one node each in a net of more than 15 places), stock subnets
as one node each, and a one-line key of the shapes (ADK's own legend, of
agents and tools, is the overlay's and stays).

* Highlighting: the UI lights up a drawn node when its title is an event's
  author or the last segment of its node path, or its label contains that
  name. A ``node:`` transition (its label names the run, ``fast``, or
  ``second·fast`` in a mount) lights up when its node emits; a failed
  transition's error event is authored by it, or by its top-level subnet
  (``assistant``), so that lights up. Nothing is titled or labelled with
  the net's own name: every event under the net carries it as its author,
  so it would light on every event, an error included. From a lit node the
  UI walks back through single predecessors; side arcs do not count as such.
* Drill-down: the answer holds a drawing for every subnet too, under its
  prefix (``first``, ``first/inner``), and ``node=<prefix>`` answers with
  that one (``{"dotSrc": ...}``). A stock subnet's is compact: its parts
  boxed, their prefixes dropped from labels, its model call marked with the
  agent it is configured from. The duck-typed graph lists each subnet as a
  node with a graph of its own, so ADK's "Agent Structure" view opens it on
  a click, with its breadcrumbs.

Every other request (other apps, apps that fail to load, a ``node=`` that is
not a subnet of a net) goes to ADK's own handler unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from .._experimental import experimental
from .._net_node import NetNodeBase
from ..net.graph import NetGraph, Theme

logger = logging.getLogger(__name__)

GRAPH_PATH = "/dev/apps/{app_name}/build_graph_image"


def net_graph_of(loader: Any, app_name: str) -> NetGraph | None:
    """The app's root net graph, or None (not a net, or it does not load)."""
    try:
        root = loader.load_agent(app_name)
    except Exception:  # ADK's own handler reports it
        return None
    node = getattr(root, "root_agent", root)
    g = getattr(node, "graph", None)
    return g if isinstance(node, NetNodeBase) and isinstance(g, NetGraph) else None


def subnet_dot(g: NetGraph, prefix: str, theme: Theme) -> str:
    """A subnet's own drawing: a stock one compact, its model call marked with its agent."""
    info = next(x for x in g.subnets if x.prefix == prefix)
    sub = g.sub(prefix)
    return sub.to_dot(theme=theme, key=True, compact=info.stock, agent=info.agent, author=g.name)


def drawings(g: NetGraph, theme: Theme) -> dict[str, dict[str, str]]:
    """The net's drawing under ``""``, and each subnet's under its prefix (the UI preloads them)."""
    out = {"": {"dotSrc": g.to_dot(theme=theme, key=True)}}
    for x in g.subnets:
        out[x.prefix] = {"dotSrc": subnet_dot(g, x.prefix, theme)}
    return out


@experimental
def install_graph_view(app: FastAPI, loader: Any) -> bool:
    """Put the Petri drawing ahead of ADK's ``build_graph_image`` in ``app``.

    ``loader`` is the server's agent loader. Returns False when ``app`` has no
    such ADK route (then there is nothing to draw in).
    """
    original: Callable[..., Awaitable[Any]] | None = None
    for route in app.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == GRAPH_PATH
            and "GET" in (route.methods or ())
        ):
            original = route.endpoint
            break
    if original is None:
        return False
    adk_graph = original

    async def build_graph_image(
        app_name: str, dark_mode: bool = False, node: str | None = None
    ) -> Any:
        g = await asyncio.to_thread(net_graph_of, loader, app_name)
        theme: Theme = "dark" if dark_mode else "light"
        path = (node or "").strip("/")
        if g is not None and path and g.name and path.split("/")[0] == g.name:
            path = path.split("/", 1)[1] if "/" in path else ""  # ADK's paths may name the root
        if g is None or (path and path not in {x.prefix for x in g.subnets}):
            return await adk_graph(app_name=app_name, dark_mode=dark_mode, node=node)
        if path:
            dot = subnet_dot(g, path, theme)
            return {"dotSrc": dot, path: {"dotSrc": dot}}
        return drawings(g, theme)

    app.router.routes.insert(
        0, APIRoute(GRAPH_PATH, build_graph_image, methods=["GET"], include_in_schema=False)
    )
    return True


__all__ = ["GRAPH_PATH", "drawings", "install_graph_view", "net_graph_of", "subnet_dot"]
