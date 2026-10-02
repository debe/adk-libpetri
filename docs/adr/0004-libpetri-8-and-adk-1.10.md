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

### 8.0: in-flight splitting (VER-004). No proof changed.

Transitions whose outputs another transition tests with an inhibitor, reset
or drain are now verified as start and complete steps. That covers every
commit and branch transition in Patterns A and C, and
`LlmAgent_BuildPrompt`. This was the change most likely to falsify a mutex
claim, because two inhibitor-guarded commits could both start before either
deposited. It did not: `placeBound(RACE_WON, 1)`, `placeBound(EVENT_OUT, 1)`
and `placeBound(COMMITTED, 1)` are still Proven under the split, so the
permit-place rewrite we had prepared was unnecessary. We did not set
`assumeAtomicFiring(true)`; it would have hidden exactly the race the split
checks for.

### Everything else: no fallout

- `VoiceSessionDemoTest` and `PatternB_QuorumDemoTest` stay Proven unchanged.
- No class implements `PetriNetExecutor` (6.1 added methods). No switch over
  `TerminationReason` (7.0 added `TERMINAL`) or `SmtProperty` (6.0 added
  `QuiescentCount`). No record patterns on `SmtVerificationResult`.
- The 5.0 Xor output validation and the 8.0 build-time arc checks rejected
  nothing in the stock subnets.
- 213 tests, 0 skipped, with `REQUIRE_Z3=1`.

## Consequences

- Consumers who run libpetri's SMT verifier themselves need a `z3` binary.
  The adk-libpetri runtime does not.
- Our deadlock-freedom claims are now strictly stronger: they assert that
  nothing is left behind unexplained, not just that a sink was reached.

## Deferred

Recorded so the next bump can pick them up deliberately:

- `GeminiLiveTransport` as a fork-free VAD tap (above).
- libpetri 6.1 snapshot/restore paired with ADK 1.10's
  `EventActions.agentState` for session checkpoint and resume.
- libpetri's injectable clock (`ExecutionEnvironment`) for the sleep-based
  timer tests.
- Per-subnet proofs via `SubnetDef.verify` with `arrivals(k, k)` (8.0), which
  would turn the shape-only `AdkNetInvariants` factories into proofs.
- `PersistStateSubnet`'s `deadline(5s)` is now reaped by a late executor
  (8.0). No current proof covers it; one that does must choose between
  `assumeNoReaping` and the reaped semantics, and say which.

## Next-bump procedure

Unchanged from [ADR 0002](0002-adk-version-compat.md). Add one step: run the
SMT suite with `REQUIRE_Z3=1` and, for any proof that flips, print the
`SmtVerificationResult`. libpetri's report names the stranded places and
gives a confirmed trace, which is faster than reasoning from the CHANGELOG.
