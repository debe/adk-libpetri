# ADR 0005: One turn at a time for the stock LLM agent

- **Status:** Accepted
- **Date:** 2026-10-03
- **Scope:** Java, unreleased. `LlmAgentSubnet` and `StreamingLlmAgentSubnet`
  (which share `buildComposedDef`), `PetriRunner`, `PetriAgent`,
  `AdkColours`.

## Context

`LlmAgentSubnet` told its turns apart by position. A second `USER_IN` that
arrived while turn 1 was still in its tool loop fired `BuildPrompt` at once,
and `BuildPrompt` reset `REASK_BUDGET` and `CONVERSATION`. Turn 1's `ReAsk`
then consumed turn 2's conversation. Worse, `ReAsk` and `BuildPrompt` can fire
in one orchestrator pass, and a reset only clears the marking the pass
started with (EXEC-003 AC5), so `CONVERSATION` could end with two tokens and
turn 2 replayed turn 1's turns. Review experiment E2 reproduces it from a
mid-loop marking.

The overlap is not hypothetical. ADK's `Runner` does not serialise
invocations per session, and `PetriAgent` ends a turn on its first terminal
event or failure, so a client that retries after a timeout sends the next
input while the previous turn is still running.

## Decision

A session's agent runs one turn at a time, and a later input queues
structurally until the current turn has ended.

- **The permit.** `AdkColours.TURN_PERMIT` holds one token while the agent is
  idle. `StartTurn` takes it together with the `USER_IN` and marks
  `TURN_ACTIVE`; a second `USER_IN` stays on its place until the permit is
  back. `BuildPrompt` runs after `StartTurn`, so the one transition that can
  throw while building a request does so after the turn holds
  `TURN_ACTIVE`, and the abort below can still clear it.
- **Every turn end returns it.** Router's answer and transfer branches now
  land on agent-owned places (`ANSWER`, `HANDOFF`), and `EmitAnswer` /
  `EmitTransfer` forward them to `EVENT_OUT` / `TRANSFER`, consume
  `TURN_ACTIVE` and the conversation, reset the reask budget, and return the
  permit in the same firing. The reask-exhausted fallback answers through
  `ANSWER`. LlmStep's model-error recovery and `BeforeModel` short-circuit
  land on `LLM_RESPONSE` and end through the router like any answer. Nothing
  of a turn outlives it, so `BuildPrompt` needs no reset arcs.
- **A failure has a way out.** A transition that fails consumes its inputs
  and produces nothing (EXEC-031), which would leave the turn holding the
  permit forever. `AdkColours.TURN_ABORT` is an environment place.
  `AbortTurn` takes it with `TURN_ACTIVE`, resets every place a turn holds,
  and returns the permit; `DropAbort` takes an abort that finds the permit
  at rest. `DropAbort` ranks above `StartTurn` (it only reads the permit),
  so a stale abort and an input that land in one pass drop the abort first;
  at equal rank `StartTurn` would win and the abort would wipe the new turn. `PetriAgent` signals `TURN_ABORT` on every transition failure of
  a runner it created, once per failure, from one subscription per runner.
- **The permit is seeded by the runner.** `PetriRunner` seeds one
  `TURN_PERMIT` token on a fresh start of a net that has the place (not on a
  restore or checkpoint resume, and not when the caller's initial marking
  names it), and declares `TURN_ABORT` as an environment place for a net
  that has it. A bare libpetri executor has to seed the permit itself. An
  agent composed through `DEF.instantiate(prefix)` has a prefixed permit,
  `prefix/turnPermit`, which the caller seeds; `PetriRunner` refuses a
  fresh start that leaves it empty, since no turn would ever start.

## Why a seeded permit, not an inhibitor

The obvious unseeded design is an inhibitor: `BuildPrompt` inhibited by a
`TURN_ACTIVE` place it marks, cleared at every turn end. On the Java executor
it works, because a transition never starts again while an earlier firing is
in flight (CONC-002). It cannot be proved without `assumeAtomicFiring`.
libpetri 8's in-flight split (VER-004) verifies `BuildPrompt` as a start and a
completion, and lets it start again while `inflight:BuildPrompt` is marked,
so two starts both see the place empty. This is VER-004 AC8's own example.
Throwaway experiment E9 (`StartTurn: one(userIn) + inhibitor(turnActive)`,
two arrivals): `placeBound(conversation, 1)` is Violated with the CONC-002
note, and Proven only under `assumeAtomicFiring`.

No unseeded variant escapes this. Any transition gated only by an inhibitor
can start twice from the same marking, so a structure that mints its own
first permit can mint two. Mutual exclusion that holds under the split needs
a token that both contenders consume, and the first one has to come from the
initial marking. We chose a proof without the atomic-firing assumption and
put the seeding in `PetriRunner`, where callers do not see it.

## Proofs

All in `StockSubnetProofsTest`, one property per `verify()`, none under
`assumeAtomicFiring`. The agent nets are verified composed rather than
through `SubnetDef.verify`, because the harness would feed the new
`turnAbort` port the same arrivals as `userIn`.

| Property | Setting |
|---|---|
| `placeBound(TURN_ACTIVE, 1)`, `placeBound(CONVERSATION, 1)`, `budgetPlaceBounded(REASK_BUDGET, 1)` | `USER_IN` `arrivals(2)` |
| `deadlockFree` with `EVENT_OUT`, `TRANSFER`, `TURN_PERMIT` as sinks; `quiescentCount(EVENT_OUT + TRANSFER) == 2` | `USER_IN` `arrivals(2, 2)` |
| `deadlockFree`, `placeBound(TURN_ACTIVE, 1)`, `placeBound(CONVERSATION, 1)` | as above, plus a failure model: a `Fail_<place>` transition per place a turn's token rests on, which takes it and signals `TURN_ABORT` |
| `placeBound(TURN_PERMIT, 1)`, `placeBound(TURN_ACTIVE, 1)` | `USER_IN` and `TURN_ABORT` both `arrivals(2)`: aborts at any moment |
| `placeBound(TURN_ACTIVE, 1)`, `placeBound(TURN_PERMIT, 1)`, `placeBound(CONVERSATION, 1)` | `StreamingLlmAgentSubnet`, `USER_IN` and `CHUNK` `arrivals(2)` |

Each was checked against a mutant: without the permit arc on `StartTurn` the
bounds come back Violated, and with `CONVERSATION` dropped from
`AbortTurn`'s resets the failure-model proof does. The reask-budget bound no
longer needs `assumeAtomicFiring`; ADR 0004 recorded it as one of two proofs
that did.

## Consequences

- **Queued turns.** A second input waits for the first turn, tool loop
  included. It is never dropped or merged, and it is answered in arrival
  order. A slow turn delays the next one by design.
- **`PetriAgent` correlates egress by position.** Two invocations open on one
  session at once both subscribe to the hot egress, so the first answer can
  settle both. The net is now correct under overlap; the adapter still
  assumes callers do not overlap. Correlating events with invocations is
  separate work.
- **What an abort does not cover.** `AbortTurn` clears what is at rest. If a
  failure elsewhere in a composed net triggers an abort while one of the
  agent's own actions is still running (a model call, say), that action's
  late output lands in the next turn. The non-streaming turn runs one action
  at a time, so a failure of one of its own transitions leaves nothing
  running, and the failure-model proof covers exactly that case. The
  streaming turn does not: `LlmCallStream` stays in flight while
  `EmitChunk` fires, so a failed `EmitChunk` leaves the stream running and
  its terminal chunk answers the next turn. Likewise, an abort from outside
  the agent that lands after `StartTurn` has fired but before its outputs
  are visible aborts the fresh turn; that turn's caller is failed by the
  same failure signal only if it had already subscribed. Closing the gap needs
  per-turn correlation (a turn identity on every in-turn token).
- **`AbortTurn` outranks the streaming step's chunk emission**, so a chunk
  admitted in the same pass as the abort is reset rather than emitted as a
  partial of the failed turn.
- **Breaking.** The DEF topology changed (new places and transitions, Router
  rebound, the `turnAbort` port, `CONVERSATION` no longer rests between
  turns); see the CHANGELOG.
