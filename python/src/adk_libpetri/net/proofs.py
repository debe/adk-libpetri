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
  turn, ``deadlock_free`` two, so a net whose first turn leaves it unable to
  answer the next is caught; ``k`` sets the number of turns for both.
* **environment** -- the places tokens arrive on from outside: the ``env:``
  places, and for a safety claim ``turnAbort`` too (the runner signals it on
  any failure or abort, at any time). By default each arrives at most ``k``
  times (exactly ``k`` for ``deadlock_free``). As written under
  ``environment``, which must list every ``env:`` place and then models
  ``userIn`` as one of them (no turn order); ``k`` replaces every
  ``arrivals`` bound. An environment place ``initial_marking`` seeds is
  closed: its tokens are the seeded ones. ``deadlock_free`` never lets
  ``turnAbort`` arrive: an abort could clear a turn that would otherwise
  hang, and the claim is about runs where nothing fails.
* **sinks** (deadlock freedom only) -- as written; else where the turn
  protocol leaves its tokens: ``eventOut``, ``turnPermit``, and each mounted
  subnet's unbound ones (a stock ``llm_agent``'s ``inst/turnPermit``, say).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

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
    one,
)
from .blueprint import Blueprint, Claim, ClaimKind, EnvMode, ProofOptions

TURN_NEXT = "turn:next"
TURNS_LEFT = "turn:remaining"
TURNS_ANSWERED = "turn:answered"
TURNS_QUIET = "turn:quiet"


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


def _describe(places: list[str], mode: EnvMode | None, turns: int | None, seeded: int) -> str:
    """The run a claim covers, in words (``NetProof.scope``)."""
    parts: list[str] = []
    if turns == 1:
        parts.append("1 turn")
    elif turns is not None:
        parts.append(f"{turns} turns, each input after the previous answer")
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

    ``None`` when the net has no ``userIn`` environment place or no ``eventOut``.
    """
    spec = bp.spec
    user_in = spec.place_named(C.USER_IN.name)
    event_out = spec.place_named(C.EVENT_OUT.name)
    if user_in is None or event_out is None or C.USER_IN.name not in bp.env:
        return None
    left: Place[Any] = Place(TURNS_LEFT, VOID)
    answered: Place[Any] = Place(TURNS_ANSWERED, event_out.token_type)
    nodes = set(bp.node_transitions)
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


def _run(bp: Blueprint, claim: Claim, k: int | None) -> _Run:
    o: ProofOptions = claim.options.over(bp.proof.options)
    deadlock = claim.kind == "deadlock_free"
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
        n = k if k is not None else (2 if deadlock else 1)
        mode = ("arrivals", n, n) if deadlock else ("arrivals", 0, n)
        if C.USER_IN.name in places and turn_spec(bp) is not None:
            # One turn needs no turn:next: the net with one userIn token.
            places.remove(C.USER_IN.name)
            turns = n
            m = kwargs["initial_marking"]
            m[C.USER_IN.name] = m.get(C.USER_IN.name, 0) + 1
            if n > 1:
                m[TURNS_LEFT] = n - 1
                m.update((quiet_place(t), 1) for t in bp.node_transitions)
    abort = C.TURN_ABORT.name
    if not deadlock and bp.spec.has_place(C.TURN_ABORT) and abort not in places + list(seeded):
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
        if turns is not None and turns > 1:
            # An answer the next turn's input followed. turn:remaining is no sink:
            # a turn that never answers keeps the next one from coming.
            sinks += [TURNS_ANSWERED, *(quiet_place(t) for t in bp.node_transitions)]
        kwargs["sink_places"] = sinks
        if o.sinks_when is not None:
            kwargs["sink_places_when"] = {m: list(ps) for m, ps in o.sinks_when.items()}
    if o.assume_atomic_firing is not None:
        kwargs["assume_atomic_firing"] = o.assume_atomic_firing
    if o.assume_atomic_firing:
        notes.append(
            "assumes every firing is one step"
            + (", node runs included (assume_atomic_nodes)" if bp.asynchronous else "")
        )
    seeded_inputs = kwargs["initial_marking"].get(C.USER_IN.name, 0) if turns is None else 0
    scope = _describe(places, mode, turns, seeded_inputs)
    return _Run(kwargs, scope, tuple(notes), turns)


def claim_options(bp: Blueprint, claim: Claim, k: int | None = None) -> dict[str, Any]:
    """The ``libpetri.verify`` keywords ``claim`` is checked under, on :func:`claim_spec`."""
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
        result = lp.verify(nets[ordered], _property(c), **run.kwargs, **verify_options)
        proofs.append(NetProof(c.label, result, c.kind, run.scope, run.notes))
    return proofs
