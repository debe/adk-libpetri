"""``adk-libpetri web``: ADK's dev server, Petri-aware (``@experimental``).

:func:`build_app` is ADK's own ``get_fast_api_app`` (the dev UI at
``/dev-ui``, the run and session APIs) with :class:`PetriAgentLoader` as its
loader. The dev UI is the whole experience: one route, inserted ahead of
ADK's own, answers the UI's existing request for a net's drawing with
Petri-aware data, and everything else is ADK's:

===============================================  ================================
``GET  /dev/apps/{app}/build_graph_image``       the net (and subnets) as Petri DOT
===============================================  ================================

Nets are written and proved outside the UI, in an editor or by a coding
agent, with ``adk-libpetri check``/``verify`` (``adk-libpetri skill`` prints
the agent skill for it).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI

from .._experimental import experimental
from .graph_view import install_graph_view
from .loader import PetriAgentLoader

_LOG = logging.getLogger(__name__)


@experimental
def build_app(
    agents_dir: str,
    *,
    loader: PetriAgentLoader | None = None,
    **adk_options: Any,
) -> FastAPI:
    """ADK's dev server for ``agents_dir``, with the Petri loader.

    ADK's graph panel draws a net as a Petri net
    (:func:`~adk_libpetri.web.graph_view.install_graph_view`).

    ``adk_options`` go to ADK's ``get_fast_api_app`` (``host``, ``port``,
    ``session_service_uri``, ``reload_agents``, ...).
    """
    from google.adk.cli.fast_api import get_fast_api_app

    loader = loader or PetriAgentLoader(agents_dir)
    adk_options.setdefault("web", True)
    # As `adk web` does: ADK guards a loopback bind against DNS rebinding and
    # warns that a non-loopback one has no authentication.
    if "host" in adk_options:
        adk_options.setdefault("bind_host", adk_options["host"])
    adk_options.setdefault("logo_text", "ADK · libpetri")
    adk_options.setdefault("logo_image_url", "/dev-ui/adk_favicon.svg")
    app = get_fast_api_app(agents_dir=agents_dir, agent_loader=loader, **adk_options)
    if not install_graph_view(app, loader):
        _LOG.warning("ADK's graph route moved: the graph panel draws nets as ADK does")
    return app


@experimental
def serve(
    agents_dir: str = ".",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    reload_agents: bool = False,
) -> None:
    """Run :func:`build_app` with uvicorn, for ``agents_dir``."""
    import uvicorn

    agents_dir = os.path.abspath(agents_dir)
    app = build_app(agents_dir, host=host, port=port, reload_agents=reload_agents)
    print(f"ADK dev UI: http://{host}:{port}/dev-ui/")
    uvicorn.run(app, host=host, port=port)


__all__ = ["build_app", "serve"]
