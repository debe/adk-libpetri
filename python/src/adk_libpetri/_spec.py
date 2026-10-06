"""Net specification IR: the single definition every stock subnet is built from.

libpetri-py's ``NetBuilder.compose`` always prefixes the composed subnet's
names, and a built ``Net`` has no ``bind_actions``. The Java port composes
flat (``LlmAgent_BuildPrompt`` stays ``LlmAgent_BuildPrompt`` inside a host
net) and binds a whole composed net in one checked call. To keep both, every
stock subnet is declared once as a stateless :class:`NetSpec`: frozen
:class:`TransitionSpec` records naming arcs, priority and timing over typed
:class:`Place` constants. From that one definition come

* :meth:`NetSpec.build` -- a runnable ``libpetri.Net`` with actions bound,
  rejecting a missing, unknown or doubly bound key (the guard Java's
  ``SubnetActions.bindComposed`` gives against silent ``passthrough()``);
* :meth:`NetSpec.subnet_def` -- a ``libpetri.SubnetDef`` for
  ``verify_subnet`` and for callers composing with libpetri primitives;
* :meth:`NetSpec.fingerprint` -- canonical JSON for cross-language
  conformance against the Java nets.

Places fuse by name, as in libpetri. Unlike libpetri-py, :meth:`NetSpec.compose`
also checks the token type, restoring Java's ``(name, tokenType)`` identity.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar, Union, cast

import libpetri as lp

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)

_CHECK_TOKENS = os.environ.get("ADK_LIBPETRI_CHECK_TOKENS") == "1"


class _Unit:
    """Token type of a unit place (Java ``Place<Void>``); its tokens are ``None``."""

    def __repr__(self) -> str:
        return "Void"


VOID: Any = _Unit()


@dataclass(frozen=True, slots=True)
class Place(Generic[T_co]):
    """A typed place. Identity is ``(name, token_type)``; libpetri sees the name only."""

    name: str
    token_type: Any = VOID

    @property
    def is_unit(self) -> bool:
        return self.token_type is VOID

    @property
    def type_name(self) -> str:
        t = self.token_type
        return "Void" if t is VOID else getattr(t, "__name__", repr(t))

    def lp(self) -> lp.Place:
        return lp.Place(self.name)

    def check(self, value: Any) -> None:
        """Raise ``TypeError`` if ``value`` is not a token of this place."""
        if self.is_unit:
            if value is not None:
                raise TypeError(f"unit place '{self.name}' takes None, got {type(value).__name__}")
            return
        t = self.token_type
        if isinstance(t, type) and not isinstance(value, t):
            raise TypeError(
                f"place '{self.name}' takes {self.type_name}, got {type(value).__name__}"
            )

    def __repr__(self) -> str:
        return f"Place({self.name!r}, {self.type_name})"


def place(name: str, token_type: Any = VOID) -> Place[Any]:
    return Place(name, token_type)


# --------------------------------------------------------------------------
# Arcs
# --------------------------------------------------------------------------

InKind = Literal["one", "exactly", "all", "at_least"]


@dataclass(frozen=True, slots=True)
class In:
    kind: InKind
    place: Place[Any]
    count: int = 1

    def lp(self) -> lp.InputSpec:
        p = self.place.lp()
        match self.kind:
            case "one":
                return lp.one(p)
            case "exactly":
                return lp.exactly(self.count, p)
            case "all":
                return lp.all_tokens(p)
            case "at_least":
                return lp.at_least(self.count, p)


def one(p: Place[Any]) -> In:
    return In("one", p)


def exactly(n: int, p: Place[Any]) -> In:
    return In("exactly", p, n)


def all_tokens(p: Place[Any]) -> In:
    return In("all", p)


def at_least(n: int, p: Place[Any]) -> In:
    return In("at_least", p, n)


@dataclass(frozen=True, slots=True)
class OutPlace:
    place: Place[Any]


@dataclass(frozen=True, slots=True)
class And:
    children: tuple[Out, ...]


@dataclass(frozen=True, slots=True)
class Xor:
    children: tuple[Out, ...]


@dataclass(frozen=True, slots=True)
class Timeout:
    after_ms: int
    child: Out


@dataclass(frozen=True, slots=True)
class Forward:
    from_: Place[Any]
    to: Place[Any]


Out = Union[OutPlace, And, Xor, Timeout, Forward]  # noqa: UP007
OutLike = Union[Place[Any], OutPlace, And, Xor, Timeout, Forward]  # noqa: UP007


def _o(x: OutLike) -> Out:
    return OutPlace(x) if isinstance(x, Place) else x


def out(p: Place[Any]) -> OutPlace:
    return OutPlace(p)


def and_(*children: OutLike) -> And:
    return And(tuple(_o(c) for c in children))


def xor(*children: OutLike) -> Out:
    """XOR over ``children``; a single child degrades to that child (Java ``Out.xor`` needs 2)."""
    cs = tuple(_o(c) for c in children)
    return cs[0] if len(cs) == 1 else Xor(cs)


def timeout(after_ms: int, child: OutLike) -> Timeout:
    return Timeout(after_ms, _o(child))


def forward_input(from_: Place[Any], to: Place[Any]) -> Forward:
    return Forward(from_, to)


def _out_lp(o: Out) -> lp.OutputSpec:
    match o:
        case OutPlace(p):
            return lp.out(p.lp())
        case And(cs):
            return lp.and_(*(_out_lp(c) for c in cs))
        case Xor(cs):
            return lp.xor(*(_out_lp(c) for c in cs))
        case Timeout(ms, c):
            return lp.timeout(ms, _out_lp(c))
        case Forward(a, b):
            return lp.forward_input(a.lp(), b.lp())


def _out_places(o: Out) -> list[Place[Any]]:
    match o:
        case OutPlace(p):
            return [p]
        case And(cs) | Xor(cs):
            return [p for c in cs for p in _out_places(c)]
        case Timeout(_, c):
            return _out_places(c)
        case Forward(_, b):
            return [b]


def _out_fp(o: Out) -> Any:
    match o:
        case OutPlace(p):
            return p.name
        case And(cs):
            return {"and": [_out_fp(c) for c in cs]}
        case Xor(cs):
            return {"xor": [_out_fp(c) for c in cs]}
        case Timeout(ms, c):
            return {"timeout": ms, "child": _out_fp(c)}
        case Forward(a, b):
            return {"forward": [a.name, b.name]}


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Timing:
    kind: Literal["immediate", "deadline", "delayed", "window", "exact"]
    earliest_ms: int = 0
    latest_ms: int | None = None

    def lp(self) -> lp.Timing:
        match self.kind:
            case "immediate":
                return lp.immediate()
            case "deadline":
                return lp.deadline(cast(int, self.latest_ms))
            case "delayed":
                return lp.delayed(self.earliest_ms)
            case "window":
                return lp.window(self.earliest_ms, cast(int, self.latest_ms))
            case "exact":
                return lp.exact(self.earliest_ms)

    def fp(self) -> Any:
        if self.kind == "immediate":
            return None
        return {"kind": self.kind, "earliest_ms": self.earliest_ms, "latest_ms": self.latest_ms}


IMMEDIATE = Timing("immediate")


def deadline(by_ms: int) -> Timing:
    return Timing("deadline", 0, by_ms)


def delayed(after_ms: int) -> Timing:
    return Timing("delayed", after_ms)


def window(earliest_ms: int, latest_ms: int) -> Timing:
    return Timing("window", earliest_ms, latest_ms)


def exact(at_ms: int) -> Timing:
    return Timing("exact", at_ms, at_ms)


# --------------------------------------------------------------------------
# Transitions and nets
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Match:
    """ν-join correlation: one ``(place, key)`` per input place, optional relays."""

    keys: tuple[tuple[Place[Any], Callable[[Any], str]], ...]
    relay_to: tuple[tuple[Place[Any], Callable[[Any], str]], ...] = ()

    def lp(self) -> lp.MatchSpec:
        return lp.match_spec(
            [(p.lp(), k) for p, k in self.keys],
            [(p.lp(), k) for p, k in self.relay_to] or None,
        )


@dataclass(frozen=True, slots=True)
class TransitionSpec:
    name: str
    inputs: tuple[In, ...] = ()
    output: Out | None = None
    reads: tuple[Place[Any], ...] = ()
    inhibitors: tuple[Place[Any], ...] = ()
    resets: tuple[Place[Any], ...] = ()
    priority: int = 0
    timing: Timing = IMMEDIATE
    match: Match | None = None

    def places(self) -> list[Place[Any]]:
        ps = [i.place for i in self.inputs]
        if self.output is not None:
            ps += _out_places(self.output)
        ps += list(self.reads) + list(self.inhibitors) + list(self.resets)
        return ps

    def build(self, action: Action | None) -> lp.BuiltTransition:
        b = lp.Transition(self.name)
        for i in self.inputs:
            b = b.input(i.lp())
        if self.output is not None:
            b = b.output(_out_lp(self.output))
        for p in self.reads:
            b = b.read(lp.read(p.lp()))
        for p in self.inhibitors:
            b = b.inhibitor(lp.inhibitor(p.lp()))
        for p in self.resets:
            b = b.reset(lp.reset(p.lp()))
        if self.match is not None:
            b = b.match_spec(self.match.lp())
        if self.priority:
            b = b.priority(self.priority)
        if self.timing.kind != "immediate":
            b = b.timing(self.timing.lp())
        if action is not None:
            b = b.action(_wrap(action))
        return b.build()

    def fingerprint(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "inputs": sorted(
                ({"kind": i.kind, "place": i.place.name, "count": i.count} for i in self.inputs),
                key=lambda d: (d["place"], d["kind"]),
            ),
            "output": None if self.output is None else _out_fp(self.output),
            "reads": sorted(p.name for p in self.reads),
            "inhibitors": sorted(p.name for p in self.inhibitors),
            "resets": sorted(p.name for p in self.resets),
            "priority": self.priority,
            "timing": self.timing.fp(),
            "match": None if self.match is None else "present",
        }


PortDirection = Literal["in", "out", "inout"]


@dataclass(frozen=True, slots=True)
class Port:
    name: str
    direction: PortDirection
    place: Place[Any]


@dataclass(frozen=True)
class NetSpec:
    """A stateless net definition. Build it per session with :meth:`build`."""

    name: str
    transitions: tuple[TransitionSpec, ...]
    extra_places: tuple[Place[Any], ...] = ()
    ports: tuple[Port, ...] = ()
    membership: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)
    """``(transition, subnet)`` pairs: the subnet each composed transition came from."""
    _places: dict[str, Place[Any]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for t in self.transitions:
            if t.name in seen:
                raise ValueError(f"net '{self.name}': transition '{t.name}' declared twice")
            seen.add(t.name)
        places: dict[str, Place[Any]] = {}
        for p in [*self.extra_places, *(p for t in self.transitions for p in t.places())]:
            _fuse(self.name, places, p)
        for port in self.ports:
            _fuse(self.name, places, port.place)
        object.__setattr__(self, "_places", places)

    # -- structure ---------------------------------------------------------

    @property
    def places(self) -> tuple[Place[Any], ...]:
        return tuple(self._places.values())

    def place_named(self, name: str) -> Place[Any] | None:
        return self._places.get(name)

    @property
    def transition_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.transitions)

    def transition(self, name: str) -> TransitionSpec:
        for t in self.transitions:
            if t.name == name:
                return t
        raise KeyError(name)

    def has_place(self, p: Place[Any] | str) -> bool:
        return (p if isinstance(p, str) else p.name) in self._places

    @staticmethod
    def compose(
        name: str,
        *parts: NetSpec | TransitionSpec,
        extra_places: Iterable[Place[Any]] = (),
        ports: Iterable[Port] = (),
    ) -> NetSpec:
        """Flat composition: transitions are unioned, places fuse by ``(name, type)``.

        Sub-spec ports are dropped; ``ports`` declares the composite's interface.
        Each composed spec's transitions belong to that spec (nested subnets
        roll up into it, as Java's ``PetriNet.subnetOf`` reports); a bare
        transition belongs to no subnet.
        """
        ts: list[TransitionSpec] = []
        extra: list[Place[Any]] = list(extra_places)
        membership: list[tuple[str, str]] = []
        for part in parts:
            if isinstance(part, NetSpec):
                ts.extend(part.transitions)
                extra.extend(part.extra_places)
                membership.extend((t.name, part.name) for t in part.transitions)
            else:
                ts.append(part)
        return NetSpec(name, tuple(ts), tuple(extra), tuple(ports), tuple(membership))

    def with_transitions(self, *more: TransitionSpec, name: str | None = None) -> NetSpec:
        return NetSpec(
            name or self.name,
            (*self.transitions, *more),
            self.extra_places,
            self.ports,
            self.membership,
        )

    def subnet_of(self, transition: str) -> str | None:
        """The composed subnet ``transition`` came from, or ``None``."""
        return dict(self.membership).get(transition)

    # -- building ----------------------------------------------------------

    def check_bindings(self, actions: Mapping[str, Any]) -> None:
        declared = set(self.transition_names)
        missing = sorted(declared - actions.keys())
        extra = sorted(actions.keys() - declared)
        if missing or extra:
            msg = f"'{self.name}' action-binding mismatch:"
            if missing:
                msg += f" missing keys {missing}"
            if extra:
                msg += f" extra keys {extra}"
            raise ValueError(f"{msg}. Declared transitions: {sorted(declared)}.")

    def build(self, actions: Mapping[str, Action] | None = None) -> lp.BuiltNet:
        """Build a runnable net. ``actions`` must bind every transition exactly.

        ``None`` builds an unbound net for structural use (verification, DOT).
        """
        if actions is not None:
            self.check_bindings(actions)
        b = lp.Net(self.name)
        for p in self.places:
            b = b.place(p.lp())
        for t in self.transitions:
            b = b.transition(t.build(None if actions is None else actions[t.name]))
        return b.build()

    def subnet_def(self, actions: Mapping[str, Action] | None = None) -> lp.BuiltSubnetDef:
        """A libpetri ``SubnetDef`` with this spec's ports (MOD-051)."""
        b = lp.SubnetDef(self.name)
        for p in self.places:
            b = b.place(p.lp())
        for t in self.transitions:
            b = b.transition(t.build(None if actions is None else actions[t.name]))
        for port in self.ports:
            match port.direction:
                case "in":
                    b = b.input_port(port.name, port.place.lp())
                case "out":
                    b = b.output_port(port.name, port.place.lp())
                case "inout":
                    b = b.inout_port(port.name, port.place.lp())
        return b.build()

    def fingerprint(self) -> dict[str, Any]:
        """Canonical, order-independent structure for conformance checks."""
        return {
            "name": self.name,
            "places": sorted(
                ({"name": p.name, "type": p.type_name} for p in self.places),
                key=lambda d: d["name"],
            ),
            "transitions": sorted(
                (t.fingerprint() for t in self.transitions), key=lambda d: d["name"]
            ),
            "ports": sorted(
                (
                    {"name": p.name, "direction": p.direction, "place": p.place.name}
                    for p in self.ports
                ),
                key=lambda d: d["name"],
            ),
        }

    def fingerprint_json(self) -> str:
        return json.dumps(self.fingerprint(), indent=2, sort_keys=True) + "\n"


def _fuse(net: str, places: dict[str, Place[Any]], p: Place[Any]) -> None:
    prior = places.get(p.name)
    if prior is None:
        places[p.name] = p
    elif prior.token_type is not p.token_type and prior.token_type != p.token_type:
        raise TypeError(
            f"net '{net}': place '{p.name}' is declared as {prior.type_name} and as {p.type_name}"
        )


# --------------------------------------------------------------------------
# Action context
# --------------------------------------------------------------------------


class Ctx:
    """Typed view of ``libpetri.TransitionContext`` keyed by :class:`Place`."""

    __slots__ = ("_c",)

    def __init__(self, c: lp.TransitionContext) -> None:
        self._c = c

    @property
    def transition_name(self) -> str:
        return self._c.transition_name

    @property
    def raw(self) -> lp.TransitionContext:
        return self._c

    def fresh_name(self) -> str:
        return self._c.fresh_name()

    def input(self, p: Place[T]) -> T:
        return self._c.input(p.name)

    def inputs(self, p: Place[T]) -> list[T]:
        return self._c.inputs(p.name)

    def read(self, p: Place[T]) -> T:
        return self._c.read(p.name)

    def reads(self, p: Place[T]) -> list[T]:
        return self._c.reads(p.name)

    def output(self, p: Place[T], value: T) -> None:
        if _CHECK_TOKENS:
            p.check(value)
        self._c.output(p.name, value)

    def signal(self, p: Place[Any]) -> None:
        """Output one unit token (``None``) to ``p``."""
        self._c.output(p.name, None)

    def output_many(self, p: Place[T], values: Iterable[T]) -> None:
        vs = list(values)
        if _CHECK_TOKENS:
            for v in vs:
                p.check(v)
        self._c.output_many(p.name, vs)

    def flush(self) -> None:
        self._c.flush()


SyncAction = Callable[[Ctx], None]
AsyncAction = Callable[[Ctx], Awaitable[None]]
Action = Union[SyncAction, AsyncAction]  # noqa: UP007


def _wrap(action: Action) -> Callable[[lp.TransitionContext], Any]:
    if inspect.iscoroutinefunction(action):
        async_action = cast(AsyncAction, action)

        async def run_async(c: lp.TransitionContext) -> None:
            await async_action(Ctx(c))

        return run_async

    sync_action = cast(SyncAction, action)

    def run(c: lp.TransitionContext) -> Any:
        result = sync_action(Ctx(c))
        if inspect.isawaitable(result):
            raise TypeError(
                f"transition '{c.transition_name}': a sync action returned an awaitable; "
                "declare it `async def`"
            )
        return None

    return run


def lp_actions(actions: Mapping[str, Action]) -> dict[str, Callable[[lp.TransitionContext], Any]]:
    """Wrap typed actions for libpetri's own binding APIs.

    ``NetSpec.build`` and ``subnet_def`` wrap for you. Use this when binding
    through libpetri directly (``Instance.bind_actions``, ``SubnetDef.bind_actions``),
    which hands actions the raw ``TransitionContext``.
    """
    return {name: _wrap(a) for name, a in actions.items()}
