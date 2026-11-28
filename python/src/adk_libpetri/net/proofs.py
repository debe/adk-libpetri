"""``prove:``: a blueprint's claims, checked by ``libpetri.verify`` (``@experimental``).

Each claim is one ``verify()`` call on the blueprint's structure (actions are
never run). Its options are the shared ``prove.options`` with the claim's own
``options`` laid over them key by key.

* **initial marking** -- the blueprint's seeds (``turnPermit`` included),
  then ``initial_marking`` place by place.
* **turns** -- with no ``environment``, the user's inputs come as the
  session's turns do, one after another: ``userIn`` starts with one token,
  and each next turn's input arrives only after an answer is on ``eventOut``
  (a verification-only transition ``turn:next`` moves that answer to
  ``turn:answered`` and puts the next input down). A safety claim covers one
  turn, ``deadlock_free`` and ``joined_or_dead_lettered`` (the claims about
  the net at rest) two, so a net whose first turn leaves it unable to
  answer the next is caught; ``k`` sets the number of turns for both. With
  ``turn: {release: place}`` the next input waits for the release instead
  (``turn:next`` moves it to ``turn:released``), and node runs of earlier
  turns may still be in flight (ADR 0010). Without a release, a ν relay node
  (:attr:`Names.relays`) is not split either, so ``turn:next`` does not wait
  for its runs (see :func:`turn_spec`).
* **environment** -- the places tokens arrive on from outside: the ``env:``
  places, and for a safety claim ``turnAbort`` too (the runner signals it on
  any failure or abort, at any time). By default each arrives at most ``k``
  times (exactly ``k`` for the claims about the net at rest). As written under
  ``environment``, which must list every ``env:`` place and then models
  ``userIn`` as one of them (no turn order); ``k`` replaces every
  ``arrivals`` bound. An environment place ``initial_marking`` seeds is
  closed: its tokens are the seeded ones. Neither of the claims about the
  net at rest lets ``turnAbort`` arrive: an abort could clear a turn that would otherwise
  hang, and the claim is about runs where nothing fails.
* **sinks** (deadlock freedom only) -- as written; else where the turn
  protocol leaves its tokens: ``eventOut``, ``turnPermit``, and each mounted
  subnet's unbound ones (a stock ``llm_agent``'s ``inst/turnPermit``, say).
  The ``turn.release`` place is a sink either way.
* **names** (a net with ``match:``) -- the ``mint:`` transitions are declared
  to libpetri as minting (``mint_transitions``; a split node, under ordered
  turns, mints at its ``complete:T:run`` step), and the places a name flows
  through toward a join's key (from a mint's target, a key or a relay target)
  are its ``carrier_places``, under ``fragment_mode="extended"`` (NU-010,
  NU-051). :func:`names` says which, and declares none when the net leaves
  the contract that rests on (a key reached by a token that does not hold
  its name in the field the key reads: another field, an emit's new Event, a
  node's NodeError, or a transition no blueprint plans; or a mint that takes
  a name and mints into a place that carries one). A single mint that marks
  a name-carrying place it does not mint into is left undeclared, with a note.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, assert_never

import libpetri as lp

from .. import colours as C
from .._experimental import experimental
from .._spec import (
    IMMEDIATE,
    VOID,
    And,
    NetSpec,
    OutPlace,
    Place,
    TransitionSpec,
    _out_places,
    one,
)
from .blueprint import (
    Blueprint,
    BlueprintError,
    Claim,
    ClaimKind,
    EnvMode,
    ProofOptions,
    _field_path,
)

TURN_NEXT = "turn:next"
TURNS_LEFT = "turn:remaining"
TURNS_ANSWERED = "turn:answered"
TURNS_QUIET = "turn:quiet"
TURNS_RELEASED = "turn:released"


def quiet_place(transition: str) -> str:
    """The place holding node transition ``transition``'s ``turn:quiet`` token."""
    return f"{TURNS_QUIET}:{transition}"


@experimental
@dataclass(frozen=True)
class NetProof:
    """One claim's verdict. Read :attr:`proven`: ``unknown`` is not a proof."""

    label: str
    result: lp.VerificationResult
    kind: ClaimKind
    scope: str = ""
    """What the proof assumed arrives from outside, e.g. ``userIn, turnAbort: at most
    1 each (1 turn)``: a claim proven over one turn says nothing about the second."""
    notes: tuple[str, ...] = ()
    """Assumptions the verdict rests on, and options that had no effect."""

    @property
    def proven(self) -> bool:
        return self.result.is_proven()

    @property
    def violated(self) -> bool:
        return self.result.is_violated()


def _structural(ctx: Any) -> None:
    raise AssertionError("verification never runs an action")


def _property(c: Claim) -> lp.SmtProperty:
    match c.kind:
        case "joined_or_dead_lettered":
            return lp.joined_or_dead_lettered(c.places[0])
        case "deadlock_free":
            return lp.deadlock_free()
        case "place_bound":
            return lp.place_bound(c.places[0], c.bound)
        case "unreachable":
            return lp.unreachable(list(c.places))
        case "mutual_exclusion":
            return lp.mutual_exclusion(list(c.places))


def _with_k(mode: EnvMode, k: int | None) -> EnvMode:
    kind, lo, hi = mode
    if kind != "arrivals" or k is None:
        return mode
    return (kind, k, k) if lo == hi else (kind, min(lo, k), k)


def _lp_mode(mode: EnvMode) -> lp.EnvironmentAnalysisMode:
    kind, lo, hi = mode
    if kind == "always":
        return lp.always_available()
    if kind == "bounded":
        return lp.bounded(hi)
    return lp.arrivals(lo, hi) if lo else lp.arrivals(hi)


def _describe(
    places: list[str], mode: EnvMode | None, turns: int | None, seeded: int, after: str
) -> str:
    """The run a claim covers, in words (``NetProof.scope``)."""
    parts: list[str] = []
    if turns == 1:
        parts.append("1 turn")
    elif turns is not None:
        parts.append(f"{turns} turns, each input after the previous {after}")
    elif seeded:
        parts.append(f"userIn: the {seeded} token(s) initial_marking seeds, no further turn")
    if places and mode is not None:
        kind, lo, hi = mode
        each = " each" if len(places) > 1 else ""
        if kind == "always":
            how = "always available"
        elif kind == "bounded":
            how = f"at most {hi} at a time{each}, refilled"
        elif lo == hi:
            how = f"exactly {hi} arrival(s){each}"
        elif lo == 0:
            how = f"at most {hi} arrival(s){each}"
        else:
            how = f"{lo} to {hi} arrivals{each}"
        text = f"{', '.join(places)}: {how}"
        if kind == "arrivals" and C.USER_IN.name in places:
            text += " (userIn in any order, not turn by turn)"
        parts.append(text)
    return "; ".join(parts) or "closed: the initial marking only, nothing arrives"


@dataclass(frozen=True)
class Names:
    """How a net with ``match:`` passes its names to libpetri's ν routes."""

    mints: tuple[str, ...]
    """The ``mint:`` transitions to declare as ``mint_transitions`` (as
    ``complete:T:run`` once :func:`turn_spec` splits ``T``): each writes a fresh
    name into every name-carrying place it marks."""
    carriers: tuple[str, ...]
    """Places a name flows through toward a join's key, from a mint's target, a key
    or a relay target (``carrier_places``)."""
    relays: tuple[str, ...]
    """``node:`` transitions whose run takes a name: a coloured consumer (it relays
    the name, under the NU-051 contract the notes state, or drains it) or a
    ``node:`` join (its relay targets are checked by NU-054). :func:`turn_spec`
    does not split them, since libpetri cannot follow a name through a split
    (VER-004)."""
    notes: tuple[str, ...] = ()
    declared: bool = True
    """``False`` when the net leaves the contract libpetri's ν route rests on (the
    notes say where): no name is passed to libpetri, so it cannot use that route."""


_Path = tuple[str, ...]
"""Where a token holds a name (``()``: the token is the name)."""


@dataclass(frozen=True)
class _Lost:
    """A token that reaches a key without the name its writer took: the cause, in
    words, for the place ``at`` it is put on."""

    at: str
    why: str


@dataclass(frozen=True)
class _Flow:
    """What a transition does with names, under the root's place names."""

    kind: str
    mints: Mapping[str, _Path]
    keys: Mapping[str, _Path]
    relays: Mapping[str, _Path]
    marks: frozenset[str]
    """The places its action's branches mark, the error branch left out."""
    errors: frozenset[str]
    """The places a node's error branch marks (with a ``NodeError``)."""


def _flows(bp: Blueprint) -> dict[str, _Flow]:
    """Each blueprint transition's :class:`_Flow`, mounted ones included."""
    out: dict[str, _Flow] = {}
    for t, plan in bp.plans.items():
        error = plan.branches[plan.error] if plan.error is not None else None
        marks = {p.name for b in plan.branches if b is not error for p in b.places}
        out[t] = _Flow(
            plan.kind,
            dict(plan.mints),
            dict(plan.keys),
            dict(plan.relays),
            frozenset(marks),
            frozenset(p.name for p in error.places) if error is not None else frozenset(),
        )
    for m in bp.mounts:
        if m.child is None:
            continue
        for t, f in _flows(m.child).items():
            out[m.transition(t)] = _Flow(
                f.kind,
                {m.places[p]: v for p, v in f.mints.items()},
                {m.places[p]: v for p, v in f.keys.items()},
                {m.places[p]: v for p, v in f.relays.items()},
                frozenset(m.places[p] for p in f.marks),
                frozenset(m.places[p] for p in f.errors),
            )
    return out


def _outs(t: TransitionSpec) -> set[str]:
    """The coloured places ``t`` marks (a unit token holds no name)."""
    if t.output is None:
        return set()
    return {p.name for p in _out_places(t.output) if not p.is_unit}


def _field(path: _Path) -> str:
    return ".".join(path) or "self"


_Reach = dict[_Path | _Lost, str]
"""Where the names reaching a place sit, each with where it came from."""


def _carried(t: str, f: _Flow | None, o: str, got: _Reach) -> _Reach:
    """What plain transition ``t`` puts on ``o`` of the names ``got`` it took."""
    if f is None:
        return {_Lost(o, f"{t} marks {o} with a token no blueprint says holds the name"): ""}
    if f.kind == "emit":
        return {_Lost(o, f"{t}'s emit puts a new Event on {o}, which holds no name it took"): ""}
    out: _Reach = dict(got) if f.kind == "move" or o in f.marks else {}
    if f.kind == "node" and o in f.errors:
        why = (
            f"{t}'s error branch marks {o} with a NodeError, which holds the node's name, "
            "not the name its run took"
        )
        out[_Lost(o, why)] = ""
    return out


def names(bp: Blueprint) -> Names | None:
    """``bp``'s :class:`Names`, or ``None`` when no transition has a ``match:``.

    A name flows from a mint's target, or a join's key or relay target, through
    the transitions that take it (a join ends it, unless it relays; a mint's
    minted places get a fresh name, so none passes through them). A place it
    flows into is a carrier when a key can be reached from there. A ``node:``
    transition that takes a name is assumed to put it into every name-carrying
    place it marks, in the same field (the relay contract, NU-051): ``move``
    forwards its token, a node function must copy the field itself.

    Names are declared only while the net keeps the contract libpetri's ν route
    rests on (NU-050, NU-051): every token that reaches a key holds its name in
    the field the key reads (not another field, an emit's new Event, a node's
    NodeError, or the token of a transition no blueprint plans), and no
    ``mint:`` transition other than a join takes a name and mints into a place
    that carries one. Else :attr:`Names.declared` is ``False`` and the notes
    say why. A mint that marks a name-carrying place it does not mint into is
    left out of :attr:`Names.mints` (a note says so); the other names stay
    declared. A mint that takes a name and whose fresh names reach no key is
    no ν mint (libpetri reads it as a forward of what it takes), and a join's
    fresh name may not reach a key: a note says so, and the join is not
    declared. A transition with no ``mint:`` that starts the tokens a key is
    reached by (it takes no token another transition puts down) gets a note
    naming the ``mint:`` it lacks. When its outputs cannot take a mint (``check``'s
    rule), the note names the one transition after it that takes them all on toward
    the key and can, or else says only where a ``mint:`` goes.
    """
    g = _graph(bp)
    if g is None:
        return None
    notes = _relay_notes(g)
    mints, mint_notes, mint_breaks = _mint_check(g)
    notes += mint_notes
    outside = sorted(p for p in bp.env if p in g.coloured)
    notes += [_outside_note(p) for p in outside]
    notes += _missing_mint_notes(g, outside)
    broken = _key_breaks(g) + mint_breaks
    relays = _relays(bp, g)
    if broken:
        notes.extend(
            f"{b}: names are not declared, so libpetri cannot use its ν route" for b in broken
        )
        return Names((), (), relays, tuple(notes), declared=False)
    matched = {t.name for t in g.joins}
    contract = [r for r in relays if r not in matched]
    if contract:
        node = "node" if len(contract) == 1 else "nodes"
        notes.append(
            f"relay contract (NU-051) assumed for {node} {', '.join(contract)}: a run puts the "
            "name it took into each place it marks that carries one, in the same field"
        )
    return Names(tuple(mints), tuple(sorted(g.carriers)), relays, tuple(notes))


@dataclass(frozen=True)
class _Graph:
    """How names flow through a net with ``match:``: what :func:`names` checks."""

    spec: NetSpec
    flows: dict[str, _Flow]
    joins: list[TransitionSpec]
    minted: dict[str, frozenset[str]]
    """Transition -> the places it marks with a fresh name."""
    plain: list[TransitionSpec]
    """The transitions without ``match:``."""
    ins: dict[str, set[str]]
    """Transition -> its input places that carry a value (unit places left out)."""
    passed: dict[str, set[str]]
    """The places a plain transition marks that do not get a fresh name there."""
    leads: set[str]
    """The places a key can be reached from."""
    reach: dict[str, _Reach]
    carriers: set[str]
    coloured: set[str]
    """Every place that holds a name: keys, relay targets and carriers."""
    key_of: dict[str, str]
    """Key place -> the join that keys it."""


def _nearest_key(g: _Graph, o: str) -> tuple[str, str, _Path | None]:
    """The first key lead ``o`` reaches over plain transitions (breadth first),
    the join that keys it, and the path that join reads (``None`` if unknown)."""
    frontier, seen = [o], {o}
    while frontier:
        nxt: list[str] = []
        for q in frontier:
            if q in g.key_of:
                j = g.key_of[q]
                fj = g.flows.get(j)
                return q, j, fj.keys.get(q) if fj is not None else None
            for s in g.plain:
                if q in g.ins[s.name]:
                    nxt += sorted(g.passed[s.name] & g.leads - seen)
                    seen |= g.passed[s.name] & g.leads
        frontier = nxt
    raise AssertionError(f"lead {o} reaches no key")


def _graph(bp: Blueprint) -> _Graph | None:
    spec = bp.spec
    joins = [t for t in spec.transitions if t.match is not None]
    if not joins:
        return None
    flows = _flows(bp)
    key_of = {p.name: t.name for t in joins if t.match for p, _ in t.match.keys}
    keyed = set(key_of)
    relayed = {p.name for t in joins if t.match for p, _ in t.match.relay_to}
    minted = {t: frozenset(f.mints) for t, f in flows.items() if f.mints}
    plain = [t for t in spec.transitions if t.match is None]
    ins = {
        t.name: {i.place.name for i in t.inputs if not i.place.is_unit} for t in spec.transitions
    }
    passed = {t.name: _outs(t) - minted.get(t.name, frozenset()) for t in plain}
    leads = _leads(keyed, plain, ins, passed)
    reach = _reach(flows, plain, ins, passed, leads)
    named = {p for p, r in reach.items() if r}
    carriers = (named & leads) - keyed - relayed
    return _Graph(
        spec=spec,
        flows=flows,
        joins=joins,
        minted=minted,
        plain=plain,
        ins=ins,
        passed=passed,
        leads=leads,
        reach=reach,
        carriers=carriers,
        coloured=keyed | relayed | carriers,
        key_of=key_of,
    )


def _leads(
    keyed: set[str],
    plain: list[TransitionSpec],
    ins: dict[str, set[str]],
    passed: dict[str, set[str]],
) -> set[str]:
    """The places a key can be reached from, over plain transitions."""
    leads = set(keyed)
    while True:
        more = {p for t in plain if passed[t.name] & leads for p in ins[t.name]} - leads
        if not more:
            return leads
        leads |= more


def _reach(
    flows: dict[str, _Flow],
    plain: list[TransitionSpec],
    ins: dict[str, set[str]],
    passed: dict[str, set[str]],
    leads: set[str],
) -> dict[str, _Reach]:
    """Where the names reaching each place sit, from the mints, relays and keys on."""
    reach: dict[str, _Reach] = {}
    for t, f in flows.items():
        for how, origin in (
            (f.mints, f"minted by {t}"),
            (f.relays, f"relayed by {t}"),
            (f.keys, f"as {t} keys it"),
        ):
            for p, path in how.items():
                reach.setdefault(p, {}).setdefault(path, origin)
    changed = True
    while changed:
        changed = False
        for t in plain:
            got: _Reach = {}
            for i in ins[t.name]:
                got.update(reach.get(i, {}))
            if not got:
                continue
            for o in passed[t.name] & leads:
                have = reach.setdefault(o, {})
                for path, origin in _carried(t.name, flows.get(t.name), o, got).items():
                    if path not in have:
                        have[path] = origin
                        changed = True
    return reach


def _relays(bp: Blueprint, g: _Graph) -> tuple[str, ...]:
    """The ``node:`` transitions that take a name-carrying place (:attr:`Names.relays`)."""
    nodes = set(bp.node_transitions)
    return tuple(
        t.name for t in g.spec.transitions if t.name in nodes and g.ins[t.name] & g.coloured
    )


def _key_breaks(g: _Graph) -> list[str]:
    """Each key reached by a token that does not hold its name in the field it reads."""
    broken: list[str] = []
    for t in g.joins:
        f = g.flows.get(t.name)
        for q, key in f.keys.items() if f is not None else ():
            for path, origin in g.reach.get(q, {}).items():
                if isinstance(path, _Lost):
                    onto = f", and that token reaches {q}" if path.at != q else ""
                    broken.append(f"{t.name} keys {q} by {_field(key)}, but {path.why}{onto}")
                elif path != key:
                    broken.append(
                        f"{t.name} keys {q} by {_field(key)}, but the name reaches it at "
                        f"{_field(path)} ({origin})"
                    )
    return broken


def _relay_notes(g: _Graph) -> list[str]:
    """A note for each join output that leads to a key without a ``relay:``."""
    notes: list[str] = []
    for t in g.joins:
        own: set[str] = {p.name for p, _ in t.match.relay_to} if t.match else set()
        for o in sorted(_outs(t) & g.leads - own - g.minted.get(t.name, frozenset())):
            q, j, key = _nearest_key(g, o)
            field = _field(key) if key is not None else "<field>"
            where = f"which is {j}'s key" if q == o else f"which reaches {j}'s key {q}"
            notes.append(
                f"{t.name} puts its token on {o}, {where}, but does not relay the name it "
                "joined there, so that name is not followed to the key and the claims stay "
                f"unknown; add relay: {{{o}: {field}}} to {t.name}"
            )
    return notes


_MintKind = Literal["mint", "note", "broken", "skip"]


def _mint_check(g: _Graph) -> tuple[list[str], list[str], list[str]]:
    """The declared mints, the notes on the undeclared ones, and the broken contracts."""
    mints: list[str] = []
    notes: list[str] = []
    broken: list[str] = []
    for t in g.spec.transitions:
        if t.name not in g.minted:
            continue
        kind, text = _mint_verdict(g, t)
        match kind:
            case "mint":
                mints.append(t.name)
            case "note":
                notes.append(text)
            case "broken":
                broken.append(text)
            case "skip":
                pass
            case _:
                assert_never(kind)
    return mints, notes, broken


def _mint_verdict(g: _Graph, t: TransitionSpec) -> tuple[_MintKind, str]:
    """How the mint check reads ``t``: ``"mint"`` (libpetri may be told of it),
    ``"note"`` or ``"broken"`` with the text, or ``"skip"`` (no ν mint)."""
    minted = g.minted[t.name]
    own: set[str] = {p.name for p, _ in t.match.relay_to} if t.match else set()
    if t.match is not None and (remint := sorted(minted & g.coloured - own)):
        shown = ", ".join(remint)
        verb = "carries" if len(remint) == 1 else "carry"
        return (
            "note",
            f"{t.name} is a join and mints into {shown}, which {verb} a name a join keys: "
            "libpetri gives a join no mint role and reads that output as a re-mint "
            "(NU-010, NU-054), so the claims stay unknown; mint the new name "
            "in a plain transition after the join",
        )
    taken = sorted(g.ins[t.name] & g.coloured) if t.match is None else []
    if taken and not minted & g.coloured:
        # Its fresh names reach no key: libpetri reads it as a forward or drain
        # of what it takes, so it is no ν mint.
        return ("skip", "")
    if taken:
        return (
            "broken",
            f"{t.name} takes the name on {', '.join(taken)} and mints a fresh one, so "
            "libpetri reads it as a relay of the name it took (NU-051)",
        )
    f = g.flows[t.name]
    rest = _outs(t) & g.coloured - minted - own
    fixable = sorted(rest & f.marks)
    fixed = sorted(rest - f.marks)  # the error branch's NodeError, or a timeout's token
    if not (fixable or fixed):
        return ("mint", "")
    parts: list[str] = []
    if fixable:
        shown = ", ".join(fixable)
        verb = "carries" if len(fixable) == 1 else "carry"
        parts.append(f"{shown} {verb} a name it does not mint; add {shown} to its mint:")
    if fixed:
        shown = ", ".join(fixed)
        parts.append(
            f"its error branch or timeout marks {shown}, whose token holds no name it "
            f"mints (a NodeError holds the node's name), so no join may key {shown} "
            "and no name may flow into it"
        )
    return (
        "note",
        f"{t.name} is not declared to mint, so the names it starts are not told "
        "apart and the claims stay unknown: " + "; ".join(parts),
    )


def _outside_note(p: str) -> str:
    # A name from outside: libpetri refuses every claim it would decide by name
    # (VER-006), and a mint: on the transition that takes it would not change that.
    # With budget_places a safety claim skips the name-aware graph and is decided
    # name-blind.
    return (
        f"{p} is an env: place, and a join keys on the name its tokens carry: verify "
        "cannot assume a name from outside is fresh (VER-006), so it decides no claim "
        "by name: deadlock_free and joined_or_dead_lettered stay unknown, and so does "
        "every other claim unless budget_places is declared, which decides a safety "
        "claim name-blind; mint the name in the net, and carry the outside value in "
        "another field"
    )


def _starts(g: _Graph, outside: list[str]) -> dict[str, set[str]]:
    """Each transition with no ``mint:`` that starts tokens a key is reached by
    (followed back from the key over plain transitions to one that takes no token
    another transition puts down), and the keys it reaches."""
    producers = {p for t in g.spec.transitions for p in _outs(t)}
    starts: dict[str, set[str]] = {}
    for q in sorted(g.key_of):
        frontier, seen = {q}, {q}
        while frontier:
            nxt: set[str] = set()
            for s in g.plain:
                if s.name in g.minted or not g.passed[s.name] & frontier:
                    continue
                if s.name in g.flows and not g.ins[s.name] & (producers | set(outside)):
                    starts.setdefault(s.name, set()).add(q)
                nxt |= g.ins[s.name] & g.leads - seen
            seen |= nxt
            frontier = nxt
    return starts


def _mint_line(g: _Graph, s: str) -> list[str] | None:
    """The ``mint:`` entries ``s`` needs, or ``None`` when one of its outputs
    toward a key cannot take a mint (``check``'s rule for a ``mint:``)."""
    paths: list[str] = []
    for o in sorted(g.passed[s] & g.leads):
        _, _, key = _nearest_key(g, o)
        if key is None:
            paths.append(f"{o}.<field>")
            continue
        place = g.spec.place_named(o)
        try:
            if place is not None:
                _field_path(s, place, _field(key), "mint target", mint=True)
        except BlueprintError:
            return None
        paths.append(o if key == () else f"{o}.{_field(key)}")
    return paths


def _missing_mint_fix(g: _Graph, start: str) -> str:
    """What to add for ``start``'s missing ``mint:``."""
    at, line, seen_at = start, _mint_line(g, start), {start}
    while line is None:
        # The tokens it starts cannot hold a name: the mint goes on the one
        # transition that takes all of them on toward the key.
        toward = g.passed[at] & g.leads
        onward = [
            s.name
            for s in g.plain
            if s.name in g.flows and s.name not in g.minted and s.name not in seen_at
            if toward <= g.ins[s.name] and g.passed[s.name] & g.leads
        ]
        if len(onward) != 1:
            break
        at = onward[0]
        seen_at.add(at)
        line = _mint_line(g, at)
    if line is None:
        return (
            "add mint: to the first transition that writes the name into a str place or a str field"
        )
    if at != start:
        return (
            f"{start}'s tokens cannot hold a name, so add mint: [{', '.join(line)}] "
            f"to {at}, which takes them on toward the keys"
        )
    return f"add mint: [{', '.join(line)}] to {start}"


def _missing_mint_notes(g: _Graph, outside: list[str]) -> list[str]:
    """A note for each transition that starts the tokens a key is reached by without a
    ``mint:``: its tokens hold no fresh name, and libpetri's reason names it with the
    nodes that only pass the name on."""
    return [
        f"{start} has no mint:, yet its tokens reach the join keys {', '.join(sorted(qs))}: "
        "they hold no fresh name, so the joins cannot tell them apart and the claims "
        "stay unknown (libpetri's reason may also name a node that only passes "
        f"the name on, which needs no mint:); {_missing_mint_fix(g, start)}"
        for start, qs in sorted(_starts(g, outside).items())
    ]


@dataclass(frozen=True)
class _Run:
    kwargs: dict[str, Any]
    scope: str
    notes: tuple[str, ...]
    turns: int | None
    """Turns in sequence (two or more run on :func:`turn_spec`), or ``None``."""


def turn_spec(bp: Blueprint) -> NetSpec | None:
    """``bp``'s net as a session runs it turn after turn.

    ``turn:next`` puts the next turn's input on ``userIn`` once an answer is on
    ``eventOut`` (it moves the answer to ``turn:answered``) and no node run is
    in flight: a turn's invocation lasts until its node runs have put their
    outputs down, and the next turn waits for it. To see the runs, each
    ``node:`` transition ``T`` is split in two: ``T`` consumes its inputs and
    marks ``inflight:T:run``, ``complete:T:run`` puts ``T``'s output down.

    The deposit has the shape of libpetri's own completion step (VER-004: named
    ``complete:<x>``, its one input ``inflight:<x>``), which the verifier never
    splits again. It is the run's completion already, and splitting it would
    add a step per node run and nothing else. ``T`` itself is split, since
    ``turn:next`` inhibits its output, so ``T`` takes its own
    ``turn:quiet:T`` token and its completion gives it back: ``turn:next``
    (which reads them all) cannot slip in between. One token per node keeps
    the runs of different nodes independent of each other, which the
    verifier's partial-order reduction (VER-024) needs; a shared token would
    make every node run depend on every other. Holding ``turn:quiet:T``
    orders two starts of ``T`` only, and their deposits are tested by
    ``turn:next`` alone, so no behaviour is lost.

    With a release place (``turn.release``, ADR 0010) ``turn:next`` takes the
    release instead and tests no node run: a turn's tail may still run when
    the next input comes, so nothing is split and no ``turn:quiet`` token is
    added (it would order a node's start in one turn against its start in the
    next, which the runtime allows).

    A node whose run carries a ν name (:attr:`Names.relays`) is not split:
    libpetri cannot follow a name through a split (VER-004), so the verifier
    fires it as one step. A run with no in-flight state needs no wait, and the
    turn order is unchanged: a whole run is a split run whose completion comes
    right after its start, and ``turn:next`` tests none of a relay's outputs,
    so moving the completion up past the steps in between changes no marking
    those steps see, except where a transition tests one of the relay's
    outputs with an inhibitor, reset or drain. NU-051 rules that out for its
    coloured outputs only. For any other output, libpetri's own in-flight
    split (VER-004) would split the relay, and it refuses because the relay
    writes a coloured place. The proof is then Unknown, never a Proven that
    the whole model alone would give.

    ``None`` when the net has no ``userIn`` environment place or no ``eventOut``.
    """
    spec = bp.spec
    user_in = spec.place_named(C.USER_IN.name)
    event_out = spec.place_named(C.EVENT_OUT.name)
    if user_in is None or event_out is None or C.USER_IN.name not in bp.env:
        return None
    left: Place[Any] = Place(TURNS_LEFT, VOID)
    if bp.release is not None:
        release = spec.place_named(bp.release)
        assert release is not None
        nxt = TransitionSpec(
            TURN_NEXT,
            (one(left), one(release)),
            And((OutPlace(user_in), OutPlace(Place(TURNS_RELEASED, VOID)))),
        )
        return NetSpec(
            spec.name, (*spec.transitions, nxt), spec.extra_places, spec.ports, spec.membership
        )
    answered: Place[Any] = Place(TURNS_ANSWERED, event_out.token_type)
    nodes = set(split_nodes(bp))
    ts: list[TransitionSpec] = []
    running: list[Place[Any]] = []
    quiets: list[Place[Any]] = []
    for t in spec.transitions:
        if t.name not in nodes:
            ts.append(t)
            continue
        run: Place[Any] = Place(f"inflight:{t.name}:run", VOID)
        quiet: Place[Any] = Place(quiet_place(t.name), VOID)
        running.append(run)
        quiets.append(quiet)
        ts.append(
            replace(
                t,
                inputs=(*t.inputs, one(quiet)),
                output=And((OutPlace(run), OutPlace(quiet))),
            )
        )
        ts.append(TransitionSpec(f"complete:{t.name}:run", (one(run),), t.output))
    ts.append(
        TransitionSpec(
            TURN_NEXT,
            (one(left), one(event_out)),
            And((OutPlace(user_in), OutPlace(answered))),
            reads=tuple(quiets),
            inhibitors=tuple(running),
        )
    )
    return NetSpec(spec.name, tuple(ts), spec.extra_places, spec.ports, spec.membership)


def split_nodes(bp: Blueprint) -> tuple[str, ...]:
    """The ``node:`` transitions :func:`turn_spec` splits (all but the ν relays)."""
    nu = names(bp)
    relays: set[str] = set(nu.relays) if nu is not None else set()
    return tuple(t for t in bp.node_transitions if t not in relays)


def _nu_kwargs(
    bp: Blueprint, o: ProofOptions, turns: int | None, injecting: bool
) -> tuple[dict[str, Any], list[str]]:
    """The ν keywords for ``libpetri.verify`` (:func:`names`), and their notes.

    ``injecting``: the claim is a quiescence claim under an ``always`` or
    ``bounded`` environment, where a budget cannot decide it.
    """
    nu = names(bp)
    if nu is None:
        if o.budget_places:
            return {}, ["budget_places has no effect: no transition joins by name"]
        return {}, []
    kwargs: dict[str, Any] = {}
    notes = list(nu.notes)
    if nu.declared:
        ordered = turns is not None and turns > 1 and bp.release is None
        split: set[str] = set(split_nodes(bp)) if ordered else set()
        # A split node's deposit is its complete:T:run step: that step mints.
        kwargs["mint_transitions"] = [f"complete:{t}:run" if t in split else t for t in nu.mints]
        kwargs["fragment_mode"] = "extended"
        kwargs["carrier_places"] = list(nu.carriers)
    if o.budget_places and injecting:
        # libpetri would hand the claim its name-aware graph left unknown to the
        # name-coloured encoding, which does not model injection: the flat
        # encoding's reason would replace the graph's.
        notes.append(
            "budget_places is not passed for this claim: under an always or bounded "
            "environment libpetri's name-coloured encoding does not apply to deadlock_free "
            "or joined_or_dead_lettered (VER-006), so a budget cannot decide it, and the "
            "name-aware graph's own reason is kept"
        )
    elif o.budget_places:
        budget, budget_notes = _budget(bp, o.budget_places, nu)
        notes.extend(budget_notes)
        if budget:
            kwargs["budget_places"] = budget
    return kwargs, notes


def _budget(bp: Blueprint, places: tuple[str, ...], nu: Names) -> tuple[list[str], list[str]]:
    """The ``budget_places`` to pass, and their notes.

    libpetri reads a transition that takes a budget token as a mint when it puts
    down a place that carries a name and takes none (NU-040 with NU-010): what it
    writes there is read as a fresh name. One that takes a name is read as a join
    or a relay (NU-051) whatever the budget. So the budget is passed only when
    every consumer that puts a name down without taking one is a declared mint.

    Nor is it passed where a join takes a key place with ``all`` or ``at_least``:
    with a budget libpetri keeps the name-blind verdict of a safety claim it cannot
    decide by name, and name-blind that arc drains every name's tokens there, not
    only the matched name's, so the run the executor takes is not among the ones
    it checks and a proven could be false.
    """
    if not nu.declared:
        return [], ["budget_places is not passed: the names are not declared (see the notes)"]
    spec = bp.spec
    drains = _drains(spec)
    if drains:
        return [], [
            "budget_places is not passed: "
            + ", ".join(drains)
            + " takes a matched key place with all or at_least; name-blind, libpetri drains "
            "every name's tokens there, not only the matched name's, so a proven with the "
            "budget could be false"
        ]
    joins = [t.match for t in spec.transitions if t.match is not None]
    coloured = {p.name for m in joins for p, _ in (*m.keys, *m.relay_to)} | set(nu.carriers)
    mints = set(nu.mints)
    ins = {t.name: {i.place.name for i in t.inputs} for t in spec.transitions}
    notes: list[str] = []
    copying: list[str] = []
    for place in places:
        consumers = [t for t in spec.transitions if place in ins[t.name]]
        if not consumers:
            notes.append(f"budget place {place} is consumed by no transition, so it bounds nothing")
        copying += [
            t.name
            for t in consumers
            if t.name not in mints and _outs(t) & coloured and not ins[t.name] & coloured
        ]
    if copying:
        shown = ", ".join(sorted(set(copying)))
        notes.append(
            "budget_places is not passed: libpetri reads a transition that takes a budget "
            "token, and puts down names without taking one, as minting them (NU-040), but "
            f"{shown} does not mint them; take the budget in the transition that mints, or "
            "add the names to its mint:"
        )
        return [], notes
    return list(places), notes


def _drains(spec: NetSpec) -> list[str]:
    """The joins that take a key place with ``all`` or ``at_least``."""
    return sorted(
        t.name
        for t in spec.transitions
        if t.match is not None
        and any(
            i.kind in ("all", "at_least") and i.place.name in {p.name for p, _ in t.match.keys}
            for i in t.inputs
        )
    )


def _run(bp: Blueprint, claim: Claim, k: int | None) -> _Run:
    o: ProofOptions = claim.options.over(bp.proof.options)
    deadlock = claim.kind == "deadlock_free"
    quiescent = deadlock or claim.kind == "joined_or_dead_lettered"
    marking = bp.initial_counts()
    if o.initial_marking is not None:
        marking.update(o.initial_marking)
    kwargs: dict[str, Any] = {"initial_marking": {p: n for p, n in marking.items() if n}}
    notes: list[str] = []
    seeded = set(o.initial_marking or ())
    mode: EnvMode | None
    turns: int | None = None
    if o.environment is not None:
        places = list(o.environment)
        mode = o.mode
    else:
        places = [p for p in bp.env if p not in seeded]
        n = k if k is not None else (2 if quiescent else 1)
        mode = ("arrivals", n, n) if quiescent else ("arrivals", 0, n)
        if C.USER_IN.name in places and turn_spec(bp) is not None:
            # One turn needs no turn:next: the net with one userIn token.
            places.remove(C.USER_IN.name)
            turns = n
            m = kwargs["initial_marking"]
            m[C.USER_IN.name] = m.get(C.USER_IN.name, 0) + 1
            if n > 1:
                m[TURNS_LEFT] = n - 1
                if bp.release is None:
                    m.update((quiet_place(t), 1) for t in split_nodes(bp))
    abort = C.TURN_ABORT.name
    if not quiescent and bp.spec.has_place(C.TURN_ABORT) and abort not in places + list(seeded):
        places.append(abort)
    if not places:
        mode = None
    if k is not None:
        if turns is None and (mode is None or mode[0] != "arrivals"):
            notes.append(f"k={k} has no effect: no environment place takes arrivals here")
        if C.USER_IN.name in seeded:
            notes.append(f"k={k} does not change userIn: initial_marking seeds it")
    if mode is not None:
        mode = _with_k(mode, k)
        kwargs["environment_places"] = places
        kwargs["environment_mode"] = _lp_mode(mode)
    if deadlock:
        sinks = list(o.sinks if o.sinks is not None else bp.rest)
        if bp.release is not None and bp.release not in sinks:
            # Nothing consumes the release place: its tokens always rest there.
            sinks.append(bp.release)
        if turns is not None and turns > 1:
            # An answer the next turn's input followed. turn:remaining is no sink:
            # a turn that never answers keeps the next one from coming.
            if bp.release is None:
                sinks += [TURNS_ANSWERED, *(quiet_place(t) for t in split_nodes(bp))]
            else:
                # The answers stay on eventOut: turn:next takes the release.
                sinks += [TURNS_RELEASED]
        kwargs["sink_places"] = sinks
        if o.sinks_when is not None:
            kwargs["sink_places_when"] = {m: list(ps) for m, ps in o.sinks_when.items()}
    injecting = quiescent and mode is not None and mode[0] != "arrivals"
    nu_kwargs, nu_notes = _nu_kwargs(bp, o, turns, injecting)
    kwargs.update(nu_kwargs)
    notes.extend(nu_notes)
    if o.assume_atomic_firing is not None:
        kwargs["assume_atomic_firing"] = o.assume_atomic_firing
    if o.assume_atomic_firing:
        notes.append(
            "assumes every firing is one step"
            + (", node runs included (assume_atomic_nodes)" if bp.asynchronous else "")
        )
    seeded_inputs = kwargs["initial_marking"].get(C.USER_IN.name, 0) if turns is None else 0
    after = "answer" if bp.release is None else "release"
    scope = _describe(places, mode, turns, seeded_inputs, after)
    return _Run(kwargs, scope, tuple(notes), turns)


def claim_options(bp: Blueprint, claim: Claim, k: int | None = None) -> dict[str, Any]:
    """The keywords ``claim``'s first ``libpetri.verify`` call gets, on :func:`claim_spec`.

    A budgeted claim that libpetri finds violated without following names is
    decided again without ``budget_places`` (:func:`_verify`).
    """
    return _run(bp, claim, k).kwargs


def claim_spec(bp: Blueprint, claim: Claim, k: int | None = None) -> NetSpec:
    """The net ``claim`` is checked on: :func:`turn_spec` when its turns are
    ordered, with every ``delayed`` timing read as immediate (:func:`untimed`)."""
    turns = _run(bp, claim, k).turns
    if turns is not None and turns > 1:
        spec = turn_spec(bp)
        assert spec is not None
        return untimed(spec)
    return untimed(bp.spec)


def untimed(spec: NetSpec) -> NetSpec:
    """``spec`` with ``delayed`` transitions immediate: the same claim, decided faster.

    The verdicts are about the untimed net either way (libpetri, VER-004), and
    a delay is only a lower bound, so the reachable markings are the same. As
    an immediate net the state space can be enumerated, where a timed one goes
    to Z3. A source transition (no input) keeps its delay; deadlines, windows
    and exact timings are kept for their reaping semantics.
    """
    ts = tuple(
        replace(t, timing=IMMEDIATE) if t.timing.kind == "delayed" and t.inputs else t
        for t in spec.transitions
    )
    return NetSpec(spec.name, ts, spec.extra_places, spec.ports, spec.membership)


def _built(spec: NetSpec) -> lp.BuiltNet:
    # Structure only: verify() never runs an action, but an unbound transition
    # with outputs would carry libpetri's passthrough(), which it rejects.
    return spec.build({t: _structural for t in spec.transition_names})


@experimental
def verify_blueprint(bp: Blueprint, k: int | None = None, **verify_options: Any) -> list[NetProof]:
    """One ``verify()`` per ``prove:`` claim, in order (see the module docstring)."""
    nets: dict[bool, lp.BuiltNet] = {}
    proofs: list[NetProof] = []
    for c in bp.proof.claims:
        run = _run(bp, c, k)
        ordered = run.turns is not None and run.turns > 1
        if ordered not in nets:
            spec = turn_spec(bp) if ordered else bp.spec
            assert spec is not None
            nets[ordered] = _built(untimed(spec))
        result, notes = _verify(nets[ordered], c, run, verify_options)
        proofs.append(NetProof(c.label, result, c.kind, run.scope, notes))
    return proofs


_NAME_BLIND = (
    "budget_places is not used for this verdict: with it, libpetri decided the claim "
    "name-blind (under an always or bounded environment, or where the name-coloured "
    "encoding does not apply), where a violated run may join two different names (NU-050); "
    "a proven with the budget would stand (budget_places is not passed where a join takes "
    "a key place with all or at_least)"
)


def _verify(
    net: lp.BuiltNet, c: Claim, run: _Run, verify_options: dict[str, Any]
) -> tuple[lp.VerificationResult, tuple[str, ...]]:
    """``c``'s verdict, and its notes.

    With ``budget_places`` libpetri keeps a safety claim off its name-aware graph.
    Where its name-coloured encoding does not apply either (an environment that
    injects, VER-006), it decides the claim name-blind: a proven is sound
    (:func:`_budget` keeps the budget out where a join takes a key place with
    ``all`` or ``at_least``), a violated may join two different names. Such a
    violated is decided again without the budget, by name or as unknown.
    """
    result = lp.verify(net, _property(c), **run.kwargs, **verify_options)
    if "budget_places" not in run.kwargs or not result.is_violated() or _by_name(result):
        return result, run.notes
    kwargs = {key: v for key, v in run.kwargs.items() if key != "budget_places"}
    return lp.verify(net, _property(c), **kwargs, **verify_options), (*run.notes, _NAME_BLIND)


def _by_name(result: lp.VerificationResult) -> bool:
    """Whether libpetri followed names to ``result``: its name-aware graph (Route B)
    or its name-coloured encoding (Route A, NU-053)."""
    return result.route == "nu-scg" or "name-colouring over k colour slots" in result.report
