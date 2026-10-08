"""Keeps a net safe from ADK's builder canvas (``@experimental``).

ADK's builder canvas (the dev UI's pencil button) knows ADK's agent kinds
only. It re-serializes its own model of the root (``name``, ``model``,
``agent_class``, ``description``, ``instruction``, ``sub_agents``, ``tools``,
callbacks) and POSTs that to ``/dev/apps/{app}/builder/save?tmp=true``
before every message to the builder assistant, and again (``tmp`` then
real) on Save. For a ``PetriNet`` root, that YAML has no ``places``,
``transitions`` or ``prove:``. ADK either writes it over the net (when the
canvas changed or dropped ``agent_class``) or rejects the whole save with
400, because ``adk_libpetri.net.PetriNet`` is a code reference outside the
app.

:func:`install_builder_guard` puts a route ahead of ADK's own save. For each
uploaded file whose current version is a net (the tmp draft's when there is
one, else the app's): a root whose ``agent_class`` is an ``adk_libpetri``
class (``PetriNet`` or ``PetriWorkflow``), or text that does not parse but
says so on its ``agent_class`` line (a half-finished edit fails closed),
and whose upload carries no net of its own (none of :data:`NET_KEYS`), the
upload is not written: the file on disk stays byte for byte as it is.

Nothing of that upload is taken. For a net root the canvas offers no field to
edit (name and agent type are disabled; it shows no description or
instruction), so its YAML only echoes what it last loaded, and an echo can
be stale: after the assistant rewrites the draft, the canvas still sends the
description it read before. Sub-agents, tools or callbacks added to a net
root in the canvas have no place in a net and are dropped, and so are the
agent files the canvas writes for such sub-agents. What was dropped is
logged and kept as a note (:func:`take_note`) the builder assistant tells
the user in its next reply. A real save (the canvas's Save button) that
would drop such an addition writes nothing and answers false: the builder
stays open (ADK shows its generic error) instead of closing as if the
addition were saved, and the note says to delete it on the canvas. A tool
renamed on the canvas cannot be seen (the canvas never sends a net root's
tools), so every note says renames are not saved. Edit a net in its YAML,
or ask the assistant.

Everything else goes to ADK's own handler unchanged: other files, apps
without a net, and uploads that carry net keys (ADK's upload checks then
apply as usual). Before the handler runs on a tmp save, the draft folder is
created as ADK's ``GET builder?tmp=true`` would (a copy of the app), so a
net's helpers (``agent.py``, mounted YAML) are in it.

On a real save, ADK copies every draft file into the app, so Save ships what
the builder assistant wrote there. For an app whose root (or its draft's) is a
net, the draft is first brought in step with the app
(:func:`~adk_libpetri.web.drafts.reconcile`): a file changed in the app after
the draft was made, and not in the draft, is refreshed in the draft, so the
copy cannot put it back. A file changed in both stops the save (the canvas
says "Something went wrong", the builder assistant names the files). After
every real save that succeeded, ``on_saved`` runs (the server drops the app
from its loader's cache and ADK's runner cache).

An app name that is a helper package (a package folder with no agent, which
the dev UI does not list, so "Create new app" lets its name through) is never
written: the save fails as above.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import posixpath
import re
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, UploadFile
from fastapi.routing import APIRoute

from .._experimental import experimental
from .drafts import make_draft, reconcile, without_marker
from .loader import defines_agent

logger = logging.getLogger(__name__)

SAVE_PATH = "/dev/apps/{app_name}/builder/save"

NET_KEYS = frozenset(
    {"nodes", "places", "transitions", "env", "ports", "subnets", "prove"}  # PetriNet
    | {"edges", "back_edge_budget", "state", "multi_route", "max_concurrency", "interruptible"}
)
"""Keys that make an upload a net of its own (a ``PetriNet`` blueprint or a
``PetriWorkflow``), not the canvas's re-serialization."""

NET_CLASS_PREFIX = "adk_libpetri."
"""A root whose ``agent_class`` starts with this is protected (``PetriNet``, ``PetriWorkflow``)."""

_NET_TEXT = re.compile(r"""^\s*agent_class\s*:\s*["']?adk_libpetri\.""", re.MULTILINE)

_YAML = (".yaml", ".yml")
_CANVAS_ONLY = frozenset(
    {"model", "instruction", "sub_agents", "tools"}
    | {f"{w}_{x}_callbacks" for w in ("before", "after") for x in ("agent", "model", "tool")}
)

_ECHOES = ("model", "instruction")
"""Fields the canvas writes for any ``LlmAgent`` it re-serializes, defaults included:
dropped from a net, but no sign the user added anything."""

Save = Callable[..., Awaitable[Any]]


def _load(content: bytes | str) -> Any:
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError:
        return None


def is_net(data: Any) -> bool:
    """Whether ``data`` (a parsed YAML document) is a net: an ``adk_libpetri`` root class."""
    if not isinstance(data, dict):
        return False
    cls = data.get("agent_class")
    return isinstance(cls, str) and cls.startswith(NET_CLASS_PREFIX)


def is_net_text(text: str) -> bool:
    """Whether the YAML ``text`` is a net; fails closed on text that does not parse.

    A half-finished edit of a net (one brace missing) is still that net: its
    ``agent_class`` line names an ``adk_libpetri`` class.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return bool(_NET_TEXT.search(text))
    return is_net(data)


def carries_net(data: Any) -> bool:
    """Whether an upload holds a net of its own (any of :data:`NET_KEYS`)."""
    return isinstance(data, dict) and bool(NET_KEYS & set(data))


def agents_base(agents_dir: str) -> Path:
    """The folder ADK's builder routes resolve app names against, as ADK does.

    ``agents_dir`` resolved against the working directory; in single-agent
    mode (``agents_dir`` is one app's folder) its parent, as
    ``get_fast_api_app`` serves it.
    """
    from google.adk.cli.utils.agent_loader import is_single_agent_directory

    path = (Path.cwd() / agents_dir).resolve()
    return path.parent if is_single_agent_directory(path) else path


def _app_root(base: Path, app_name: str) -> Path:
    # ADK's _get_app_root.
    if app_name in ("", ".", "..") or Path(app_name).name != app_name or "\\" in app_name:
        raise ValueError(f"Invalid app name: {app_name!r}")
    root = (base / app_name).resolve()
    if not root.is_relative_to(base):
        raise ValueError(f"Invalid app name: {app_name!r}")
    return root


def _rel_path(app_name: str, filename: str | None) -> str:
    # ADK's _parse_upload_filename.
    if not filename:
        raise ValueError("Upload filename is missing.")
    name = filename.replace("\\", "/").lstrip("/")
    rel = name[len(app_name) + 1 :] if name.startswith(f"{app_name}/") else name
    if not rel or ".." in rel.split("/"):
        raise ValueError(f"Invalid upload filename: {filename!r}")
    if os.path.splitext(rel)[1].lower() not in _YAML:
        raise ValueError(f"File type not allowed: {rel!r}")
    return rel


def _under(root: Path, rel: str) -> Path:
    p = root / rel
    if not p.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes root_dir: {rel!r}")
    return p


def _ensure_tmp(app_root: Path, tmp_root: Path) -> None:
    """The draft folder, made the way ADK's ``GET builder?tmp=true`` makes it (with a baseline)."""
    make_draft(app_root, tmp_root)


def is_helper_package(app_root: Path) -> bool:
    """Whether ``app_root`` is a package that defines no agent (a helper of ``.agent.fn`` refs).

    The dev UI does not list it (:class:`~adk_libpetri.web.loader.PetriAgentLoader`),
    so its "Create new app" dialog lets its name through.
    """
    return (
        app_root.is_dir() and (app_root / "__init__.py").is_file() and not defines_agent(app_root)
    )


def is_net_file(path: Path) -> bool:
    """Whether the YAML file ``path`` is a net (:func:`is_net_text`); fails closed on a file
    that is not UTF-8. Raises ``OSError`` if it cannot be read."""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return True
    return is_net_text(text)


def involves_net(app_root: Path, tmp_root: Path) -> bool:
    """Whether the app's root or its draft's is a net."""
    for root in (app_root, tmp_root):
        try:
            if is_net_file(root / "root_agent.yaml"):
                return True
        except OSError:
            continue
    return False


def _upload(name: str | None, content: bytes, headers: Any = None) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name, headers=headers)


def _config_refs(rel: str, data: Any) -> set[str]:
    """The files an agent config names: its sub-agents' and agent tools' ``config_path``."""
    if not isinstance(data, dict):
        return set()
    paths = [s.get("config_path") for s in data.get("sub_agents") or () if isinstance(s, dict)]
    for tool in data.get("tools") or ():
        agent = ((tool.get("args") or {}) if isinstance(tool, dict) else {}).get("agent")
        paths.append(agent.get("config_path") if isinstance(agent, dict) else None)
    here = posixpath.dirname(rel)
    return {posixpath.normpath(posixpath.join(here, p)) for p in paths if isinstance(p, str)}


def _names(items: Any) -> list[str]:
    """What the canvas calls each added sub-agent, tool or callback."""
    out: list[str] = []
    for item in items if isinstance(items, list) else [items]:
        if isinstance(item, dict):
            ref = item.get("name") or item.get("config_path") or item.get("code")
            out.append(posixpath.basename(str(ref)).removesuffix(".yaml") if ref else "?")
        elif item:
            out.append(str(item))
    return out


def _plan(
    app_root: Path, tmp_root: Path, uploads: list[tuple[str, bytes]]
) -> tuple[list[str], list[int], list[str]]:
    """The nets an upload would replace (kept), the uploads that pass to ADK, and what was dropped.

    The agent files the canvas writes for sub-agents or agent tools it put
    under a kept net (its YAML names them) are not written either.
    """
    kept: list[str] = []
    passed: list[int] = []
    dropped: list[str] = []
    orphans: set[str] = set()
    for i, (rel, content) in enumerate(uploads):
        draft = _under(tmp_root, rel)
        current = draft if draft.is_file() else _under(app_root, rel)
        data = _load(content)
        if carries_net(data) or not current.is_file():
            passed.append(i)
            continue
        if not is_net_file(current):
            passed.append(i)
            continue
        fields = data.items() if isinstance(data, dict) else ()
        added = sorted((k, _names(v)) for k, v in fields if k in _CANVAS_ONLY and v)
        if added:
            logger.warning(
                "builder save: %s is a net; dropped the canvas's %s", rel, [k for k, _ in added]
            )
            dropped += [f"{k} ({', '.join(n)})" if n else k for k, n in added]
        logger.info("builder save: kept the net in %s (the canvas's YAML not written)", rel)
        kept.append(rel)
        orphans |= _config_refs(rel, data)
    parsed = {i: _load(uploads[i][1]) for i in passed}
    while True:
        more = {i for i in passed if uploads[i][0] in orphans}
        if not more:
            break
        for i in sorted(more):
            logger.warning(
                "builder save: %s is the canvas's agent under a net; not written", uploads[i][0]
            )
            orphans |= _config_refs(uploads[i][0], parsed[i])
        passed = [i for i in passed if i not in more]
    return kept, passed, dropped


# ----------------------------------------------------------------------------
#  What the last save dropped, for the builder assistant to tell the user
# ----------------------------------------------------------------------------

_NOTES: dict[str, str] = {}
_NOTES_LOCK = threading.Lock()


def _set_note(app_name: str, text: str) -> None:
    with _NOTES_LOCK:
        before = _NOTES.get(app_name)
        if before and text in before:
            return  # said already (the canvas sends the same save again and again)
        _NOTES[app_name] = f"{before}\n\n{text}" if before else text


_RENAMES = "Tools renamed on the canvas are not saved either."


def _note(app_name: str, kept: list[str], dropped: list[str]) -> None:
    _set_note(
        app_name,
        f"The builder canvas cannot change a Petri net: its last save kept "
        f"{', '.join(kept)} as it was and dropped what was added on the canvas: "
        f"{'; '.join(dropped)}. {_RENAMES} Ask me for the change instead (an agent "
        f"can be mounted in the net as a stock llm_agent subnet).",
    )


def _stopped_note(app_name: str, kept: list[str], dropped: list[str]) -> None:
    _set_note(
        app_name,
        f"Save stopped, nothing was written: the canvas added {'; '.join(dropped)} to "
        f"the Petri net in {', '.join(kept)}, and a net cannot hold that. Delete it on "
        f"the canvas (or send me any message: the canvas then reloads the net), and "
        f"Save again. {_RENAMES} Ask me for the change instead (an agent can be "
        f"mounted in the net as a stock llm_agent subnet).",
    )


def _conflict_note(app_name: str, files: tuple[str, ...]) -> None:
    _set_note(
        app_name,
        f"Save stopped, nothing was written: {', '.join(files)} changed in the app "
        f"after this builder draft was made, and the draft changed "
        f"{'it' if len(files) == 1 else 'them'} too. Saving would overwrite one of the "
        f"two. Tell me which to keep, or discard the draft (delete "
        f"`{app_name}/tmp/{app_name}`) and open the builder again.",
    )


def take_note(app_name: str) -> str | None:
    """What the canvas's last save on ``app_name`` dropped from a net, once; or None."""
    with _NOTES_LOCK:
        return _NOTES.pop(app_name, None)


@experimental
def install_builder_guard(
    app: FastAPI, agents_dir: str, *, on_saved: Callable[[str], None] | None = None
) -> bool:
    """Put the guarded save ahead of ADK's ``builder/save`` in ``app``.

    ``agents_dir`` is the one given to ``get_fast_api_app`` (resolved now, as
    ADK does: :func:`agents_base`, one app's folder included).
    ``on_saved(app_name)`` runs after
    every real save that succeeded (the server drops the app from its loader's
    cache and ADK's runner cache, so the graph panel and chat show what was
    saved, a net or not). Returns False when ``app`` has no ADK
    builder save route.
    """
    original: Save | None = None
    for route in app.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == SAVE_PATH
            and "POST" in (route.methods or ())
        ):
            original = route.endpoint
            break
    if original is None:
        return False
    adk_save: Save = original
    base = agents_base(agents_dir)

    async def builder_save(app_name: str, files: list[UploadFile], tmp: bool | None = False) -> Any:
        uploads = [(f.filename, await f.read(), f.headers) for f in files]

        async def adk(chosen: list[int] | None = None) -> Any:
            picked = uploads if chosen is None else [uploads[i] for i in chosen]
            return await adk_save(app_name=app_name, files=[_upload(*u) for u in picked], tmp=tmp)

        def prepare() -> tuple[Path, Path, list[str], list[int], list[str], bool, bool]:
            app_root = _app_root(base, app_name)
            tmp_root = _under(app_root, f"tmp/{app_name}")
            rels = [(_rel_path(app_name, n), c) for n, c, _ in uploads]
            kept, passed, dropped = _plan(app_root, tmp_root, rels)
            net = bool(kept) or involves_net(app_root, tmp_root)
            return app_root, tmp_root, kept, passed, dropped, net, is_helper_package(app_root)

        try:
            # File work (YAML, drafts) runs off the event loop: the canvas saves before
            # every message to the builder assistant.
            app_root, tmp_root, kept, passed, dropped, net, helper = await asyncio.to_thread(
                prepare
            )
        except (ValueError, OSError):
            return await adk()
        if helper:
            # "Create new app" with a helper package's name: never write into it.
            logger.warning(
                "builder save: %s is a helper package (no agent of its own); not written",
                app_name,
            )
            _set_note(
                app_name,
                f"`{app_name}` is already a Python package in this folder (helper code "
                f"other apps use), so nothing was saved there. Pick another app name.",
            )
            return bool(tmp)  # the canvas's Save then says "Something went wrong"
        added = [d for d in dropped if not d.startswith(_ECHOES)]
        if added and not tmp:
            # Saving the rest would close the builder as if the canvas's edit
            # were saved: answer false (the builder stays open) and say why.
            logger.warning(
                "builder save: %s not saved, the canvas added %s to a net", app_name, added
            )
            _stopped_note(app_name, kept, added)
            return False
        if dropped:
            _note(app_name, kept, dropped)
        if tmp:
            if kept:
                try:
                    await asyncio.to_thread(_ensure_tmp, app_root, tmp_root)
                    await asyncio.to_thread(reconcile, app_root, tmp_root)
                except OSError:
                    logger.exception("builder save: could not make the draft of %s", app_name)
                    return False  # as ADK's handler answers an OSError
            return await adk(passed)
        if net:
            try:
                conflicts = (await asyncio.to_thread(reconcile, app_root, tmp_root)).conflicts
            except OSError:
                logger.exception("builder save: could not check the draft of %s", app_name)
                return False
            if conflicts:
                logger.warning(
                    "builder save: %s changed in both %s and its draft; not saved",
                    list(conflicts),
                    app_name,
                )
                _conflict_note(app_name, conflicts)
                return False
        with without_marker(tmp_root):
            ok = await adk(passed)
        if ok and on_saved is not None:
            on_saved(app_name)
        return ok

    route = APIRoute(
        SAVE_PATH,
        builder_save,
        methods=["POST"],
        response_model_exclude_none=True,
        include_in_schema=False,
    )
    app.router.routes.insert(0, route)
    return True


__all__ = [
    "NET_CLASS_PREFIX",
    "NET_KEYS",
    "SAVE_PATH",
    "agents_base",
    "carries_net",
    "install_builder_guard",
    "is_net",
    "is_net_file",
    "is_net_text",
    "take_note",
]
