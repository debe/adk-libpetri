"""A folder's blueprints loaded from a copy, under a package name of its own.

The builder's draft (``<app>/tmp/<app>``) has its app's package name. Loaded
in place, its ``.agent.fn`` refs import as ``<app>.agent``: either the draft's
code takes the app's slot in ``sys.modules`` (and ADK's loader then serves
the draft as the app), or the app is already imported and the draft is
checked against the app's stale code. Swapping ``sys.modules`` entries
around the load only moves the race: ADK's loader imports on other threads.

:func:`staged` copies the folder to a temporary one named
``_petri_stage_<hex>``, a package nothing else imports. ADK resolves a
leading-dot ref against the YAML file's folder name, so the copy's refs
resolve in the copy; absolute refs to the app's own package (``<app>.agent.fn``)
are rewritten to the copy's in its YAML files. The copy's modules leave
``sys.modules`` afterwards, and :meth:`Stage.restore` maps the copy's names
back in any text shown to the user.
"""

from __future__ import annotations

import contextlib
import importlib
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

_SKIP = ("__pycache__", "tmp", ".venv", ".adk", ".git", "node_modules")
_YAML = (".yaml", ".yml")


@dataclass(frozen=True)
class Stage:
    source: Path
    """The folder copied."""
    root: Path
    """Its copy, ``<tempdir>/<package>``."""
    package: str

    def path(self, original: Path) -> Path:
        """Where ``original`` (a file under :attr:`source`) is in the copy."""
        return self.root / original.resolve().relative_to(self.source)

    def restore(self, text: str) -> str:
        """``text`` with the copy's paths and package name put back to the source's."""
        for root in {str(self.root.resolve()), str(self.root)}:
            text = text.replace(root, str(self.source))
        return re.sub(rf"\b{self.package}\b", self.source.name, text)


def _refs(text: str, package: str, into: str) -> str:
    """``package.x`` refs in YAML ``text`` renamed ``into.x`` (a ``package.yaml`` file is not)."""
    pattern = rf"(?<![\w./-]){re.escape(package)}\.(?!ya?ml\b)(?=[A-Za-z_])"
    return re.sub(pattern, into + ".", text)


@contextlib.contextmanager
def staged(folder: Path, files: Mapping[Path, str] | None = None) -> Iterator[Stage]:
    """A copy of ``folder`` (with ``files``, paths under it, written over) as a package of its own.

    The copy's modules are dropped from ``sys.modules``, and its folders' finders from
    ``sys.path_importer_cache``, when the block ends.
    """
    source = folder.resolve()
    package = f"_petri_stage_{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="petri-stage-") as tmp:
        root = Path(tmp) / package
        if source.is_dir():
            shutil.copytree(source, root, ignore=shutil.ignore_patterns(*_SKIP))
        else:
            root.mkdir()
        stage = Stage(source, root, package)
        for target, content in (files or {}).items():
            dest = stage.path(target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        for p in root.rglob("*"):
            if p.suffix in _YAML and p.is_file():
                text = p.read_text(encoding="utf-8")
                renamed = _refs(text, source.name, package)
                if renamed != text:
                    p.write_text(renamed, encoding="utf-8")
        importlib.invalidate_caches()
        try:
            yield stage
        finally:
            for k in [k for k in sys.modules if k == package or k.startswith(package + ".")]:
                sys.modules.pop(k, None)
            # The import system caches a finder per folder it looked in; the copy's go.
            for k in [k for k in sys.path_importer_cache if k.startswith(tmp)]:
                sys.path_importer_cache.pop(k, None)


__all__ = ["Stage", "staged"]
