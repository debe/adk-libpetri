"""ADK's web UI for Petri nets (``@experimental``): ``adk-libpetri web``.

ADK's own dev UI is the experience; nothing in it is patched. The server
answers the UI's own request for an app's drawing with Petri-aware data:

* :func:`~adk_libpetri.web.server.build_app` / :func:`~adk_libpetri.web.server.serve`:
  ADK's dev server with everything below installed;
* :class:`~adk_libpetri.web.loader.PetriAgentLoader`: ADK's dev-UI loader (nested apps
  too), leaving helper packages out of the app list, and tracing every net it
  loads when given a :class:`~adk_libpetri.bridge.marking_trace.MarkingTraces`;
* :func:`~adk_libpetri.web.graph_view.install_graph_view`: ADK's graph panel
  draws a net as a Petri net, in the UI's theme, each subnet opening in
  "Agent Structure" (``build_app`` installs it).

google-adk 2.15 removed its Visual Agent Builder, and with it the builder
assistant this package used to replace. A net is written and proved outside
the UI, in an editor or by a coding agent: ``adk-libpetri skill`` prints the
agent skill for that loop (``check``, ``verify``, fix from the
counterexample).

Needs ``fastapi`` and ``uvicorn``, which ``google-adk`` installs.
"""

from .loader import PetriAgentLoader
from .server import build_app, serve

__all__ = [
    "PetriAgentLoader",
    "build_app",
    "serve",
]
