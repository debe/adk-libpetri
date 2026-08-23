# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when
working with the adk-libpetri repository.

## Project overview

adk-libpetri replaces Google ADK Java's orchestration core
(`SequentialAgent`, `ParallelAgent`, `LoopAgent`, `BaseLlmFlow`,
`AgentTransfer`, `Runner` driving an RxJava pipeline) with a
Coloured Time Petri Net runtime built on
[libpetri](https://github.com/debe/libpetri). Stock ADK `Runner`
consumes turn-based sessions through the `PetriAgent extends
BaseAgent` adapter; Live/BIDI paths bridge via `BidiPetriAgent`
over `LiveConnection`. There is no ADK source fork.

The repo follows libpetri's multi-language layout (`java/`,
eventually `typescript/`, `rust/`, `python/`). It is currently Java
only. The layout is multi-language-ready for trivial port additions.

## Build and test commands

### Java (`java/`)

```bash
cd java
./mvnw verify                                   # Full build + tests
./mvnw test                                     # Tests only
./mvnw test -Dtest="MultiAgentDemoTest"        # Single class
./mvnw test -Dtest="*Streaming*"                # Wildcard
```

Java 25 (no preview features needed). Maven 3.9.x via wrapper. Z3 is
pulled transitively from libpetri. Tests requiring Z3 native libs
use `@EnabledIf("z3Available")` so the build does not fail without
them.

### Diagrams (`docs/diagrams/`)

```bash
cd docs/diagrams
npm install
npm run build
```

Regenerates the SVG diagrams embedded in the root README.
Requires Node.js 20 or later and graphviz `dot`.

## Architecture

The architectural pitch (sequential-projection critique, runtime
model, stock subnet catalog, ADK Runner integration) and the
project's design commitments all live in the root
[`README.md`](README.md) — it is the single source of truth. Read it
before making structural changes. The diagrams it embeds are
generated from [`docs/diagrams/`](docs/diagrams/).

### Source layout (`java/src/main/java/org/libpetri/adk/`)

- **`colours/`**: `AdkColours`, the typed boundary catalog
  (`Place<Content>`, `Place<Event>`, `Place<LlmRequest>`, etc.). Has
  wrapper records for generic-typed places (`ToolCalls`,
  `ToolResults`, `LegacySessionWrite`, `TransferTarget`).
- **`bridge/`**: `EventStoreToFlowableBridge` (EventStore decorator
  to RxJava `Flowable<Event>`, requires an explicit downstream
  `EventStore`) and `OtelEventStore` (OT spans per transition fire).
  Executors are caller-supplied. The library carries no shared
  executor singleton.
- **`subnet/`**: 9 stock subnets plus `SubnetActions` validator.
  `LlmStep`, `ToolDispatch`, `PromptBuilder`, `Router`, `LlmAgent`,
  `PersistState`, `TransferRouter`, plus the beta SSE-streaming pair
  `LlmStreamingStep` and `StreamingLlmAgent` (both `@Experimental`).
  Voice-specific demo subnets (`BargeIn`, `LiveApiRecovery`, `Vad`)
  are not part of the shipped library. They live under
  `src/test/java/org/libpetri/adk/demos/voice/` as exemplars of
  BIDI and Live-API composition. `Vad` is the producer that reads
  genai's Live session directly and turns its speech-activity edges
  into the `VOICE_ACTIVITY_OPEN` window `BargeIn` reads.
- **`runner/`**: `PetriRunner` (libpetri-native handle),
  `PetriAgent` (ADK `BaseAgent` adapter),
  `SessionExecutorRegistry` (lazy per-session executor map), and
  `SessionKey`.
- **`verify/`**: `AdkNetInvariants`. 3 structural validators (run
  on every `mvn verify`) plus 3 SMT property factories
  (`PlaceBound`, `MutualExclusion` via libpetri's `SmtVerifier`).

### Two demo programs (`java/src/test/java/org/libpetri/adk/demos/`)

- **`MultiAgentDemoTest`**. Planner LlmAgent plus TransferRouter
  composed, driven via stock `InMemoryRunner`, observability via
  `OtelEventStore`, structural invariants asserted before execution.
  Z3 proves the composed net is deadlock-free.
- **`VoiceSessionDemoTest`**. Streaming plus barge-in plus silence
  recovery composed into one long-lived per-session net with
  multi-direction env places. Z3 proves it deadlock-free and proves the
  chunk budget bounded, with the env places modelled via
  `environmentMode(bounded(1))`. Without that the verifier returns
  `Unknown`, because a proof that ignores env places would be vacuous.
  SCG bounded exploration lives next door in `LiveApiRecoverySubnetTest`
  and confirms a finite reachable state space for the composed BIDI net.

## Load-bearing design principles

These are the project's structural commitments (the README states
them as "Design commitments"). Violating any of them introduces the
bug classes the design is meant to eliminate.

1. **The interaction model with a running net is env-place
   injection only.** No method calls into transitions. No side
   channels. This applies to *every* external signal, not just
   `USER_IN`. Scroll events, sensor readings, webhooks, and the
   rest all enter via their own typed env place. See
   `ScrollAwareDemoTest`.
2. **The marking IS the state.** External stores (ADK
   `Session.state`, DB, cache) are write-only legacy bridges, never
   read from inside the net during execution. Read arcs on typed
   in-net places are fine (the in-net conversation-place pattern).
3. **Typed colours per domain concept.** Never collapse
   heterogeneous state into a single `Place<Map<String, Object>>`
   "bag." The only bag-shaped colour in the catalog is
   `LegacySessionWrite`, named loudly and used only for the ADK
   Session export bridge.
4. **No ADK source fork — and no dependency fork either. Zero
   forks.** Integration is via the `PetriAgent extends BaseAgent`
   adapter in `runner/PetriAgent.java`. The orchestration core
   (`SequentialAgent`/`LoopAgent`/`BaseLlmFlow`/`AgentTransfer`/`Runner`)
   is replaced, never forked. Where a defect lives in *ADK's wrapper
   over genai* — the `commonPool` hops in `Gemini.generateContent`,
   the VAD/barge-in signals dropped by `GeminiLlmConnection` — the
   wrapper is bypassed in thin user code that calls genai directly
   (the `SyncGeminiLlm` exemplar for the LLM path; a direct
   `client.async.live` read for voice), never patched in a fork of
   genai or ADK.
5. **Observability via `EventStore` decorator chain.**
   `OtelEventStore`, `EventStore.logging()` (libpetri-provided),
   and any future structured-logging or debug-recording stores
   wrap each other via the delegate pattern. Never reach for
   `ExecutionContextProvider` for observability. That is only for
   action-side ambient-context propagation.
   `PetriRunner.failureSignal()` is control flow, not observability: it
   exists so a caller can fail the turn that was in flight without
   killing the session's egress, and it emits `TransitionFailure` only.
   Route anything you want to *record* through the `EventStore` chain.
6. **Reask budgets bound autonomous LLM-and-tool loops
   structurally.** Use `Place<Void>` with a
   priority-and-inhibitor exhaustion-fallback transition (the
   reask-budget pattern). This is not a generic loop bound. It is
   only for autonomous-runaway protection inside `LlmAgentSubnet`.
7. **Stock subnets are convenience templates, not the framework.**
   Users compose their own subnets directly via
   `PetriNet.builder().compose()` plus `SubnetDef.fromNet(...)`.
   The framework IS the composition primitives. Stock subnets are
   examples that happen to work for common cases.
8. **Per-session executor lifetime is caller-bound, with an
   explicit-close default.** `SessionExecutorRegistry.strongOwned()`
   is the documented default: the runner lives until the caller
   invokes `close(SessionKey)`/`closeAll()` from a session-end hook.
   `cleanerOwned()` is opt-in for callers that hold a stable strong
   owner whose GC tracks session end — when that owner is collected,
   `java.lang.ref.Cleaner` tears the runner down. Neither mode has an
   API path that registers a runner without a teardown route, so
   leaks (orphan orchestrator threads, hot processors, marking state)
   are structural non-options. `ctx.session()` is NOT a safe
   `cleanerOwned()` owner with `InMemorySessionService` (defensive
   copies); supply a stable identity (websocket session, holder map).

## Key conventions

- Java 25, `record`s for immutable types, `sealed` interfaces for
  discriminated unions.
- Subnet definitions are stateless `SubnetDef<Void>` instances.
  Per-session action closures bind via `PetriNet.bindActions(Map)`.
- All subnet transition names are `<NAME>_<Verb>` (for example
  `LlmAgent_BuildPrompt`) so `SubnetActions.bind` validates keys
  reliably.
- All `Place<T>` constants live in either `AdkColours` (boundary)
  or on the owning subnet's `Places` holder class (internal).
- Tests use `BitmapNetExecutor.builder(net, initial).run()` for
  synchronous test patterns. Long-lived BIDI tests use `runAsync`
  plus drain.

## Multi-language readiness

When porting to TypeScript, Rust, or Python, mirror the Java module
layout under a sibling top-level subdir (`typescript/`, `rust/`,
`python/`). Each language adapter calls into the corresponding
libpetri language port. Cross-language specs (if any) live in
`spec/`.

## Versioning and release

Each language has its own version, tagged with the language prefix
(for example `java/v0.4.0`). Java is currently the only one.

**Versioning is 0.x.** A minor may break API. The turn-based path is the
settled part; `@Experimental` surfaces (SSE, BIDI/live) may change in any
release. Do not describe anything as "stable for 1.x" anywhere in the
repo: that phrasing predates the first real release and was removed.

**No `-SNAPSHOT`.** Mirroring libpetri, `java/pom.xml` carries a bare
release version on `main` between releases. There is no post-release bump
step and no snapshot publishing.

**Maven Central**: `org.libpetri:adk-libpetri`, published by
`scripts/release-java.sh <version>`. The `release` profile in
`java/pom.xml` (source/javadoc JARs, GPG signing, central-publishing with
`autoPublish=true` and `waitUntil=published`) does the actual work; the
script only drives it. Publishing is local, from a developer machine.
There is deliberately no publish-on-tag workflow and no signing key in
GitHub secrets, which is also how libpetri does it.

Do **not** add `flatten-maven-plugin`. libpetri uses it, but
`flattenMode=ossrh` drops `dependencyManagement`, and this project's
protobuf floor is delivered through exactly that block on a non-direct
dependency (see the README's protobuf section). Flattening would silently
break it.

The release ritual, in order:

1. Commit the CHANGELOG dating: rename the unreleased heading to
   `## Java <version> - YYYY-MM-DD`, and bump the version in the README
   install blocks. The script reads the CHANGELOG and never writes it.
2. `scripts/release-java.sh --dry-run <version>` to rehearse
   (`mvn clean verify -Prelease`, which really signs), then
   `git reset HEAD~1` to drop the version commit it leaves behind.
3. `scripts/release-java.sh <version>`.

It stamps the version, commits `release: java <version>`, runs
`clean deploy -Prelease` blocking until Central reports published, tags
`java/v<version>`, pushes commit and tag, and creates the GitHub release
from the CHANGELOG section.

Prerequisites, all checked by the script's preflight: clean tree, on
`main` and not behind `origin/main`, a GPG secret key in the agent,
`<server id="central">` in `~/.m2/settings.xml`, `gh` authenticated, a
CHANGELOG section for the version, and a tag that does not yet exist.
