# ADR 0001: Pre-port design gate

- **Status:** Accepted
- **Date:** 2026-07-06
- **Scope:** Java `1.3.0-SNAPSHOT` (`adk-libpetri`), before any TypeScript /
  Rust / Python port is started.

## Context

Two questions must be settled before the Java design is mirrored into other
languages, because a port inherits whatever the Java reference commits to:

1. **Is the interface/class design sound and uniform across the four domains**
   (normal turn-based, nonstreaming, streaming/SSE, voice/BIDI)? A port should
   mirror the module layout and contracts without re-litigating design
   decisions three times.
2. **Does the project prove its central claim** that it is better to orchestrate
   an LLM-agent session with a Coloured Timed Petri Net than with Google ADK's
   stock orchestrator (`SequentialAgent` / `ParallelAgent` / `LoopAgent` /
   `BaseLlmFlow` / `AgentTransfer` / `Runner`)?

This ADR records the verdict on each, the one architectural decision that
unblocks the ports, and the gate (a tracked checklist) that closes the rest.

## Verdict A: design soundness across the four domains

**The orchestration core is sound and port-ready. The ADK host-adapter layer is
not uniform yet, but that non-uniformity is confined to code a port does not
inherit.**

Port-stable and already uniform (mirror these):

- `colours/` (the typed boundary catalog),
- the stock `subnet/` set + `SubnetActions`,
- `verify/` (structural validators + SMT property factories),
- the reask-budget / chunk-budget marking pattern,
- `SessionExecutorRegistry` (two ownership modes, language-neutral contracts).

Not uniform (all inside the ADK adapter): the four domains are wired with three
invocation-id idioms, two end-of-turn idioms, and two hot-stream race guards,
spread across ad-hoc branches in `PetriAgent.runAsyncImpl` / `runLiveImpl`:

- invocation id is stamped three ways: a subnet fake supplier (NONE), overwrite
  with `ctx.invocationId()` (SSE, `PetriAgent.java:280`), and a fresh
  `Event.generateEventId()` (`BidiPetriAgent.java:101`). The code's own TODO
  asks to make it a net token (`PetriAgent.java:74-78`).
- end-of-turn is inferred three ways: `Event.partial()==false` (egress),
  `LlmResponse.turnComplete` (`LlmStreamingStepSubnet`), and the unused,
  first-class `AdkColours.END_INVOCATION` `Place<Void>`.
- the hot-stream subscribe-before-inject race is solved twice: a
  `CompletableFuture` sink (NONE) vs `replay().connect()` (SSE).

### Decision (P0 #1): `PetriRunner` IS the ADK adapter

The single biggest pre-port question was where the host-neutral line falls
inside `runner/`. `PetriRunner.adkEvents()` is typed to
`com.google.adk.events.Event` (`PetriRunner.java:169`), which a port with no ADK
cannot mirror. Two options were considered:

- **(a)** Declare `PetriRunner` explicitly the ADK adapter; the port reference is
  `PetriNetExecutor` + `subnet/` + `colours/` + `verify/` + `SessionExecutorRegistry`
  only. Each language writes its own thin host adapter.
- **(b)** Extract a host-neutral runner core (`inject` / `signal` / `egress<T>` /
  lifecycle) that the ADK `PetriRunner` specializes.

**Decision: (a).** `PetriRunner` is the ADK adapter. Consequences:

- The ADK-typed egress is correct-by-design, not a smell. There is **no
  host-neutral runner core to extract**.
- The port reference is the executor + subnets + colours + verify + registry.
  The concrete, port-neutral contracts for these belong in [`spec/`](../../spec/)
  when the first port begins.
- This **downgrades P0 #2 (end-of-turn) and P0 #3 (invocation-id) from
  port-blocking decisions to adapter-internal consistency refactors** (see P1).
  They should still be unified for cleanliness, but a port no longer inherits
  them.
- The `PetriAgent` factory matrix (`of` x2 + `ofLive` x2 + `LiveConfig`) is a
  faithful shadow of ADK's `runAsync` / `runLive` + `RunConfig.StreamingMode`
  seam. It is fine for the Java ADK adapter and is explicitly **not** part of the
  port reference; other ecosystems have no ADK `Runner`.

## Verdict B: is "Petri net > ADK orchestrator" proven?

**Proven airtight for one of the four domains, showcased (not compared) for the
other three, and two flagship cases are prose-only.** Proof surface as it stands:

| Domain / pattern | Executable demo | ADK-only foil | Machine-checked property |
|---|---|---|---|
| Multi-agent transfer (normal) | yes | no | deadlock-free (Z3) |
| Speculative race (Pattern A) | yes | yes | `PlaceBound(EVENT_OUT, 1)` |
| K-of-N quorum (Pattern B) | yes | yes | exactly-one-synthesis (Z3) |
| Optimistic commit (Pattern C) | yes | yes | at-most-one-commits (Z3) |
| README Case 1: fan-out batch state | no | no | none (SVG + prose) |
| README Case 2: stale-result / generation | no | no | none (SVG + prose) |
| Streaming (SSE) | yes | **no** | `CHUNK_BUDGET <= K` (Z3) |
| Voice (BIDI) | yes | **no** | deadlock-free + bounded SCG (Z3) |

Honest reading:

- **Rigorous (demo + foil + Z3):** the three composition patterns
  (turn-based / nonstreaming). These are the real, falsifiable head-to-heads.
- **Showcased, no comparison:** streaming and voice carry SMT-checked internal
  invariants but no ADK-only foil. The strongest real-world argument (ADK's
  `GeminiLlmConnection` drops the VAD / barge-in edges) is asserted in docs,
  never run.
- **Prose + SVG only:** README Cases 1 and 2 have no `COLLECTOR` /
  `LATEST_GENERATION` net in the test tree.
- **Two overclaims:** `MultiAgentDemoTest` narrates an `AgentTransfer` NPE in a
  comment but never runs stock `AgentTransfer` with a bad name; `PatternC`'s
  javadoc promises a concurrent `Session.state` race the foil never executes.
- **Framing:** the foils honestly show ADK *can* match the behavior via a custom
  `BaseAgent` escape. The defensible thesis is therefore **"ADK requires a
  framework escape that forfeits the structural, SMT-checkable guarantees,"** not
  "ADK cannot express this." The `patterns/package-info` "cannot express"
  wording overstates the evidence.
- **Safety vs liveness:** every Z3 proof establishes safety (at-most-once,
  boundedness, deadlock-freedom). The liveness wins (first-wins latency, early
  K-of-N fire, pre-warm fail-path) rest on generous `elapsedMs` timing
  assertions: real, but empirical rather than proven.

## The gate

P0 is decided above. P1 makes the Java adapter internally uniform. P2 makes the
thesis airtight. P1 and P2 are independent and can land as separate commits.

### P1: adapter-internal uniformity (Java now)

- [ ] Collapse NONE + SSE into one `TurnEgressPolicy` seam (one race guard, one
      `emitPartials` flag, one id-decoration). Keep BIDI as its own seam.
- [ ] Resolve `runLiveImpl`'s `liveConfig == null` branch: make `LiveConfig`
      mandatory for `runLive`, or name an explicit egress-only mode (the current
      null path is a "brain with no ears": egress with no input pump).
- [ ] Hide `executorRef` / `deferredExecutorRef` behind one opaque `ChunkSink`
      (a `TransitionContext` injector in libpetri is the ideal home but is out of
      this repo's scope). Document the env-place resolution-by-Place-identity
      contract the streaming action relies on (`LlmStreamingStepSubnet.java:247`).
- [ ] Unify end-of-turn and invocation-id inside the adapter (from P0 #2/#3):
      prefer a first-class net token for each so egress stops inferring them.

### P2: make the thesis airtight (proof-surface work)

- [ ] **Voice/BIDI foil** (strongest, most concrete): a stock-ADK path
      (`GeminiLlmConnection` or a faithful mock over the same `LiveServerMessage`
      frames `SyncGeminiLiveConnectionTest` uses) fed a VAD / barge-in edge,
      asserting the edge is absent from ADK's `Flowable<LlmResponse>`.
- [ ] **Streaming/SSE foil**: show the property libpetri proves
      (`CHUNK_BUDGET <= K`, at-most-K concurrent emissions) is not
      expressible/verifiable in ADK's streaming. This is a
      "structural-guarantee-absent" foil, not a "broken-behavior" one; land a
      falsifiable assertion or record it honestly as a documented gap.
- [ ] **Run the two comment-only failures:** stock `AgentTransfer` with a
      hallucinated target (vs `MultiAgentDemoTest`'s typed-error `Out.xor`); and
      `PatternC`'s promised concurrent `Session.state` race. Convert narration
      to a green-locked assertion.
- [ ] **README Cases 1 and 2:** back with real demo + foil + SMT, or demote to
      "illustrative" with a one-line honesty note.
- [ ] **Framing fixes (docs):** restate the thesis as "escape-required +
      guarantees-forfeited" (fix `patterns/package-info` "cannot express"); label
      the `elapsedMs` latency wins as empirical.

## What a port inherits

A port mirrors the executor + `subnet/` + `colours/` + `verify/` + registry
contracts (see `spec/`), plus a domain-by-domain proof template: for each of the
four domains, a demo, an ADK-only foil, and a machine-checked property. P2 exists
so that template is complete before it is copied into a second language.
