"""The builder's draft of an app (``<app>/tmp/<app>``), kept in step with the app.

ADK's dev UI makes the draft once, as a copy of the app, the first time the
builder opens on it, and never refreshes or drops it: a draft left by a closed
tab or an earlier server run is reused as it is. On Save, ADK copies every
draft file over the app. A file edited in the app after the draft was made
(in an editor, or through ``/petri``) would be put back to the draft's older
copy.

:func:`make_draft` makes the draft as ADK does (a copy of the app, ``tmp``
left out) and records a baseline: each copied file's digest, in
:data:`MARKER` inside the draft (it goes when the draft goes).
:func:`reconcile` compares each draft file with the app's and its baseline:

* only the app's changed: the draft file is refreshed from the app;
* only the draft's changed (the builder assistant wrote it): it stays, and
  Save ships it;
* deleted in the app, unchanged in the draft: deleted from the draft too
  (deleted in the app and changed in the draft: a conflict);
* both changed: a conflict. The draft stays as it is; the guarded Save
  refuses (:func:`~adk_libpetri.web.builder_guard.install_builder_guard`)
  and says which files.

A draft without a baseline (made by ADK's own route, or before) falls back to
file times: an app file newer than its draft copy refreshes it, unless the
draft copy was written in the draft (not copied there, which keeps the app's
older modification time): then neither can be told the newer work, and it is
a conflict.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MARKER = ".adk_libpetri_draft.json"
"""The draft's baseline: ``{"files": {relative path: sha256}}``, inside the draft."""

_SKIP_DIRS = frozenset({"__pycache__", ".git"})


def draft_root(app_root: Path, app_name: str) -> Path:
    """Where the builder keeps ``app_name``'s draft (ADK's ``_get_tmp_agent_root``)."""
    return app_root / "tmp" / app_name


def _files(root: Path) -> Iterator[tuple[str, Path]]:
    """Every file under ``root`` as ``(relative posix path, path)``; ``tmp/``, caches and
    the marker left out."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            rel = p.relative_to(root).as_posix()
            if p.is_dir():
                if p.name in _SKIP_DIRS or (d == root and p.name == "tmp"):
                    continue
                stack.append(p)
            elif p.is_file() and rel != MARKER:
                yield rel, p


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def baseline(tmp_root: Path) -> dict[str, str] | None:
    """The draft's baseline, or None (no marker, or one that does not parse)."""
    try:
        data = json.loads((tmp_root / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return None
    return {str(k): str(v) for k, v in files.items()}


def _write_baseline(tmp_root: Path, files: dict[str, str]) -> None:
    (tmp_root / MARKER).write_text(json.dumps({"files": files}, indent=1), encoding="utf-8")


def make_draft(app_root: Path, tmp_root: Path) -> bool:
    """The draft, made as ADK's ``GET builder?tmp=true`` makes it, with its baseline.

    Returns False when it existed already (it is left as it is).
    """
    if tmp_root.exists():
        return False
    tmp_root.mkdir(parents=True, exist_ok=True)
    for src in app_root.iterdir():
        if src.name == "tmp":
            continue
        if src.is_dir():
            shutil.copytree(src, tmp_root / src.name, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, tmp_root / src.name)
    _write_baseline(tmp_root, {rel: _digest(p.read_bytes()) for rel, p in _files(tmp_root)})
    return True


@dataclass(frozen=True)
class Reconciled:
    refreshed: tuple[str, ...] = ()
    """Draft files refreshed from the app (changed only there)."""
    conflicts: tuple[str, ...] = ()
    """Files changed in both since the draft was made; the draft's copy kept."""


def reconcile(app_root: Path, tmp_root: Path) -> Reconciled:
    """Refresh the draft's files the app changed and the draft did not; name the conflicts."""
    if not tmp_root.is_dir():
        return Reconciled()
    base = baseline(tmp_root)
    refreshed: list[str] = []
    conflicts: list[str] = []
    for rel, draft in list(_files(tmp_root)):
        app = app_root / rel
        if not app.is_file():
            if _deleted_in_app(rel, draft, base, conflicts):
                draft.unlink(missing_ok=True)
                refreshed.append(rel)
                if base is not None:
                    base.pop(rel, None)
            continue
        try:
            d, a = draft.read_bytes(), app.read_bytes()
        except OSError:
            continue
        if d == a:
            continue
        if base is None:
            stale = app.stat().st_mtime_ns > draft.stat().st_mtime_ns
            if stale and _written_in_draft(draft):
                conflicts.append(rel)  # work in the draft, and a newer app file
                continue
        else:
            was = base.get(rel)
            if was is not None and _digest(a) == was:
                continue  # only the draft changed: the builder's work
            stale = was is not None and _digest(d) == was
            if not stale:
                conflicts.append(rel)
                continue
        if stale:
            shutil.copy2(app, draft)
            refreshed.append(rel)
            if base is not None:
                base[rel] = _digest(a)
    if base is not None and refreshed:
        _write_baseline(tmp_root, base)
    if refreshed:
        logger.info("builder draft of %s: refreshed %s from the app", app_root.name, refreshed)
    return Reconciled(tuple(refreshed), tuple(conflicts))


def _deleted_in_app(
    rel: str, draft: Path, base: dict[str, str] | None, conflicts: list[str]
) -> bool:
    """Whether a draft file the app no longer has was deleted there since the draft was made.

    Only a baseline tells: a file it lists was copied from the app. If the
    draft's copy is unchanged, the deletion wins (ADK's Save would put the
    file back); if the draft changed it too, it is a conflict. A file the
    baseline does not list is the draft's own (the builder wrote it).
    """
    was = None if base is None else base.get(rel)
    if was is None:
        return False
    try:
        unchanged = _digest(draft.read_bytes()) == was
    except OSError:
        return False
    if not unchanged:
        conflicts.append(rel)
    return unchanged


def _written_in_draft(p: Path) -> bool:
    """Whether a draft file without a baseline was written in the draft, not copied there.

    ADK copies the app with ``shutil.copytree`` (``copy2``), which puts the
    source's older modification time back after writing, so a copied file's
    status changed later than its content; a file written in the draft
    changed both at once.
    """
    st = p.stat()
    return st.st_mtime_ns >= st.st_ctime_ns - 1_000_000


def draft_changed(app_root: Path, tmp_root: Path) -> bool:
    """Whether a draft file differs from the app's (or the app has no such file).

    ADK's own store (``.adk/``, the app's sessions) is not the user's work.
    """
    for rel, draft in _files(tmp_root):
        if rel.startswith(".adk/"):
            continue
        app = app_root / rel
        try:
            if not app.is_file() or app.read_bytes() != draft.read_bytes():
                return True
        except OSError:
            return True
    return False


@contextlib.contextmanager
def without_marker(tmp_root: Path) -> Iterator[None]:
    """The draft without its marker for the block (ADK copies every draft file into the
    app on Save); the marker is put back if the draft is still there afterwards."""
    marker = tmp_root / MARKER
    try:
        saved = marker.read_bytes()
    except OSError:
        saved = None
    if saved is not None:
        marker.unlink(missing_ok=True)
    try:
        yield
    finally:
        if saved is not None and tmp_root.is_dir() and not marker.exists():
            marker.write_bytes(saved)


__all__ = [
    "MARKER",
    "Reconciled",
    "baseline",
    "draft_changed",
    "draft_root",
    "make_draft",
    "reconcile",
    "without_marker",
]
