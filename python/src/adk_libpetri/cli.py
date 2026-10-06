"""``adk-libpetri``: check and verify net blueprints from the shell (``@experimental``).

::

    adk-libpetri check  agents/my_net/root_agent.yaml       # load and build, no Z3
    adk-libpetri verify agents/my_net/root_agent.yaml       # run the prove: claims
    adk-libpetri verify FILE --k 2 --recursive              # 2 arrivals; children too
    adk-libpetri guide                                      # print AUTHORING.md
    adk-libpetri schema                                     # print the JSON Schema

A file is loaded the way ``adk web`` and ``adk run`` load it: the directory
above the file's own directory goes on ``sys.path`` (``adk web`` runs from the
folder that holds the agents), then ADK's ``from_config`` builds the node, so
``.agent.fn`` refs and leading-dot types resolve as they will at run time.

Exit codes: 0 when the file loads (``check``) or every claim is proven
(``verify``); 1 on a load error or a claim that is violated or unknown; 2 on
bad usage.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import warnings
from collections.abc import Iterator, Sequence
from importlib import resources
from typing import Any, TextIO

_EXIT_OK, _EXIT_FAIL = 0, 1


class _LoadError(Exception):
    """A file that does not load; ``str()`` is the one-line message."""


@contextlib.contextmanager
def _agents_dir_on_path(path: str) -> Iterator[None]:
    """``adk web``'s ``sys.path``: the folder holding the agent's folder."""
    agents_dir = os.path.dirname(os.path.dirname(os.path.abspath(path)))
    added = agents_dir not in sys.path
    if added:
        sys.path.insert(0, agents_dir)
    try:
        yield
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(agents_dir)


def _blueprint_error(err: BaseException) -> Any:
    """The ``BlueprintError`` behind ``err``, if ADK's loader wrapped one."""
    from .net import BlueprintError

    seen: set[int] = set()
    e: BaseException | None = err
    while e is not None and id(e) not in seen:
        if isinstance(e, BlueprintError):
            return e
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return None


def _validation_error(err: BaseException) -> Any:
    """The pydantic ``ValidationError`` behind ``err``, if that is what failed."""
    from pydantic import ValidationError

    seen: set[int] = set()
    e: BaseException | None = err
    while e is not None and id(e) not in seen:
        if isinstance(e, ValidationError):
            return e
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return None


_HINTS = {
    "nodes": "write each entry as a list: - [file.yaml], - [.agent.fn] or - [{agent_class: ...}]",
    "name": "use a Python identifier: letters, digits and _, not starting with a digit",
}
_MAX_ERRORS = 3


def _validation_lines(path: str, err: Any) -> str:
    """A pydantic error as ``check``'s lines: ``<file>: <key path>: <message>. Fix: <hint>``.

    One line per field, at most a few: a ``nodes:`` entry fails every branch
    of ADK's edge union, and pydantic reports each.
    """
    lines: dict[str, str] = {}
    for e in err.errors():
        loc = [str(x) for x in e.get("loc", ())]
        field = ".".join(loc[:2] if loc[:1] == ["nodes"] else loc[:1]) or "<top>"
        if field in lines:
            continue
        message = str(e.get("msg", "invalid")).removeprefix("Value error, ").rstrip(".")
        hint = _HINTS.get(loc[0] if loc else "")
        lines[field] = f"{path}: {field}: {message}" + (f". Fix: {hint}" if hint else "")
    shown = list(lines.values())[:_MAX_ERRORS]
    if len(lines) > _MAX_ERRORS:
        shown[-1] += f" (and {len(lines) - _MAX_ERRORS} more)"
    return "\nERROR ".join(shown)


def _load(path: str) -> Any:
    """Build the ``PetriNet`` in ``path`` through ADK's loader, or raise :class:`_LoadError`."""
    if not os.path.isfile(path):
        raise _LoadError(f"{path}: no such file")
    from google.adk.agents.config_agent_utils import from_config

    from .net import PetriNet

    with _agents_dir_on_path(path), warnings.catch_warnings():
        # ADK announces its experimental YAML loader on every load.
        warnings.simplefilter("ignore", UserWarning)
        try:
            node = from_config(path)
        except Exception as err:
            bp = _blueprint_error(err)
            if bp is not None:
                raise _LoadError(str(bp.with_source(os.path.abspath(path)))) from err
            invalid = _validation_error(err)
            if invalid is not None:
                raise _LoadError(_validation_lines(os.path.abspath(path), invalid)) from err
            raise _LoadError(f"{path}: {type(err).__name__}: {err}") from err
    if not isinstance(node, PetriNet):
        raise _LoadError(
            f"{path}: defines a {type(node).__name__}, not a PetriNet. "
            "Fix: write agent_class: adk_libpetri.net.PetriNet"
        )
    return node


def _summary(node: Any) -> str:
    bp = node.blueprint
    spec = bp.spec
    return (
        f"PetriNet {node.name!r}: {len(spec.places)} places, {len(spec.transitions)} "
        f"transitions, {len(bp.mounts)} subnets, {len(bp.proof.claims)} claims"
    )


def _check(path: str, out: TextIO) -> int:
    try:
        node = _load(path)
    except _LoadError as err:
        print(f"ERROR {err}", file=out)
        return _EXIT_FAIL
    print(f"OK {path}: {_summary(node)}", file=out)
    return _EXIT_OK


def _children(node: Any) -> list[Any]:
    """The child ``PetriNet``s ``node`` mounts with ``subnets: {x: {net: name}}``, once each."""
    from .net import PetriNet

    by_name: dict[str, Any] = {}
    for item in node.nodes:
        for el in item if isinstance(item, list | tuple) else (item,):
            if isinstance(el, PetriNet):
                by_name[el.name] = el
    children: list[Any] = []
    for decl in node.subnets.values():
        name = decl.get("net") if isinstance(decl, dict) else None
        child = by_name.get(name) if isinstance(name, str) else None
        if child is not None and all(c is not child for c in children):
            children.append(child)
    return children


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def _counterexample(result: Any) -> str:
    lines: list[str] = []
    fired = result.counterexample_transitions
    if fired:
        lines.append("  fires: " + " -> ".join(fired))
    trace = result.counterexample_trace
    if trace:
        lines.append("  markings:")
        for i, marking in enumerate(trace):
            shown = ", ".join(f"{p}: {n}" for p, n in sorted(marking.items())) or "empty"
            lines.append(f"    {i}: {{{shown}}}")
    if result.report:
        lines.append("  report:")
        lines.append(_indent(result.report.rstrip()))
    return "\n".join(lines)


def _verify_one(node: Any, k: int | None, out: TextIO) -> tuple[int, int, int]:
    """Print every claim's verdict; ``(proven, violated, unknown)``."""
    source = node.blueprint.source or "<python>"
    print(f"verify {node.name} ({source})", file=out)
    if not node.blueprint.proof.claims:
        print("  no claims under prove:", file=out)
        return 0, 0, 0
    proven = violated = unknown = 0
    for proof in node.verify(k):
        verdict = proof.result.verdict
        print(f"{verdict.upper():<9}{proof.label}  [{proof.kind}]", file=out)
        if proof.scope:
            print(f"  under: {proof.scope}", file=out)
        for note in proof.notes:
            print(f"  note: {note}", file=out)
        if proof.proven:
            proven += 1
        elif proof.violated:
            violated += 1
            print(_counterexample(proof.result), file=out)
        else:
            unknown += 1
            reason = proof.result.reason or proof.result.report
            if reason:
                print(_indent(str(reason).rstrip(), "  "), file=out)
    return proven, violated, unknown


def _verify(path: str, k: int | None, recursive: bool, out: TextIO) -> int:
    try:
        node = _load(path)
    except _LoadError as err:
        print(f"ERROR {err}", file=out)
        return _EXIT_FAIL
    nets = [node]
    if recursive:
        i = 0
        while i < len(nets):
            nets.extend(c for c in _children(nets[i]) if all(c is not n for n in nets))
            i += 1
    totals = [0, 0, 0]
    for net in nets:
        for j, n in enumerate(_verify_one(net, k, out)):
            totals[j] += n
    proven, violated, unknown = totals
    print(f"{proven} proven, {violated} violated, {unknown} unknown", file=out)
    if unknown:
        import libpetri as lp

        if not lp.z3_available():
            print(
                "note: no z3 binary found (PATH or LIBPETRI_Z3); "
                "claims the state-space route cannot close stay unknown",
                file=out,
            )
    return _EXIT_FAIL if violated or unknown else _EXIT_OK


def _packaged(name: str) -> str:
    return resources.files("adk_libpetri.net").joinpath(name).read_text(encoding="utf-8")


def _positive(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError(f"expected an integer >= 1, got {n}")
    return n


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adk-libpetri",
        description="Check and verify Petri-net blueprints "
        "(agent_class: adk_libpetri.net.PetriNet).",
    )
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="load the YAML through ADK's loader and build the net (no Z3)")
    c.add_argument("file")
    v = sub.add_parser("verify", help="run the prove: claims; exit 1 on violated or unknown")
    v.add_argument("file")
    v.add_argument(
        "--k",
        type=_positive,
        default=None,
        help="replace every arrivals bound by K (user inputs the proofs assume)",
    )
    v.add_argument(
        "--recursive",
        action="store_true",
        help="also verify each mounted child PetriNet's own prove:, on the child alone",
    )
    sub.add_parser("guide", help="print the authoring guide (AUTHORING.md)")
    sub.add_parser("schema", help="print the JSON Schema of the YAML format")
    return p


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    """Run the CLI; returns the exit code (the console script exits with it)."""
    args = _parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    match args.command:
        case "check":
            return _check(args.file, stream)
        case "verify":
            return _verify(args.file, args.k, args.recursive, stream)
        case "guide":
            stream.write(_packaged("AUTHORING.md"))
            return _EXIT_OK
        case "schema":
            stream.write(_packaged("schema.json"))
            return _EXIT_OK
    return 2  # pragma: no cover - argparse rejects any other command


if __name__ == "__main__":
    sys.exit(main())
