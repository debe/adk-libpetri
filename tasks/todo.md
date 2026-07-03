# Inflight Change Review

## Goal
Assess the inflight ADK/libpetri Java changes for human readability, idiomatic Java 25 fit, net/session architecture fit, performance risk, documentation quality, and commit readiness.

## Domain-boundary workflow
- Runner/live-session domain: `org.libpetri.adk.runner`, net/session abstractions, lifecycle, synchronization, API ergonomics.
- Build/dependency/docs domain: Maven changes plus `README.md`, `java/README.md`, and `scripts/release-java.sh` public-facing instructions.
- Demo/test domain: demo tests, integration tests, coverage of new runner/live behavior.
- Main orchestrator: consolidate agent findings, make minimal source-level refactors only where needed, verify with targeted commands, and record final readiness.

## Domain review results

### Runner/live-session
- `BidiPetriAgent` is a good fit for the current boundary: it owns the generic genai-Live pump/merge/dispose path while `LiveConnection` stays the narrow provider seam and `PetriRunner` stays the net/env-place seam.
- Hardened `BidiPetriAgent.bridge(...)`: validates all arguments before subscriptions, and failed fire-and-forget realtime/content sends now close the connection instead of silently leaving a broken pump.
- `LiveConnection` is intentionally scoped to genai `LiveServerMessage`; a truly provider-neutral bridge would be a larger redesign and is not required for this commit.
- Updated `PetriAgent` live javadocs so the shipped `BidiPetriAgent.bridge(...)` helper is the documented BIDI input path.

### Runner lifecycle/performance
- `SessionExecutorRegistry` ownership model is coherent: `strongOwned()` is the safe default with explicit close; `cleanerOwned()` remains opt-in for a stable strong owner.
- Hardened cleaner path: cleaner actions remove entries and call `PetriRunner.drainAsync()` instead of blocking the single shared Cleaner daemon on synchronous shutdown.
- Added `Reference.reachabilityFence(owner)` after cleaner registration so owner reachability cannot disappear between weak entry publication and cleaner registration.
- Explicit `close(SessionKey)`/`closeAll()` intentionally remain synchronous teardown APIs.

### Build/docs/release
- `java/pom.xml` aligns dependency pins and release profile: libpetri 2.10.4, JUnit 6.1.0, OpenTelemetry test 1.63.0, protobuf floor 4.33.5, sources/javadocs/GPG/Central publishing profile.
- README and Java README now document the Live/BIDI split, registry lifecycle choice, source layout, and correct PetriRunner builder inputs.
- Fixed `scripts/release-java.sh` usage text; removed stale “add release profile first” note and removed command-substitution backticks from the unquoted heredoc help text.

### Tests/demos
- Runner-level BIDI tests are provider-free; Gemini-specific raw-message decoding stays in `SyncGeminiLiveConnectionTest`.
- `PetriRunnerTest` covers `Place<Void>` signal injection through `signal(Place<Void>)` and `inject(Place<T>, Token<T>)`.
- `SessionExecutorRegistryTest` covers reuse, owner isolation, explicit close, cleaner cleanup, deprecated constructor compatibility, and a forced concurrent first-call race.
- Demo tests use explicit `cleanerOwned()` instead of the deprecated ambiguous constructor.

## Risks accepted
- `cleanerOwned()` still depends on a stable, strongly-held owner. This is documented and not mechanically enforceable.
- `BidiPetriAgent.bridge(...)` still fires send `Completable`s independently; ordering/backpressure semantics depend on the supplied `LiveConnection`. This matches the existing ADK-style live pump and avoids broad API redesign.
- Release-profile verification ran with `-Dgpg.skip=true`; actual GPG signing still depends on local key setup and should be exercised during release rehearsal.

## Verification performed
- `bash -n scripts/release-java.sh && scripts/release-java.sh --help` — passed; help output printed without invoking Maven.
- `./mvnw -Dtest=MultiAgentDemoTest,ScrollAwareDemoTest,VoiceSessionDemoTest,PatternA_SpeculativeRaceDemoTest,PatternB_QuorumDemoTest,PatternC_OptimisticCommitDemoTest,PetriAgentIntegrationTest,BidiPetriAgentTest,PetriRunnerTest,SessionExecutorRegistryTest,SessionExecutorRegistryStrongOwnedTest,SyncGeminiLiveConnectionTest test` — passed; 52 tests, 0 failures, 0 errors, 0 skipped.
- `./mvnw verify` — passed; 191 tests, 0 failures, 0 errors, 0 skipped; jar built.
- `./mvnw -Prelease -Dgpg.skip=true verify` — passed; 191 tests, sources jar, javadoc jar, and main jar built. Existing `PromptBuilderSubnet` javadoc warnings remain outside this inflight change.
- Sentrux session diff — passed; quality stable/improved (`quality_signal` 6931 → 6932), no violations.
- Serena diagnostics on changed runner main files — no diagnostics returned.

## Commit readiness
Commit-ready: yes, after staging the intended tracked edits plus new source/test/review files.

Intentional files to include:
- `.gitignore`
- `README.md`
- `java/README.md`
- `java/pom.xml`
- `scripts/release-java.sh`
- `java/src/main/java/org/libpetri/adk/colours/AdkColours.java`
- `java/src/main/java/org/libpetri/adk/runner/PetriAgent.java`
- `java/src/main/java/org/libpetri/adk/runner/PetriRunner.java`
- `java/src/main/java/org/libpetri/adk/runner/SessionExecutorRegistry.java`
- `java/src/main/java/org/libpetri/adk/runner/BidiPetriAgent.java`
- `java/src/main/java/org/libpetri/adk/runner/LiveConnection.java`
- changed demo/integration tests under `java/src/test/java/org/libpetri/adk/...`
- new tests/exemplars: `SyncGeminiLiveConnection.java`, `SyncGeminiLiveConnectionTest.java`, `BidiPetriAgentTest.java`
- `tasks/todo.md` if retaining the requested review documentation in-repo.

Do not include generated outputs. `.serena/` is now ignored as agent metadata.

## Progress
- [x] Plan recorded.
- [x] Project memories captured.
- [x] Domain agents partitioned.
- [x] Inflight changes mapped.
- [x] Runner/live-session reviewed.
- [x] Build/docs reviewed.
- [x] Demo/test surface reviewed.
- [x] Necessary refactors applied.
- [x] Review results documented.
- [x] Verification run.
- [x] Commit readiness assessed.

## Design-review fix execution — 2026-07-03

### Library track (`~/repositories/adk-libpetri`)
- Reconciled Java artifact to `1.3.0-SNAPSHOT` and moved the changelog entry to `Unreleased`.
- Added library-owned `@Experimental` and applied it to BIDI/live and SSE-streaming public surfaces.
- Repaired `CLAUDE.md` release/lifecycle notes and removed internal roadmap markers from public javadocs/comments.
- Shared the composed agent-subnet builder between streaming and non-streaming subnets.
- Removed the dead `legacySessionWrite` output port from agent subnet interfaces while keeping `AdkColours.LEGACY_SESSION_WRITE` and `PersistStateSubnet`.
- Committed with message `Address ADK design-review library track: version, beta fencing, docs, dedup, dead-port removal`.

### Marvin track (`~/otto-repos/nucleus_marvin`)
- Deployed `adk-libpetri:1.3.0-SNAPSHOT` into committed `maven-repo/` and scoped Gradle resolution to that module.
- Aligned Marvin's explicit `google-adk` pin to `1.4.0`.
- Deleted dead vendored `com/google/adk/models` sources.
- Extracted `AbstractLiveAssistantService` for shared live-assistant dispatch behavior and updated coverage.
- Fixed deleted-class javadocs/version rot and added google-genai reflection diagnostics tied to the shutdown canary.
- Committed as `a44ddfaa` (`Address ADK design-review Marvin track`).

### Verification performed during execution
- Library: `cd java && ./mvnw verify` — passed; 194 tests, 0 failures/errors.
- Library bytecode: `javap -v ... | grep -i Experimental` on `BidiPetriAgent`, `LiveConnection`, and `StreamingLlmAgentSubnet` — each printed `org.libpetri.adk.Experimental`.
- Library: `cd java && ./mvnw -Prelease -Dgpg.skip=true verify` — passed; 194 tests, sources and javadoc JARs built; pre-existing javadoc warnings remained.
- Marvin clean-resolve proof: after deleting `~/.m2/repository/org/libpetri/adk-libpetri`, `./gradlew compileJava --refresh-dependencies` — passed from committed `maven-repo/`. The plan's `:marvin:compileJava` path was invalid because this checkout's root project is already `marvin`.
- Marvin: `./gradlew check` — passed.
- Marvin post-check guards: vendored `com/google/adk/models` absent; versions are `adk-libpetri:1.3.0-SNAPSHOT` and `google-adk:1.4.0`; deleted `LiveApiService`/`LiveApiContentUpdaterService` refs are gone from `src`; `VoiceLiveEndpoint` BIDI drop guard remains.
