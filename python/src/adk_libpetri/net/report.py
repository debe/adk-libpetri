"""Load, check and verify a blueprint file, as data (``@experimental``).

What ``adk-libpetri check``/``verify`` print, the ``adk-libpetri web`` routes
return and the Petri builder assistant's tools hand the model all come from
here: :func:`load_net` builds the node the way ``adk web`` does,
:func:`check_file` and :func:`verify_file` turn the outcome into plain
dataclasses (``to_dict()`` for JSON).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import warnings
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from .._experimental import experimental


class LoadError(Exception):
    """A file that does not load; ``str()`` is the one-line message.

    ``path``, ``key_path`` and ``hint`` are set when a ``BlueprintError`` was
    the cause: the YAML key that is wrong and how to fix it.
    """

    def __init__(
        self,
        message: str,
        *,
        key_path: str | None = None,
        detail: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.key_path = key_path
        self.detail = detail
        self.hint = hint


@contextlib.contextmanager
def agents_dir_on_path(path: str) -> Iterator[None]:
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


def _cause(err: BaseException, cls: type[BaseException]) -> Any:
    """The ``cls`` exception behind ``err``, if ADK's loader wrapped one."""
    seen: set[int] = set()
    e: BaseException | None = err
    while e is not None and id(e) not in seen:
        if isinstance(e, cls):
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
        key = ".".join(loc[:2] if loc[:1] == ["nodes"] else loc[:1]) or "<top>"
        if key in lines:
            continue
        message = str(e.get("msg", "invalid")).removeprefix("Value error, ").rstrip(".")
        hint = _HINTS.get(loc[0] if loc else "")
        lines[key] = f"{path}: {key}: {message}" + (f". Fix: {hint}" if hint else "")
    shown = list(lines.values())[:_MAX_ERRORS]
    if len(lines) > _MAX_ERRORS:
        shown[-1] += f" (and {len(lines) - _MAX_ERRORS} more)"
    return "\nERROR ".join(shown)


_QUIETED = False
_QUIET_LOCK = threading.Lock()


def _quiet_adk_yaml_loader() -> None:
    """Silence ADK's announcement of its experimental YAML loader, which it makes on every load.

    One targeted filter, installed once: ``warnings.catch_warnings`` is not
    thread-safe (loads run on worker threads in ``adk-libpetri web``), and
    interleaved ones can leave a broad "ignore" behind for the whole process.
    """
    global _QUIETED
    with _QUIET_LOCK:
        if _QUIETED:
            return
        warnings.filterwarnings(
            "ignore",
            message=r"\[EXPERIMENTAL\] feature FeatureName\.AGENT_CONFIG",
            category=UserWarning,
        )
        _QUIETED = True


def load_node(path: str) -> Any:
    """Build the node in ``path`` through ADK's loader (any class), or raise :class:`LoadError`."""
    from pydantic import ValidationError

    from .blueprint import BlueprintError

    if not os.path.isfile(path):
        raise LoadError(f"{path}: no such file")
    from google.adk.agents.config_agent_utils import from_config

    _quiet_adk_yaml_loader()
    with agents_dir_on_path(path):
        try:
            return from_config(path)
        except Exception as err:
            bp = _cause(err, BlueprintError)
            if bp is not None:
                bp = bp.with_source(os.path.abspath(path))
                raise LoadError(str(bp), key_path=bp.path, detail=bp.message, hint=bp.hint) from err
            invalid = _cause(err, ValidationError)
            if invalid is not None:
                raise LoadError(_validation_lines(os.path.abspath(path), invalid)) from err
            raise LoadError(f"{path}: {type(err).__name__}: {err}") from err


def load_net(path: str) -> Any:
    """Build the ``PetriNet`` in ``path`` through ADK's loader, or raise :class:`LoadError`."""
    from .node import PetriNet

    node = load_node(path)
    if not isinstance(node, PetriNet):
        raise LoadError(
            f"{path}: defines a {type(node).__name__}, not a PetriNet. "
            "Fix: write agent_class: adk_libpetri.net.PetriNet",
            key_path="agent_class",
            hint="write agent_class: adk_libpetri.net.PetriNet",
        )
    return node


def children(node: Any) -> list[Any]:
    """The child ``PetriNet``s ``node`` mounts with ``subnets: {x: {net: name}}``, once each."""
    from .node import PetriNet

    by_name: dict[str, Any] = {}
    for item in node.nodes:
        for el in item if isinstance(item, list | tuple) else (item,):
            if isinstance(el, PetriNet):
                by_name[el.name] = el
    found: list[Any] = []
    for decl in node.subnets.values():
        name = decl.get("net") if isinstance(decl, dict) else None
        child = by_name.get(name) if isinstance(name, str) else None
        if child is not None and all(c is not child for c in found):
            found.append(child)
    return found


def nets_of(node: Any, *, recursive: bool) -> list[Any]:
    """``node``, then (``recursive``) every child it mounts, breadth first, once each."""
    nets = [node]
    if recursive:
        i = 0
        while i < len(nets):
            nets.extend(c for c in children(nets[i]) if all(c is not n for n in nets))
            i += 1
    return nets


# ----------------------------------------------------------------------------
#  Reports
# ----------------------------------------------------------------------------


@experimental
@dataclass(frozen=True)
class NetSummary:
    name: str
    places: int
    transitions: int
    subnets: int
    claims: int

    def line(self) -> str:
        return (
            f"PetriNet {self.name!r}: {self.places} places, {self.transitions} "
            f"transitions, {self.subnets} subnets, {self.claims} claims"
        )


def summary(node: Any) -> NetSummary:
    bp = node.blueprint
    return NetSummary(
        node.name,
        len(bp.spec.places),
        len(bp.spec.transitions),
        len(bp.mounts),
        len(bp.proof.claims),
    )


@experimental
@dataclass(frozen=True)
class CheckReport:
    """``ok``, with the net's size; or the error, with its YAML key path and fix."""

    path: str
    ok: bool
    net: NetSummary | None = None
    error: str | None = None
    key_path: str | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@experimental
def check_file(path: str) -> CheckReport:
    """Load ``path`` and build its net (no Z3)."""
    try:
        node = load_net(path)
    except LoadError as err:
        return CheckReport(path, False, error=str(err), key_path=err.key_path, hint=err.hint)
    return CheckReport(path, True, net=summary(node))


Verdict = Literal["proven", "violated", "unknown"]


@experimental
@dataclass(frozen=True)
class ClaimResult:
    """One claim's verdict; a violated one carries its counterexample."""

    net: str
    label: str
    kind: str
    verdict: Verdict
    scope: str = ""
    notes: tuple[str, ...] = ()
    fires: tuple[str, ...] = ()
    """The counterexample's firing sequence."""
    markings: tuple[dict[str, int], ...] = ()
    """The marking before the first firing, then after each."""
    report: str = ""
    reason: str = ""
    places: tuple[str, ...] = ()
    """The places the claim names (``place_bound``, ``unreachable``, ``mutual_exclusion``)."""
    bound: int = 0
    """A ``place_bound`` claim's bound."""


@experimental
@dataclass(frozen=True)
class VerifyReport:
    path: str
    nets: tuple[str, ...] = ()
    claims: tuple[ClaimResult, ...] = ()
    error: str | None = None
    key_path: str | None = None
    hint: str | None = None
    z3: bool = True
    """Whether a ``z3`` binary was found; without it some claims stay unknown."""
    no_claims: tuple[str, ...] = field(default=())
    """Nets checked that have no ``prove:`` claims."""
    graphs: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    """Each verified net's :class:`~adk_libpetri.net.graph.NetGraph`, by net name, to
    draw a counterexample on. Not part of :meth:`to_dict`."""

    @property
    def proven(self) -> int:
        return sum(c.verdict == "proven" for c in self.claims)

    @property
    def violated(self) -> int:
        return sum(c.verdict == "violated" for c in self.claims)

    @property
    def unknown(self) -> int:
        return sum(c.verdict == "unknown" for c in self.claims)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.violated and not self.unknown

    def to_dict(self) -> dict[str, Any]:
        d = asdict(replace(self, graphs={}))
        del d["graphs"]
        d.update(ok=self.ok, proven=self.proven, violated=self.violated, unknown=self.unknown)
        return d


def _claim(net: str, proof: Any, claim: Any = None) -> ClaimResult:
    r = proof.result
    verdict: Verdict = "proven" if proof.proven else "violated" if proof.violated else "unknown"
    fires: tuple[str, ...] = ()
    markings: tuple[dict[str, int], ...] = ()
    if verdict == "violated":
        fires = tuple(r.counterexample_transitions or ())
        markings = tuple(
            {str(p): int(n) for p, n in sorted(m.items())} for m in r.counterexample_trace or ()
        )
    return ClaimResult(
        net=net,
        label=proof.label,
        kind=proof.kind,
        verdict=verdict,
        scope=proof.scope,
        notes=tuple(proof.notes),
        fires=fires,
        markings=markings,
        report=str(r.report or "") if verdict == "violated" else "",
        reason=str(r.reason or r.report or "") if verdict == "unknown" else "",
        places=tuple(getattr(claim, "places", ())),
        bound=int(getattr(claim, "bound", 0)),
    )


def verify_net(node: Any, k: int | None = None) -> list[ClaimResult]:
    """Every ``prove:`` claim of one loaded net."""
    claims = node.blueprint.proof.claims
    return [_claim(node.name, p, c) for p, c in zip(node.verify(k), claims, strict=True)]


@experimental
def verify_file(path: str, k: int | None = None, *, recursive: bool = False) -> VerifyReport:
    """Run the ``prove:`` claims of ``path`` (and, ``recursive``, of each mounted child)."""
    import libpetri as lp

    try:
        node = load_net(path)
    except LoadError as err:
        return VerifyReport(path, error=str(err), key_path=err.key_path, hint=err.hint)
    nets = nets_of(node, recursive=recursive)
    claims: list[ClaimResult] = []
    empty: list[str] = []
    for net in nets:
        if not net.blueprint.proof.claims:
            empty.append(net.name)
        claims.extend(verify_net(net, k))
    return VerifyReport(
        path,
        nets=tuple(n.name for n in nets),
        claims=tuple(claims),
        z3=lp.z3_available(),
        no_claims=tuple(empty),
        graphs={n.name: n.graph for n in nets},
    )


__all__ = [
    "CheckReport",
    "ClaimResult",
    "LoadError",
    "NetSummary",
    "VerifyReport",
    "agents_dir_on_path",
    "check_file",
    "children",
    "load_net",
    "load_node",
    "nets_of",
    "summary",
    "verify_file",
    "verify_net",
]
