# ADR 0010: Turn release, the next turn before the last one's tail

- **Status:** Accepted
- **Date:** 2026-10-08
- **Scope:** Python, `adk_libpetri.net` (`PetriNet`, `@experimental`).
  google-adk `~=2.11.0`, libpetri-py `>=7.3,<8`. Java and `from_workflow`
  are unchanged.

## Context

A `PetriNet` session serves one turn at a time (ADR 0008, "The turn
protocol"). A turn's invocation lasts from its `userIn` inject until its
first `eventOut` token *and* every node run it started has finished;
`NetScope.turn_slot` makes a second invocation of the session wait for that.

Java Marvin's text net does not work that way. Its `CloseTurn` gives the turn
budget back once guard, intent and response are done. The delivery tail
(output guard, the `assistant.message` frame, quick responses, the
finalising frame) runs outside the budget, so a queued user message starts
the next turn while the previous answer is still being guarded. marvin_adk,
the Python port on this library, needs that overlap to conform.

Nothing in the library gives it today:

- the tail's node runs hold the invocation open (`NetScope.drain`), and the
  next invocation waits on `turn_slot`;
- a node run that starts with no turn open waits for the *next* turn's
  invocation, which is the opposite of overlap;
- `PetriNet.inject` refuses `userIn`, and there is no long-lived invocation.

## Decision

### The format

A blueprint may declare where admission ends:

```yaml
turn:
  release: turnReleased
```

`turn.release` names a unit place of the root net. It may not be `userIn`,
`eventOut`, an `env:` place or a seeded place, and, as for `eventOut`, no
transition may consume, read or inhibit it: the place keeps every release,
so what the runtime sees is what the proofs see. With no `turn:` key nothing
below applies and a net behaves exactly as before.

### The runtime

- **Admission.** A token on the release place releases the oldest turn not
  yet released, and the next invocation of the session may open
  (`NetScope.open_turn`, in arrival order) and inject `userIn`. A turn
  admits the next at its release and nowhere else, except when it fails or
  is aborted (below). A turn that ends
  without a release keeps the next one waiting; `deadlock_free` over two
  turns reports such a net.
- **The released event.** The released invocation yields one event,
  `custom_metadata={"adk_libpetri": "released"}`, no content, authored by
  the net. A server that holds the next message until then reads this event
  instead of waiting for `run_async` to return.
- **Open invocations.** More than one invocation of a session may be open.
  `NetScope` keeps one record per open turn (its ctx, loop, node runs). A node
  run counts against the newest open turn and runs on its ctx, so its ADK
  events stream through that invocation; the tail of turn N started after
  turn N+1 opened streams through N+1. Each invocation waits only for its own
  node runs.
- **Answers.** `eventOut` tokens are answers in turn order: each one answers
  the oldest open turn that has none. An invocation returns once it has its
  answer and its node runs have finished. A net whose later turn can answer
  first gets its answers swapped between invocations; a client that must
  tell them apart puts an identity in the token (marvin_adk's frames carry
  their turn's `event_id`) instead of relying on the invocation id.
- **Failure and abort.** A failed node run fails the turn it counts against,
  and a failed or aborted turn admits the next one at once. `turnAbort`
  stays one net-wide signal: the net decides what an abort clears.
- **A release after the answer** is still reported while the invocation
  waits for its node runs. One that comes after the invocation ended admits
  the next turn and yields no event.

### The proofs

With `turn.release`, `turn_spec` becomes:

```
turn:next: (turn:remaining, <release>) -> (userIn, turn:released)
```

The next input waits for the release only. `turn:next` no longer tests node
runs, so the split of each `node:` transition into a start and a
`complete:T:run` deposit, and the per-node `turn:quiet:T` tokens, are not
added: they existed so `turn:next` could not fire between a node's start and
its deposit, and keeping them would order a tail-N start of a node against a
turn-N+1 start of the same node, which the runtime allows. `turn:next` reads
nothing the node transitions write, so it is independent of all of them for
VER-024's partial-order reduction.

- `deadlock_free` covers two turns with `eventOut` and `turn:released` among
  the sinks. Each verdict's scope reads "k turns, each input after the
  previous release".
- Safety claims cover overlapping turns. `place_bound: {place: eventOut,
  bound: 1}` no longer holds per turn (two answers may be pending); state the
  bound over the turns the claim covers.
- A failed or aborted turn admits the next input without a release. The
  model does not cover that, as ADR 0008's turn model does not cover the
  next input after a failed turn: the claims are about turns that do not
  fail (a safety claim still lets `turnAbort` arrive).

### Deployment (Vertex AI and the session services)

Release mode runs two `run_async` calls on one session at once. ADK 2.11
allows that: `Runner.run_async` documents that later queries "can be started
concurrently". The session services, read in the 2.11 source:

- `InMemorySessionService.append_event` writes through a stale copy to the
  stored session ("Update the storage session if the caller holds a stale
  copy"). Safe.
- `VertexAiSessionService.append_event` appends remotely with no
  stale-session check. Safe; the released event's `custom_metadata` is
  stored in `event_metadata` and round-trips.
- `DatabaseSessionService` (and `BaseSessionService.append_event`'s
  documented `StaleSessionError`) may reject an append from an invocation
  whose session copy was superseded by the other invocation's. Not safe
  without a retry in the caller; release mode is not recommended there.

Vertex Agent Engine is unchanged in kind: a session's marking lives in the
process that serves it (`SessionExecutorRegistry`), for every `PetriNet`,
`PetriWorkflow` and `PetriAgent`, so a deployment must route a session's
requests to one process. Release mode adds no new requirement on top of that.
No test here exercises Agent Engine.

## Consequences

- A net can model "the next input may arrive after release" and the runtime
  serves exactly that, so a proof over the release-mode `turn_spec` speaks
  for the deployed turn order.
- Nets without `turn:` keep the one-turn-at-a-time protocol, the same
  `turn_spec` (split, quiet tokens) and the same verdicts; the spec fixtures
  and diagrams do not change.
- An `eventOut` token no longer identifies its turn in release mode; the
  colour must.
- Events of two turns interleave in the session history in tap order.

## Plan

1. **Stub (tracer bullet):** parse and validate `turn.release`
   (`Blueprint.release`); yield the released event. Done.
2. **Overlap:** per-turn records in `NetScope` (`OpenTurn`), admission on
   release, answers in turn order, failure per turn. Done.
3. **Proofs:** release-mode `turn_spec`. Done. `tests/net/test_release.py`
   proves the Marvin-shaped `bp_turns/release.yaml` over two overlapping
   turns, runs turn N+1 while turn N's tail is in flight, and shows a claim
   (`place_bound` on the tail's input) that the serial twin proves and
   overlap violates.
