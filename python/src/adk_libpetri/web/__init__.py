"""ADK's web UI for Petri nets (``@experimental``): ``adk-libpetri web``.

ADK's own dev UI is the experience; nothing in it is patched. The server
answers some of the UI's own requests with Petri-aware data:

* :func:`~adk_libpetri.web.server.build_app` / :func:`~adk_libpetri.web.server.serve`:
  ADK's dev server with everything below installed;
* :class:`~adk_libpetri.web.loader.PetriAgentLoader`: ADK's dev-UI loader (nested apps
  too), serving the Petri builder assistant, and tracing every net it loads when
  given a :class:`~adk_libpetri.bridge.marking_trace.MarkingTraces`;
* :func:`~adk_libpetri.web.builder_guard.install_builder_guard`: ADK's
  builder canvas cannot write its YAML over a net, Save never puts back an
  app file edited since the draft was made (:mod:`~adk_libpetri.web.drafts`),
  and chat runs what was saved (``build_app`` installs it);
* :func:`~adk_libpetri.web.canvas_view.install_canvas_view`: ADK's builder
  canvas shows a net's root with the functions and agents it runs
  (``build_app`` installs it);
* :func:`~adk_libpetri.web.graph_view.install_graph_view`: ADK's graph panel
  draws a net as a Petri net, in the UI's theme, each subnet opening in
  "Agent Structure" (``build_app`` installs it);
* :func:`~adk_libpetri.web.proof_view.install_proof_view`: a violated
  claim's counterexample as a picture the builder assistant puts in its
  reply, drawn in the UI's theme (``build_app`` installs it);
* :func:`~adk_libpetri.web.builder.create_petri_builder_assistant`: ADK's
  Agent Builder Assistant with tools to write, check and verify blueprints
  (each on a copy of the draft, :mod:`~adk_libpetri.web.staging`), and,
  without a model call, a greeting with a net app's verdicts, ``verify`` and
  ``check``, and a plain reply when no Gemini key is set.

Needs ``fastapi`` and ``uvicorn``, which ``google-adk`` installs.
"""

from .builder import create_petri_builder_assistant
from .loader import PetriAgentLoader
from .server import build_app, serve

__all__ = [
    "PetriAgentLoader",
    "build_app",
    "create_petri_builder_assistant",
    "serve",
]
