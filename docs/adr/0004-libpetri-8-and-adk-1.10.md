# ADR 0004: libpetri 8.0.0 and ADK 1.10.1

- **Status:** Accepted
- **Date:** 2026-10-03
- **Scope:** Java, unreleased. Bumps libpetri `3.0.1` -> `8.0.0` (five
  majors: 4.0, 5.0, 6.0, 7.0, 8.0) and google-adk `1.8.0` -> `1.10.1`
  (spanning 1.9.0).

## Context

[ADR 0003](0003-libpetri-3-and-adk-1.8.md)'s next-bump procedure, executed
again. The asymmetry is the same as last time: ADK barely moved on our
surface; libpetri moved a lot, and almost all of it is in the verifier.

## Dependency resolution

`./mvnw dependency:tree`: google-genai stays `1.58.0`, protobuf-java stays
`4.33.5`, rxjava stays `3.1.12`, opentelemetry-api stays `1.51.0`. The
protobuf floor and the README's consumer guidance are correct verbatim; only
the ADK version named in the prose changed. The one transitive that moved is
`io.modelcontextprotocol.sdk:mcp` `1.1.2` -> `2.0.0`, which we do not use;
consumers who do should read MCP's own 2.0 notes.

libpetri 4.0 dropped `org.sosy-lab:javasmt-solver-z3` entirely (see below),
so the dependency tree is smaller, not larger.

## ADK 1.8.0 -> 1.10.1: nothing breaking on our surface

`InvocationContext`, `Event`, `BaseLlm`, `BaseLlmConnection`,
`LiveRequestQueue`, `RunConfig`, `BaseTool`, `ToolContext`,
`BaseSessionService` and `InMemorySessionService` are byte-identical.
`BaseAgent` changed only a `"user"` literal to `Role.USER`. Additive changes:
`EventActions.agentState` (1.10.0), `Session.Builder.eventsView`, and the
`GeminiLiveTransport` seam (1.9.0). `LlmResponse.create` now treats a STOP
candidate with no parts as a successful empty turn rather than an error; no
test of ours depended on the old behaviour. `Runner.runAsync` now appends the
user event inside `runAgentForUserEvent`, with the same order and events.

### Both bypass rationales re-verified

- **`commonPool` hop in `Gemini.generateContent`: still there**
  (`.thenApplyAsync(LlmResponse::create)`). `SyncGeminiLlm` stays.
- **VAD edges dropped by `GeminiLlmConnection`: still dropped.**
  `convertToServerResponse` still ignores `voiceActivity()`.
  `VoiceVadEdgeAdkFoilTest` is green against 1.10.1. Its reflection target
  survived: the connection's constructor changed to take a
  `CompletableFuture<GeminiLiveTransport>`, but the static
  `convertToServerResponse(LiveServerMessage)` is unchanged.

The new `GeminiLiveTransport` seam is the first upstream hook that sees raw
`LiveServerMessage`s before ADK maps them. It could replace the direct
`client.async.live` read for VAD with a wrapping transport, staying fork-free.
Deferred; see below.

## libpetri 3.0.1 -> 8.0.0: what it cost us

### 4.0: Z3 is a binary, not JNI. The only compile break.

libpetri now drives Z3 as an external process (`z3` 4.8+ on `PATH`, or
`LIBPETRI_Z3`). `com.microsoft.z3.Context` no longer arrives transitively, so
every `z3Available()` probe that constructed a `Context` stopped compiling.
All seven now delegate to `SmtVerifier.z3Available()`. `Z3NativeGateTest`
keeps its job (fail the build under `REQUIRE_Z3` rather than let the SMT suite
skip silently) and now checks for the binary.

CI's "Install z3 JNI natives" step resolved the Z3 version from
`javasmt-solver-z3` in the dependency tree. That artifact no longer exists, so
the step would have failed outright. It is replaced by `apt-get install z3`.

### 5.0: strict deadlock-freedom. Three proofs went red, all correctly.

`deadlockFree()` now reads a quiescent marking with a token on any non-sink
place as a stranding. Before 5.0, one token on a sink excused the whole
marking. Three `isProven()` assertions failed, each with a confirmed
counterexample from closed state-space enumeration:

| Test | Stranded token | Why it is by design |
|---|---|---|
| `PatternA_SpeculativeRaceDemoTest` | `triggerB`, `triggerC` | Losing branches that never started; `inhibitor(RACE_WON)` is the cancellation. |
| `PatternC_OptimisticCommitDemoTest` | `slowTrigger` | The slow path, cancelled by `inhibitor(COMMITTED)`. |
| `MultiAgentDemoTest` | `LlmAgent_reaskBudget` | Unspent budget after a turn with no tool calls; the next `BuildPrompt` resets it. Surfaces on both the `EVENT_OUT` and the transfer-target endings. |

We did not answer with plain `sinkPlaces`, which would excuse those tokens
unconditionally and hide a real stranding mid-turn. Each one is declared with
`sinkPlacesWhen(marker, ...)` (5.1, VER-014) instead, so it is excused only
while the marker that explains it holds a token: `RACE_WON`, `COMMITTED`, or
the place the turn ended in.

### 8.0: in-flight splitting (VER-004). Two claims were false.

Transitions whose outputs another transition tests with an inhibitor, reset
or drain are now verified as start and complete steps. That covers every
commit and branch transition in Patterns A and C, and
`LlmAgent_BuildPrompt`. This was the change most likely to falsify a mutex
claim, because two inhibitor-guarded commits could both start before either
deposited.

The first pass of this upgrade recorded that no proof changed. That was
wrong, and the tests could not have shown it. Every multi-property test
chained `.property(...)` calls on one `SmtVerifier`, and
`SmtVerifier.property(p)` replaces the property rather than adding one, so
each test checked only its last property, which was `deadlockFree()` in
most of them. Checked one `verify()` at a time:

| Claim | Verdict alone | Outcome |
|---|---|---|
| Pattern A `placeBound(RACE_WON, 1)`, `placeBound(EVENT_OUT, 1)` | Violated | A real bug, not a modelling artefact. `inhibitor(RACE_WON)` reads the marking as of the start of an orchestrator pass and a commit deposits at its end (EXEC-003 AC5), so two branch results ready in one pass both commit on `BitmapNetExecutor`: `RACE_WON` and `EVENT_OUT` reach 2. Fixed structurally: `StartRace` seeds one `RACE_PERMIT` and every commit consumes it. Both bounds now prove without `assumeAtomicFiring`, and a regression test replays the double-commit marking. |
| Pattern C `placeBound(COMMITTED, 1)`, `mutualExclusion(VALIDATION_PASSED, VALIDATION_FAILED)` | Proven | The XOR on validation, not the inhibitor, is what excludes a second commit: only one commit is ever enabled in a turn. |
| Pattern B `placeBound(QUORUM_MET, 1)`, `placeBound(EVENT_OUT, 1)` | Proven | One synthesis transition, which the executor never restarts while in flight (CONC-002). |
| Voice `CHUNK_BUDGET` bound | Violated | The budget bounded nothing: `EmitChunk` returned the permit it took, and overlapping requests reached K+1 at runtime. Removed from `LlmStreamingStepSubnet` and `StreamingLlmAgentSubnet`. The streaming step's own proof seeded no request and held even at bound 0; it is replaced by a seeded deadlock-freedom proof with a starved-emit mutant that must come back Violated. |
| Multi-agent `deadlockFree()` | Proven | Unchanged. |

Every SMT test now proves one property per `verify()` call, through the
`SmtProofs` test helper or a `VerificationHarness` (which does accumulate),
and asserts `isProven()` per property.

Two proofs set `assumeAtomicFiring(true)`: the reask budget and the Pattern A
race permit, each across two arrivals. The assumption is exact for both, but
not for the reason first given (that a completed future's outputs land in
the same step; they land at the end of the pass). Without it, the only
counterexample starts the seed transition again while an earlier firing is in
flight. libpetri's report flags that (CONC-002), and the Java executor never
does it. Modelling the rule directly, with a token the transition takes at
start and returns at completion, both bounds prove with the split in place
(checked once with a throwaway model, not part of the build).
Every other proof runs with the split. The Pattern A commit bounds are
claimed per turn only: across overlapping turns a commit still in flight
when the next turn starts lands after that turn's reset, and the demo does
not tag results with their turn.

### Everything else: no fallout

- `PatternB_QuorumDemoTest` stays Proven, now property by property. The voice
  net stays deadlock-free.
- No class implements `PetriNetExecutor` (6.1 added methods). No switch over
  `TerminationReason` (7.0 added `TERMINAL`) or `SmtProperty` (6.0 added
  `QuiescentCount`). No record patterns on `SmtVerificationResult`.
- The 5.0 Xor output validation and the 8.0 build-time arc checks rejected
  nothing in the stock subnets.
- 239 tests, 0 skipped, with `REQUIRE_Z3=1`.

## Consequences

- Consumers who run libpetri's SMT verifier themselves need a `z3` binary.
  The adk-libpetri runtime does not.
- Our deadlock-freedom claims are now strictly stronger: they assert that
  nothing is left behind unexplained, not just that a sink was reached.
- A proof is only as good as the property it actually checks. One property
  per `verify()` call is now the rule, and a new proof should be shown to
  fail on a variant that breaks it before it is trusted.

## Follow-up

The items this ADR first deferred were taken up in the same release:

- `GeminiLiveTransport` as a fork-free VAD tap: `VadTapGemini`.
- Snapshot/restore with `EventActions.agentState`: `SessionCheckpointStore`
  and the `AgentStateCheckpointStore` exemplar.
- The injectable clock: `ManualClock`, used by the silence-recovery tests.
- Per-subnet proofs via `SubnetDef.verify` with `arrivals(k, k)`:
  `StockSubnetProofsTest`.
- `PersistStateSubnet`'s reaped deadline: the per-subnet proof showed the
  deadline never bounded what it claimed to (a hung `appendEvent`), so it
  became an action timeout rather than a choice between `assumeNoReaping`
  and stranded writes.

## Next-bump procedure

Unchanged from [ADR 0002](0002-adk-version-compat.md). Add one step: run the
SMT suite with `REQUIRE_Z3=1` and, for any proof that flips, print the
`SmtVerificationResult`. A flip in a mutex or bound proof is worth replaying
on `BitmapNetExecutor`, as Pattern A's was. libpetri's report names the stranded places and
gives a confirmed trace, which is faster than reasoning from the CHANGELOG.
