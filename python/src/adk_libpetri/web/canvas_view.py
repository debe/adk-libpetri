"""A net on ADK's builder canvas (``@experimental``).

ADK's builder canvas (the dev UI's pencil button) loads the root from
``GET /dev/apps/{app}/builder`` (``?tmp=true``: the draft the builder
assistant writes, reloaded after every reply). It knows ADK's agent kinds
only: a ``PetriNet`` root shows as one empty box.

:func:`install_canvas_view` puts a route ahead of ADK's that answers, for a
root that is a net (a ``PetriNet`` blueprint or a ``PetriWorkflow``; a net
whose text does not parse included), with what the canvas can show of it
(:func:`canvas_card`):

* ``name`` and ``agent_class`` as in the blueprint (the canvas keeps a root's
  name and type read-only);
* ``tools``: the functions and agents the net runs, each once (its ``node:``
  transitions' functions as ``module.function``, the agents it runs or
  configures a stock subnet from as the YAML file the net loads them from).
  The canvas draws them inside the root box;
* ``description``: the net's size and its last verdicts, or why it does not
  load. The canvas shows no description for a root that is not an
  ``LlmAgent``; the line is there for any other reader.

Nothing more. Sub-agents would be safe to show (the guarded save drops what
the canvas would write for them) but the canvas offers them as editable
``LlmAgent`` forms whose edits would go nowhere, and a status line as a
fake tool would open a tool picker. The places, transitions and proofs are
in ADK's graph panel once saved
(:func:`~adk_libpetri.web.graph_view.install_graph_view`), and the builder
assistant states the verdicts in its replies.

The card round-trips safely: the canvas never sends ``tools`` back for a root
that is not an ``LlmAgent``, and
:func:`~adk_libpetri.web.builder_guard.install_builder_guard` keeps the net on
disk whatever the canvas sends. Every other request (a non-net root, another
file, a missing or invalid app) goes to ADK's own handler unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute

from .._experimental import experimental
from .._net_node import NetNodeBase
from ..net.report import LoadError, load_node, summary
from .builder import PETRI_CLASS, known_verdicts, verdict_line
from .builder_guard import (
    _app_root,
    _ensure_tmp,
    _load,
    _under,
    agents_base,
    involves_net,
    is_helper_package,
    is_net_text,
)
from .drafts import reconcile
from .staging import staged

logger = logging.getLogger(__name__)

BUILDER_PATH = "/dev/apps/{app_name}/builder"
_CLASS_LINE = re.compile(r"""^\s*agent_class\s*:\s*["']?([\w.]+)""", re.MULTILINE)
ROOT_FILE = "root_agent.yaml"


def _items(nodes: Any) -> list[Any]:
    out: list[Any] = []
    for item in nodes or ():
        out.extend(item if isinstance(item, list | tuple) else (item,))
    return out


def _ref(node: Any) -> str | None:
    """A ``FunctionNode``'s function as ``module.function``."""
    fn = getattr(node, "_unwrapped_func", None)
    module, name = getattr(fn, "__module__", None), getattr(fn, "__qualname__", None)
    return f"{module}.{name}" if module and name and "<" not in name else None


def _yaml_refs(raw: Any, here: Path) -> dict[str, str]:
    """Agent name -> the YAML file (as written) its ``nodes``/``edges`` load it from."""
    found: dict[str, str] = {}
    stack = [raw.get("nodes"), raw.get("edges")] if isinstance(raw, dict) else []
    while stack:
        item = stack.pop()
        if isinstance(item, list | tuple):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, str) and item.endswith((".yaml", ".yml")):
            try:
                data = _load((here / item).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = data.get("name") if isinstance(data, dict) else None
            if isinstance(name, str):
                found.setdefault(name, item)
    return found


def runs(node: Any, agents: dict[str, str] | None = None) -> list[str]:
    """What the loaded net ``node`` runs, as canvas tool names, in transition order, once each.

    A function is ``module.function``; an agent is the YAML file it is loaded
    from (``agents``: name -> file), else ``<Class>.<name>``. Every name holds
    a dot: the canvas opens its built-in tool picker for a name without one.
    """
    from ..net.node import PetriNet

    agents = agents or {}
    graph = node.graph
    by_name = {getattr(el, "name", None): el for el in _items(getattr(node, "nodes", None))}

    def agent_ref(name: str, agent: Any) -> str:
        if name in agents:
            return agents[name]
        kind = type(agent).__name__ if agent is not None else "LlmAgent"
        return f"{kind}.{name}"

    names: list[str] = []
    for t in graph.transitions:
        found = graph.adk_nodes.get(t.name)
        if found is not None:
            ref = _ref(found)
            names.append(ref or agent_ref(found.name, found))
    for sub in graph.subnets:
        if sub.agent:
            agent = by_name.get(sub.agent)
            if not isinstance(agent, PetriNet):
                names.append(agent_ref(sub.agent, agent))
    return list(dict.fromkeys(names))


def _claims_line(path: Path, claims: int) -> str:
    if not claims:
        return "no prove: claims"
    report = known_verdicts(path)
    if report is None:
        return f"{claims} claims, not verified yet"
    if report.error:
        return f"{claims} claims, the last verify failed"
    return verdict_line(report).rstrip(".")


def _describe(node: Any, path: Path) -> str:
    g = node.graph
    size = f"{len(g.places)} places, {len(g.transitions)} transitions"
    if getattr(node, "blueprint", None) is None:  # a PetriWorkflow: compiled from its edges
        return f"Petri workflow: {size} (compiled from its edges)"
    s = summary(node)
    subnets = f", {s.subnets} subnets" if s.subnets else ""
    return f"Petri net: {size}{subnets}; {_claims_line(path, s.claims)}"


def canvas_card(path: Path, *, draft: bool = False) -> dict[str, Any]:
    """The canvas's view of the net at ``path``: name, type, description, tools.

    The net (a ``PetriNet`` blueprint or a ``PetriWorkflow``) is loaded
    through ADK's loader. A ``draft`` (the builder's ``<app>/tmp/<app>``
    copy, whose ``agent.py`` may differ from the app's) is loaded from a copy
    under a package name of its own (:func:`~adk_libpetri.web.staging.staged`).
    """
    text = path.read_text(encoding="utf-8")
    data = _load(text)
    raw = data if isinstance(data, dict) else {}
    cls = raw.get("agent_class")
    if not isinstance(cls, str):  # text that does not parse: its agent_class line
        m = _CLASS_LINE.search(text)
        cls = m.group(1) if m else PETRI_CLASS
    card: dict[str, Any] = {"name": str(raw.get("name") or path.parent.name), "agent_class": cls}
    agents = _yaml_refs(raw, path.parent)
    try:
        if draft:
            with staged(path.parent) as stage:
                try:
                    node = _net(load_node(str(stage.path(path))), path)
                except LoadError as err:
                    raise LoadError(stage.restore(err.detail or str(err))) from err
                tools = [stage.restore(n) for n in runs(node, agents)]
                description = _describe(node, path)
        else:
            node = _net(load_node(str(path)), path)
            tools = runs(node, agents)
            description = _describe(node, path)
    except LoadError as err:
        problem = err.detail or str(err)
        for p in (str(path.parent), str(path.parent.resolve())):
            problem = problem.replace(p + "/", "")
        card["description"] = f"Petri net that does not load yet: {_short(problem)}"
        return card
    card["description"] = description
    if tools:
        card["tools"] = [{"name": n} for n in tools]
    return card


def _net(node: Any, path: Path) -> Any:
    if not isinstance(node, NetNodeBase) or getattr(node, "graph", None) is None:
        raise LoadError(f"{path.name}: defines a {type(node).__name__}, not a net")
    return node


def _short(problem: str) -> str:
    """A load error as one line, at most 400 characters."""
    return " ".join(problem.split())[:400]


Handler = Callable[..., Awaitable[Any]]


def _is_root_file(file_path: str | None) -> bool:
    """Whether the canvas asks for the app's ``root_agent.yaml`` (no ``file_path`` or that file)."""
    if not file_path:
        return True
    return posixpath.normpath(file_path.replace("\\", "/")) == ROOT_FILE


@experimental
def install_canvas_view(app: FastAPI, agents_dir: str) -> bool:
    """Put the net's canvas card ahead of ADK's ``GET builder`` in ``app``.

    ``agents_dir`` is the one given to ``get_fast_api_app`` (resolved now, as
    ADK does: :func:`~adk_libpetri.web.builder_guard.agents_base`). Returns
    False when ``app`` has no ADK builder route.
    """
    original: Handler | None = None
    for route in app.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == BUILDER_PATH
            and "GET" in (route.methods or ())
        ):
            original = route.endpoint
            break
    if original is None:
        return False
    adk_get: Handler = original
    base = agents_base(agents_dir)

    def card_of(app_name: str, tmp: bool) -> str | None:
        app_root = _app_root(base, app_name)
        if not app_root.is_dir():
            return None
        if is_helper_package(app_root):
            return ""  # as for an app that does not exist: no draft inside a helper
        if tmp:
            tmp_root = _under(app_root, f"tmp/{app_name}")
            _ensure_tmp(app_root, tmp_root)  # as ADK's GET builder?tmp=true does
            if involves_net(app_root, tmp_root):
                reconcile(app_root, tmp_root)  # files edited in the app since
            target = tmp_root / ROOT_FILE
        else:
            target = app_root / ROOT_FILE
        if not target.is_file() or not is_net_text(target.read_text(encoding="utf-8")):
            return None
        card = canvas_card(target, draft=tmp)
        return yaml.safe_dump(card, sort_keys=False, allow_unicode=True)

    async def get_agent_builder(
        app_name: str, file_path: str | None = None, tmp: bool | None = False
    ) -> Any:
        body = None
        if _is_root_file(file_path):
            try:
                body = await asyncio.to_thread(card_of, app_name, bool(tmp))
            except (ValueError, OSError):
                body = None  # ADK's handler answers as it does
            except Exception:
                logger.exception("builder canvas: no card for %s", app_name)
                body = None
        if body is None:
            return await adk_get(app_name=app_name, file_path=file_path, tmp=tmp)
        return PlainTextResponse(
            body, media_type="application/x-yaml", headers={"Cache-Control": "no-store"}
        )

    route = APIRoute(
        BUILDER_PATH,
        get_agent_builder,
        methods=["GET"],
        response_class=PlainTextResponse,  # as ADK's: its "" answers stay plain text
        response_model_exclude_none=True,
        include_in_schema=False,
    )
    app.router.routes.insert(0, route)
    return True


__all__ = ["BUILDER_PATH", "canvas_card", "install_canvas_view", "runs"]
