"""``adk-libpetri``: check and verify net blueprints from the shell (``@experimental``).

::

    adk-libpetri check  agents/my_net/root_agent.yaml       # load and build, no Z3
    adk-libpetri verify agents/my_net/root_agent.yaml       # run the prove: claims
    adk-libpetri verify FILE --k 2 --recursive              # 2 arrivals; children too
    adk-libpetri verify FILE --json                         # verdicts as JSON
    adk-libpetri web agents/                                # ADK's dev UI, Petri-aware
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
import json
import sys
from collections.abc import Sequence
from importlib import resources
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from .net.report import ClaimResult

_EXIT_OK, _EXIT_FAIL = 0, 1


def _check(path: str, out: TextIO, *, as_json: bool = False) -> int:
    from .net.report import check_file

    report = check_file(path)
    if as_json:
        _dump(report.to_dict(), out)
    elif report.ok:
        assert report.net is not None
        print(f"OK {path}: {report.net.line()}", file=out)
    else:
        print(f"ERROR {report.error}", file=out)
    return _EXIT_OK if report.ok else _EXIT_FAIL


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def _counterexample(claim: ClaimResult) -> str:
    lines: list[str] = []
    if claim.fires:
        lines.append("  fires: " + " -> ".join(claim.fires))
    if claim.markings:
        lines.append("  markings:")
        for i, marking in enumerate(claim.markings):
            shown = ", ".join(f"{p}: {n}" for p, n in sorted(marking.items())) or "empty"
            lines.append(f"    {i}: {{{shown}}}")
    if claim.report:
        lines.append("  report:")
        lines.append(_indent(claim.report.rstrip()))
    return "\n".join(lines)


def _verify_one(node: Any, k: int | None, out: TextIO) -> tuple[int, int, int]:
    """Print every claim's verdict; ``(proven, violated, unknown)``."""
    from .net.report import verify_net

    source = node.blueprint.source or "<python>"
    print(f"verify {node.name} ({source})", file=out)
    if not node.blueprint.proof.claims:
        print("  no claims under prove:", file=out)
        return 0, 0, 0
    proven = violated = unknown = 0
    for claim in verify_net(node, k):
        print(f"{claim.verdict.upper():<9}{claim.label}  [{claim.kind}]", file=out)
        if claim.scope:
            print(f"  under: {claim.scope}", file=out)
        for note in claim.notes:
            print(f"  note: {note}", file=out)
        if claim.verdict == "proven":
            proven += 1
        elif claim.verdict == "violated":
            violated += 1
            print(_counterexample(claim), file=out)
        else:
            unknown += 1
            if claim.reason:
                print(_indent(claim.reason.rstrip(), "  "), file=out)
    return proven, violated, unknown


def _verify(
    path: str, k: int | None, recursive: bool, out: TextIO, *, as_json: bool = False
) -> int:
    from .net.report import LoadError, load_net, nets_of, verify_file

    if as_json:
        report = verify_file(path, k, recursive=recursive)
        _dump(report.to_dict(), out)
        return _EXIT_OK if report.ok else _EXIT_FAIL
    try:
        node = load_net(path)
    except LoadError as err:
        print(f"ERROR {err}", file=out)
        return _EXIT_FAIL
    totals = [0, 0, 0]
    for net in nets_of(node, recursive=recursive):
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


def _dump(data: Any, out: TextIO) -> None:
    json.dump(data, out, indent=2, default=str)
    out.write("\n")


def _web(args: argparse.Namespace) -> int:
    from .web.server import serve

    serve(args.agents_dir, host=args.host, port=args.port, reload_agents=args.reload)
    return _EXIT_OK


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
    c.add_argument("--json", action="store_true", help="print the result as JSON")
    v = sub.add_parser("verify", help="run the prove: claims; exit 1 on violated or unknown")
    v.add_argument("file")
    v.add_argument("--json", action="store_true", help="print the verdicts as JSON")
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
    w = sub.add_parser(
        "web",
        help="serve ADK's dev UI for Petri nets: the net in its graph panel, "
        "and a builder assistant that writes and proves blueprints",
    )
    w.add_argument("agents_dir", nargs="?", default=".", help="the folder holding the agents")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--reload", action="store_true", help="reload agents when their files change")
    sub.add_parser("guide", help="print the authoring guide (AUTHORING.md)")
    sub.add_parser("schema", help="print the JSON Schema of the YAML format")
    return p


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    """Run the CLI; returns the exit code (the console script exits with it)."""
    args = _parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    match args.command:
        case "check":
            return _check(args.file, stream, as_json=args.json)
        case "verify":
            return _verify(args.file, args.k, args.recursive, stream, as_json=args.json)
        case "web":
            return _web(args)
        case "guide":
            stream.write(_packaged("AUTHORING.md"))
            return _EXIT_OK
        case "schema":
            stream.write(_packaged("schema.json"))
            return _EXIT_OK
    return 2  # pragma: no cover - argparse rejects any other command


if __name__ == "__main__":
    sys.exit(main())
