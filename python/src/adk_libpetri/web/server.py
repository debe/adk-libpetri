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

``/petri`` is an optional power tool, linked from nowhere and kept out of the
OpenAPI schema (``petri_page=False`` leaves it out): a YAML editor beside the
drawn net, verify with steppable counterexamples, and session replays.

==========================================  =====================================
``GET  /petri``                             the page: YAML editor, net, proofs, runs
``GET  /petri/api/apps``                    apps, with their YAML files
``GET  /petri/api/apps/{app}/files/{path}`` a YAML file's text
``PUT  /petri/api/apps/{app}/files/{path}`` write it, reload the app; returns ``check``
``POST /petri/api/apps/{app}/check``        ``{file}``: load and build, no Z3
``POST /petri/api/apps/{app}/verify``       ``{file, k, recursive}``: the ``prove:`` claims
``GET  /petri/api/apps/{app}/net``          ``?file=&marking=&fired=``: graph JSON and DOT
``GET  /petri/api/apps/{app}/sessions``     sessions with a trace
``GET  /petri/api/apps/{app}/trace``        ``?user=&session=``: firings and markings
==========================================  =====================================

Files are confined to ``<agents_dir>/<app>`` and to ``.yaml``/``.yml``. A
write passes the check ADK's ``builder/save`` applies to an upload (no
``args``, code references only inside the app or ADK's built-ins), with one
addition: ``agent_class: adk_libpetri.…``, the blueprint's own class.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .._experimental import experimental
from .._net_node import NetNodeBase
from ..net.graph import NetGraph
from ..net.report import LoadError, check_file, load_net, verify_file
from .builder import PETRI_CLASS, net_digest, remember_verdicts
from .builder_guard import install_builder_guard
from .canvas_view import install_canvas_view
from .drafts import draft_root, reconcile
from .graph_view import install_graph_view
from .loader import PetriAgentLoader
from .proof_view import install_proof_view

_LOG = logging.getLogger(__name__)

_YAML = (".yaml", ".yml")
_SKIP_DIRS = {"tmp", "__pycache__", ".adk", ".venv", "node_modules"}


class _FileBody(BaseModel):
    content: str


class _CheckBody(BaseModel):
    file: str


class _VerifyBody(BaseModel):
    file: str
    k: int | None = None
    recursive: bool = False


def _static(name: str) -> bytes:
    return resources.files("adk_libpetri.web").joinpath("static", name).read_bytes()


_CONTENT_TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}


class _Petri:
    """The ``/petri`` routes over one loader."""

    def __init__(
        self, loader: PetriAgentLoader, forget: Callable[[str], None] | None = None
    ) -> None:
        self.loader = loader
        self.root = Path(loader.agents_dir).resolve()
        self.forget = forget or loader.remove_agent_from_cache

    # -- confinement -------------------------------------------------------------

    def app_dir(self, app: str) -> Path:
        if app.startswith("_") or app not in self.loader.list_agents():
            raise HTTPException(404, f"no app {app!r}")
        d = _app_path(self.root, app).resolve()
        if not d.is_dir() or not d.is_relative_to(self.root) or d == self.root:
            raise HTTPException(404, f"app {app!r} is not a folder")
        return d

    def file(self, app: str, rel: str, *, exists: bool = True) -> Path:
        base = self.app_dir(app)
        p = (base / rel).resolve()
        try:
            p.relative_to(base)
        except ValueError:
            raise HTTPException(400, f"{rel!r} is outside app {app!r}") from None
        if p.suffix not in _YAML:
            raise HTTPException(400, f"{rel!r}: only .yaml/.yml files")
        if exists and not p.is_file():
            raise HTTPException(404, f"no file {rel!r} in app {app!r}")
        return p

    # -- listing -----------------------------------------------------------------

    def apps(self) -> list[dict[str, Any]]:
        out = []
        for app in self.loader.list_agents():
            if app.startswith("_"):
                continue
            d = _app_path(self.root, app)
            files = []
            for p in sorted(d.rglob("*")):
                rel = p.relative_to(d)
                if p.suffix not in _YAML or _SKIP_DIRS & set(rel.parts[:-1]):
                    continue
                files.append({"path": rel.as_posix(), "blueprint": _is_blueprint(p)})
            out.append({"name": app, "files": files})
        return out

    # -- the net -----------------------------------------------------------------

    def graph(self, app: str, file: str | None) -> NetGraph:
        if file:
            p = self.file(app, file)
            if _is_blueprint(p):
                try:
                    return load_net(str(p)).graph
                except LoadError as err:
                    raise HTTPException(422, str(err).replace(str(p), file)) from err
        self.app_dir(app)
        root = self.loader.load_agent(app)
        node = getattr(root, "root_agent", root)
        g = getattr(node, "graph", None)
        if isinstance(node, NetNodeBase) and isinstance(g, NetGraph):
            return g
        raise HTTPException(
            422, f"app {app!r} is not a PetriNet or PetriWorkflow; pick a blueprint file"
        )

    def reload(self, app: str) -> None:
        self.forget(app)


def _app_path(root: Path, app: str) -> Path:
    """An app's folder: ``group.app`` (a nested app, as ADK lists it) is ``group/app``."""
    return root.joinpath(*app.split("."))


def check_upload(content: str, *, filename: str, app: str) -> None:
    """Raise ``ValueError`` if ``content`` could make the loader run code outside ``app``.

    ADK's ``builder/save`` check (its ``_check_uploaded_yaml``): no ``args``
    key, and every code reference (``agent_class``, ``tools``, callbacks,
    schemas, ...) undotted, an ADK built-in, or under ``<app>.``; plus an
    ``agent_class`` under ``adk_libpetri.``, which is what a blueprint is.
    ``app`` is the dotted app name; a nested app's code is under its last
    segment's package as ADK imports it, so the full dotted name is required.
    """
    from google.adk.cli.dev_server import _CODE_REFERENCE_KEYS, _check_code_reference

    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError as err:
        raise ValueError(f"invalid YAML in {filename!r}: {err}") from err

    def refs(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [r for item in value for r in refs(item)]
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            return [value["name"]]
        return []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "args":
                    raise ValueError(
                        f"Blocked key 'args' found in {filename!r}: it can execute arbitrary code"
                    )
                if key in _CODE_REFERENCE_KEYS:
                    for ref in refs(value):
                        if key == "agent_class" and ref.startswith("adk_libpetri."):
                            continue
                        _check_code_reference(ref, app_name=app, filename=filename, field_name=key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for doc in docs:
        walk(doc)


def _relative(report: dict[str, Any], p: Path, rel: str) -> dict[str, Any]:
    """``report`` with its file named as the page knows it (app-relative)."""
    if report.get("error"):
        report["error"] = str(report["error"]).replace(str(p), rel)
    report["path"] = rel
    return report


def _is_blueprint(p: Path) -> bool:
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and data.get("agent_class") == PETRI_CLASS


@experimental
def petri_router(
    loader: PetriAgentLoader, *, forget: Callable[[str], None] | None = None
) -> APIRouter:
    """The ``/petri`` power tool (page and API) for ``loader``'s apps and traces.

    Unlisted: its routes stay out of the OpenAPI schema. ``forget(app)`` runs
    after a file of the app is written (default: drop it from ``loader``'s
    cache).
    """
    petri = _Petri(loader, forget)
    r = APIRouter(prefix="/petri", include_in_schema=False)

    @r.get("", response_class=HTMLResponse, include_in_schema=False)
    @r.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def page() -> HTMLResponse:
        return HTMLResponse(_static("index.html"))

    @r.get("/static/{name}", include_in_schema=False)
    async def static(name: str) -> Response:
        suffix = Path(name).suffix
        if "/" in name or suffix not in _CONTENT_TYPES:
            raise HTTPException(404)
        try:
            body = _static(name)
        except (FileNotFoundError, OSError):
            raise HTTPException(404) from None
        return Response(body, media_type=_CONTENT_TYPES[suffix])

    @r.get("/api/apps")
    async def apps() -> list[dict[str, Any]]:
        return await asyncio.to_thread(petri.apps)

    @r.get("/api/apps/{app}/files/{path:path}")
    async def read_file(app: str, path: str) -> dict[str, Any]:
        def read() -> str:
            return petri.file(app, path).read_text(encoding="utf-8")

        return {"path": path, "content": await asyncio.to_thread(read)}

    @r.put("/api/apps/{app}/files/{path:path}")
    async def write_file(app: str, path: str, body: _FileBody) -> dict[str, Any]:
        try:
            check_upload(body.content, filename=path, app=app)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err

        def write() -> tuple[Path, bool]:
            p = petri.file(app, path, exists=False)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body.content, encoding="utf-8")
            # An open builder draft takes the change too, unless it changed that file itself.
            app_root = petri.app_dir(app)
            reconcile(app_root, draft_root(app_root, app))
            return p, _is_blueprint(p)

        p, blueprint = await asyncio.to_thread(write)
        petri.reload(app)
        check = None
        if blueprint:
            check = _relative((await asyncio.to_thread(check_file, str(p))).to_dict(), p, path)
        return {"path": path, "written": True, "check": check}

    @r.post("/api/apps/{app}/check")
    async def check(app: str, body: _CheckBody) -> dict[str, Any]:
        p = petri.file(app, body.file)
        report = (await asyncio.to_thread(check_file, str(p))).to_dict()
        return _relative(report, p, body.file)

    @r.post("/api/apps/{app}/verify")
    async def verify(app: str, body: _VerifyBody) -> dict[str, Any]:
        p = petri.file(app, body.file)
        digest = await asyncio.to_thread(net_digest, p)
        report = await asyncio.to_thread(verify_file, str(p), body.k, recursive=body.recursive)
        if body.k is None and not body.recursive:
            remember_verdicts(str(p), report, digest=digest)
        return _relative(report.to_dict(), p, body.file)

    @r.get("/api/apps/{app}/net")
    async def net(
        app: str, file: str | None = None, marking: str | None = None, fired: str | None = None
    ) -> dict[str, Any]:
        g = await asyncio.to_thread(petri.graph, app, file)
        counts = None
        if marking:
            try:
                counts = {str(k): int(v) for k, v in json.loads(marking).items()}
            except (ValueError, AttributeError):
                raise HTTPException(400, "marking: a JSON object of place -> count") from None
        return {"graph": g.to_dict(), "dot": g.to_dot(counts, fired=fired, collapse_mounts=False)}

    @r.get("/api/apps/{app}/sessions")
    async def sessions(app: str) -> list[dict[str, Any]]:
        petri.app_dir(app)
        seen: dict[tuple[str, str], list[str]] = {}
        for k in loader.traces.sessions():
            if k.app_name == app:
                seen.setdefault((k.user_id, k.session_id), []).append(k.scope)
        return [{"user": u, "session": s, "scopes": sc} for (u, s), sc in seen.items()]

    @r.get("/api/apps/{app}/trace")
    async def trace(app: str, user: str, session: str) -> dict[str, Any]:
        petri.app_dir(app)
        t = loader.traces.trace(app, user, session)
        if t is None:
            raise HTTPException(404, "no trace for that session (run a turn first)")
        return t

    return r


@experimental
def build_app(
    agents_dir: str,
    *,
    loader: PetriAgentLoader | None = None,
    petri_page: bool = True,
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
    ``petri_page`` mounts the unlisted ``/petri`` power tool
    (:func:`petri_router`).

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
    if petri_page:
        app.include_router(petri_router(loader, forget=forget))
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


__all__ = ["adk_web_server", "build_app", "petri_router", "serve"]
