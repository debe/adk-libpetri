"""The public API, golden-checked: ``spec/api/python.txt``.

Every name reachable from a public package's ``__all__`` (and from the
``__all__`` of each module one of them exports), with its signature, the
members its class defines, and ``@experimental`` where it is marked. A change
to the public surface shows up as a diff of that file in review.

Regenerate after an intended change::

    PUBLICAPI_WRITE=1 pytest tests/test_public_api.py
"""

from __future__ import annotations

import collections.abc
import dataclasses
import difflib
import enum
import importlib
import inspect
import os
import re
import types
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from adk_libpetri import Place, is_experimental

WRITE = os.environ.get("PUBLICAPI_WRITE") == "1"
GOLDEN = Path(__file__).resolve().parents[2] / "spec" / "api" / "python.txt"
REGENERATE = "regenerate with PUBLICAPI_WRITE=1 pytest tests/test_public_api.py"

ROOT = "adk_libpetri"
MODULES = (
    ROOT,
    f"{ROOT}.bridge",
    f"{ROOT}.colours",
    f"{ROOT}.net",
    f"{ROOT}.runner",
    f"{ROOT}.subnet",
    f"{ROOT}.verify",
    f"{ROOT}.web",
    f"{ROOT}.workflow",
)
"""The public packages (and ``colours``, the boundary catalog every net uses)."""

_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")


def _type(t: Any) -> str:
    """A type as text that reads the same on every supported Python."""
    if isinstance(t, str):
        return t
    if t is inspect.Parameter.empty:
        return ""
    if t is None or t is type(None):
        return "None"
    if t is typing.Any:
        return "Any"
    if isinstance(t, typing.ForwardRef):
        return t.__forward_arg__
    origin, args = typing.get_origin(t), typing.get_args(t)
    if origin is typing.Union or origin is types.UnionType:
        return " | ".join(_type(a) for a in args)
    if origin is typing.Literal:
        return f"Literal[{', '.join(repr(a) for a in args)}]"
    if origin is not None:
        name = _type(origin)
        if origin is collections.abc.Callable and args and isinstance(args[0], list):
            return f"{name}[[{', '.join(_type(a) for a in args[0])}], {_type(args[1])}]"
        return f"{name}[{', '.join(_type(a) for a in args)}]" if args else name
    if isinstance(t, type):
        mod = t.__module__
        return t.__qualname__ if mod == "builtins" else f"{mod}.{t.__qualname__}"
    return _ADDRESS.sub("", repr(t))


def _value(v: Any) -> str:
    if isinstance(v, str | int | float | bool | None):
        return repr(v)
    if v is dataclasses.MISSING or type(v).__name__ == "_HAS_DEFAULT_FACTORY_CLASS":
        return "<factory>"
    if isinstance(v, Place):
        return f"<Place {v.name}: {v.type_name}>"
    if isinstance(v, enum.Enum):
        return f"{type(v).__name__}.{v.name}"
    if inspect.isfunction(v) or inspect.isclass(v):
        return f"{v.__module__}.{v.__qualname__}"
    if (
        isinstance(v, tuple | frozenset)
        and len(v) <= 8
        and all(isinstance(x, str | int) for x in v)
    ):
        return repr(v)
    return f"<{type(v).__name__}>"


def _signature(obj: Any) -> str:
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return "(...)"
    parts: list[str] = []
    seen_kw = False
    prior: inspect._ParameterKind | None = None
    for p in sig.parameters.values():
        if prior is inspect.Parameter.POSITIONAL_ONLY and p.kind != prior:
            parts.append("/")
        if p.kind is inspect.Parameter.KEYWORD_ONLY and not seen_kw:
            parts.append("*")
            seen_kw = True
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            seen_kw = True
        text = {
            inspect.Parameter.VAR_POSITIONAL: "*",
            inspect.Parameter.VAR_KEYWORD: "**",
        }.get(p.kind, "") + p.name
        ann = _type(p.annotation)
        if ann:
            text += f": {ann}"
        if p.default is not inspect.Parameter.empty:
            text += f" = {_value(p.default)}"
        parts.append(text)
        prior = p.kind
    if prior is inspect.Parameter.POSITIONAL_ONLY:
        parts.append("/")
    ret = _type(sig.return_annotation)
    return f"({', '.join(parts)})" + (f" -> {ret}" if ret else "")


def _flag(obj: Any) -> str:
    try:
        return "  @experimental" if is_experimental(obj) else ""
    except TypeError:
        return ""


def _own_classes(cls: type) -> list[type]:
    return [c for c in cls.__mro__ if c.__module__.startswith(ROOT)]


def _model_fields(cls: type[BaseModel]) -> list[str]:
    lines: list[str] = []
    for c in reversed(_own_classes(cls)):
        for name, ann in inspect.get_annotations(c).items():
            if name.startswith("_") or name not in cls.model_fields:
                continue
            lines.append(f"    field {name}: {_type(ann)}")
    return lines


def _members(cls: type) -> list[str]:
    lines: list[str] = []
    fields = {f.name for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set()
    done: set[str] = set()
    for c in _own_classes(cls):
        for name, raw in c.__dict__.items():
            if name.startswith(("_", "model_")) or name in done or name in fields:
                continue
            if isinstance(raw, enum.Enum):
                continue
            if issubclass(cls, BaseModel) and name in cls.model_fields:
                continue
            done.add(name)
            if isinstance(raw, property):
                ret = _type(inspect.signature(raw.fget).return_annotation) if raw.fget else ""
                lines.append(f"    property {name}" + (f": {ret}" if ret else ""))
            elif isinstance(raw, classmethod | staticmethod):
                kind = "classmethod" if isinstance(raw, classmethod) else "staticmethod"
                fn = getattr(cls, name)
                lines.append(f"    {kind} {name}{_signature(fn)}{_flag(raw.__func__)}")
            elif inspect.isfunction(raw):
                lines.append(f"    def {name}{_signature(raw)}{_flag(raw)}")
            elif inspect.isclass(raw):
                lines.append(f"    class {name}")
            else:
                lines.append(f"    {name} = {_value(raw)}")
    return sorted(lines, key=lambda line: line.split()[1].split("(")[0].split(":")[0])


def _describe(name: str, obj: Any) -> list[str]:
    if inspect.ismodule(obj):
        return [f"module {name}"]
    if inspect.isclass(obj):
        head = f"class {name}"
        if issubclass(obj, BaseModel):
            lines = [head + _flag(obj), *_model_fields(obj)]
        elif issubclass(obj, enum.Enum):
            lines = [head + _flag(obj), *(f"    {m.name} = {m.value!r}" for m in obj)]
        elif getattr(obj, "_is_protocol", False):
            lines = [f"{head} (Protocol){_flag(obj)}"]
        elif issubclass(obj, BaseException):
            bases = ", ".join(_type(b) for b in obj.__bases__)
            lines = [f"{head}({bases}){_flag(obj)}"]
        else:
            lines = [head + _signature(obj) + _flag(obj)]
        return lines + _members(obj)
    if inspect.isfunction(obj) or inspect.isbuiltin(obj):
        return [f"def {name}{_signature(obj)}{_flag(obj)}"]
    if callable(obj) and not isinstance(obj, type) and hasattr(obj, "__wrapped__"):
        return [f"def {name}{_signature(obj)}{_flag(obj)}"]
    if type(obj).__module__ in ("typing", "types", "collections.abc"):
        return [f"alias {name} = {_type(obj)}"]
    return [f"{name}: {_value(obj)}"]


def _public(module: types.ModuleType) -> list[str]:
    names = getattr(module, "__all__", None)
    if names is None:
        names = [n for n in vars(module) if not n.startswith("_")]
    return sorted(names)


def render() -> str:
    lines = [
        "# adk-libpetri Python public API, generated by python/tests/test_public_api.py.",
        f"# {REGENERATE}",
        "",
    ]
    first: dict[int, str] = {}
    queue = list(MODULES)
    visited: set[str] = set()
    while queue:
        modname = queue.pop(0)
        if modname in visited:
            continue
        visited.add(modname)
        module = importlib.import_module(modname)
        lines.append(f"[{modname}]")
        for name in _public(module):
            obj = getattr(module, name)
            path = f"{modname}.{name}"
            if inspect.ismodule(obj):
                lines.append(f"module {name}")
                if obj.__name__.startswith(ROOT) and obj.__name__ not in visited:
                    queue.append(obj.__name__)
                continue
            seen = first.get(id(obj))
            if seen is not None and (inspect.isclass(obj) or inspect.isfunction(obj)):
                lines.append(f"{name} = {seen}")
                continue
            if inspect.isclass(obj) or inspect.isfunction(obj):
                first[id(obj)] = path
            lines.extend(_describe(name, obj))
        lines.append("")
    return _ADDRESS.sub("", "\n".join(lines).rstrip("\n") + "\n")


def test_the_public_api_matches_its_golden_file() -> None:
    text = render()
    if WRITE:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8")
        return
    if not GOLDEN.exists():
        pytest.fail(f"{GOLDEN} is missing; {REGENERATE}")
    golden = GOLDEN.read_text(encoding="utf-8")
    if golden != text:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                text.splitlines(keepends=True),
                "spec/api/python.txt",
                "public API now",
                n=1,
            )
        )
        pytest.fail(f"the public API changed; {REGENERATE}\n{diff}")


def test_every_package_all_names_something_that_exists() -> None:
    for modname in MODULES:
        module = importlib.import_module(modname)
        missing = [n for n in getattr(module, "__all__", ()) if not hasattr(module, n)]
        assert not missing, f"{modname}.__all__ names {missing}"
