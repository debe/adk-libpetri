"""``adk-libpetri web``: ADK's dev server, Petri-aware (``@experimental``).

:func:`build_app` is ADK's own ``get_fast_api_app`` (the dev UI at
``/dev-ui``, the run and session APIs) with :class:`PetriAgentLoader` as its
loader. The dev UI is the whole experience: a few routes, inserted ahead of
ADK's own, answer the UI's existing requests with Petri-aware data, and
everything else is ADK's:

===============================================  ================================
``GET  /dev/apps/{app}/build_graph_image``       the net (and subnets) as Petri DOT
``POST /dev/apps/{app}/builder/save``            the canvas cannot write over a net
``GET  /dev/apps/{app}/builder``                 the canvas card: what a net runs
``GET  /dev/petri/counterexamples/{id}.svg``     a violated claim, as a picture
===============================================  ================================
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from .._experimental import experimental
from .builder_guard import install_builder_guard
from .canvas_view import install_canvas_view
from .graph_view import install_graph_view
from .loader import PetriAgentLoader
from .proof_view import install_proof_view

_LOG = logging.getLogger(__name__)


@experimental
def build_app(
    agents_dir: str,
    *,
    loader: PetriAgentLoader | None = None,
    **adk_options: Any,
) -> FastAPI:
    """ADK's dev server for ``agents_dir``, with the Petri loader.

    ADK's builder canvas cannot write over a net, and Save drops the app's
    cached runner so chat runs the saved net
    (:func:`~adk_libpetri.web.builder_guard.install_builder_guard`), and shows
    a net's root with what it runs
    (:func:`~adk_libpetri.web.canvas_view.install_canvas_view`), and ADK's
    graph panel draws a net as a Petri net
    (:func:`~adk_libpetri.web.graph_view.install_graph_view`). A violated
    claim's counterexample is a picture the builder assistant shows in its
    reply (:func:`~adk_libpetri.web.proof_view.install_proof_view`).

    ``adk_options`` go to ADK's ``get_fast_api_app`` (``host``, ``port``,
    ``session_service_uri``, ``reload_agents``, ...).
    """
    from google.adk.cli.fast_api import get_fast_api_app

    loader = loader or PetriAgentLoader(agents_dir)
    adk_options.setdefault("web", True)
    adk_options.setdefault("logo_text", "ADK · libpetri")
    adk_options.setdefault("logo_image_url", "/dev-ui/adk_favicon.svg")
    app = get_fast_api_app(agents_dir=agents_dir, agent_loader=loader, **adk_options)
    forget = _forgetter(app, loader)
    install_builder_guard(app, agents_dir, on_saved=forget)
    install_canvas_view(app, agents_dir)
    install_graph_view(app, loader)
    install_proof_view(app)
    return app


def adk_web_server(app: FastAPI) -> Any:
    """ADK's ``AdkWebServer`` behind ``app``'s routes, or None.

    ``get_fast_api_app`` does not return it; its route handlers close over it.
    """
    for route in app.router.routes:
        for cell in getattr(getattr(route, "endpoint", None), "__closure__", None) or ():
            try:
                value = cell.cell_contents
            except ValueError:  # an empty cell
                continue
            if isinstance(getattr(value, "runners_to_clean", None), set) and hasattr(
                value, "runner_dict"
            ):
                return value
    return None


def _forgetter(app: FastAPI, loader: PetriAgentLoader) -> Callable[[str], None]:
    """Drop an app whose files changed: from the loader's cache and from ADK's runner cache.

    ADK keeps one ``Runner`` per app (``runner_dict``) and evicts it only for
    an app in ``runners_to_clean``, which its file watcher fills under
    ``reload_agents``. Without this, chat would keep running the old net
    after Save while the graph panel draws the new one.
    """
    server = adk_web_server(app)
    if server is None:
        _LOG.warning(
            "adk-libpetri web: ADK's AdkWebServer was not found behind the app's routes; "
            "after Save, chat may run an app's old runner until the server restarts"
        )

    def forget(app_name: str) -> None:
        loader.remove_agent_from_cache(app_name)
        if server is not None:
            server.runners_to_clean.add(app_name)

    return forget


@experimental
def serve(
    agents_dir: str = ".",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    reload_agents: bool = False,
) -> None:
    """Run :func:`build_app` with uvicorn, from ``agents_dir``.

    The process works from ``agents_dir``, as ADK's builder assistant expects:
    its file tools resolve the dev UI's ``<app>/tmp/<app>`` against the
    working directory.
    """
    import uvicorn

    agents_dir = os.path.abspath(agents_dir)
    loader = PetriAgentLoader(agents_dir)
    # Single-agent mode (agents_dir is one app's folder) serves from its parent.
    os.chdir(loader.agents_dir)
    app = build_app(agents_dir, loader=loader, host=host, port=port, reload_agents=reload_agents)
    print(f"ADK dev UI: http://{host}:{port}/dev-ui/")
    uvicorn.run(app, host=host, port=port)


__all__ = ["adk_web_server", "build_app", "serve"]
