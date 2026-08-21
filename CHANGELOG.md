# Changelog

All notable changes to this project will be documented in this file.

Format: per-language sections under each version. Tags are language-
prefixed (e.g. `java/v1.0.0`).


## 1.3.0 - Unreleased

### Java

- **Streaming SSE path**: promoted `LlmStreamingStepSubnet` into
  `src/main` and added `StreamingLlmAgentSubnet`, the SSE counterpart to
  `LlmAgentSubnet`. `PetriAgent.runAsyncImpl` now honors
  `RunConfig.StreamingMode.SSE` by replaying token partials through to the
  first non-partial terminal event, with one ADK invocation id across the
  whole turn.
- **Live/BIDI path**: `PetriAgent.ofLive(...)` and `PetriAgent.LiveConfig`
  ship a first-class path through `BidiPetriAgent.bridge`; plain
  `PetriAgent.of(...)` keeps the egress-only `runLive` behavior.
- **Live egress is net-owned** (breaking, within the `@Experimental` BIDI
  surface): `BidiPetriAgent.bridge` no longer takes an `author` and no longer
  maps server frames to `Event`s. It returns `PetriRunner.adkEvents()` alone;
  model content enters the net through the consumer's `onServerMessage`
  callback and a net transition authors the `Event`, setting
  `partial`/`turnComplete` from the marking. The old merged path emitted live
  events with neither flag set, which ADK's `runLive` consumers cannot tell
  from finals, and under ADK >= 1.5 each such event also costs a
  `sessionService.appendEvent`. Egress ordering moves into the net with it: a
  burst of frames is admitted to the marking in one pass and each enabled
  transition then fires at most once per pass, so a terminal transition
  enabled alongside queued chunks emits between them. Inhibit the terminal on
  the chunk place and the decode callback stays fire-and-forget.
- **Executor wiring**: `PetriRunner.Builder.deferredExecutorRef(...)`
  populates streaming subnet executor references before the orchestrator
  starts, removing the manual post-build `AtomicReference#set` ordering trap.
- **API surface**: normal turn-based mode is stable for 1.x; SSE streaming
  and BIDI/live are beta within 1.x. Both surfaces are marked
  `@Experimental` in source. `LiveConnection` remains genai
  Live-message typed by design.
- **Registry cleanup**: removed the deprecated no-arg
  `SessionExecutorRegistry()` constructor; use `strongOwned()` or
  `cleanerOwned()` explicitly.
- **Dependencies (second bump this release)**: libpetri `2.12.0` ->
  `3.0.1` (a major, spanning 2.13.0/2.14.0/3.0.0/3.0.1) and ADK `1.7.0`
  -> `1.8.0`. genai stays `1.58.0` and protobuf stays `4.33.5`, because ADK
  1.8.0's POM differs from 1.7.0's on the version line alone. Test-scoped:
  JUnit `6.1.2` -> `6.1.3`, `opentelemetry-sdk-testing` `1.64.0` ->
  `1.65.0`, surefire `3.5.5` -> `3.5.6`. ADK 1.8.0 needed no adaptation
  (zero new files; our whole touched surface byte-identical bar
  `Runner`, whose `runLive` append path is unchanged). Both bypass
  rationales re-verified against 1.8.0 sources and all four foils green.
  Details in [ADR 0003](docs/adr/0003-libpetri-3-and-adk-1.8.md).
- **Verification is no longer vacuous or skeletal** (test-facing). libpetri
  CORE-043 rejects a transition that declares an output while carrying
  `passthrough()`, which caught nine sites analysing *unbound* nets,
  proving properties about nets whose transitions could never fire. Every
  site now verifies the bound net it actually runs. Separately,
  `VoiceSessionDemoTest`'s and `LlmStreamingStepSubnetTest`'s budget bounds
  were returning `Unknown` ("a proof would be vacuous") because their
  environment places were unmodelled; both now pass
  `environmentMode(EnvironmentAnalysisMode.bounded(1))` and return
  `Proven`. Six assertions moved from `isViolated()==false` (which also
  passes on `Unknown`) to `isProven()==true`, so a future downgrade fails
  loudly instead of silently outliving the claim.
- **Action failures are no longer silent.** libpetri 2.13 contains an action
  failure to its transition and loses that transition's consumed tokens
  (EXEC-031); its default WARNING is suppressed whenever an `EventStore`
  recorded the failure, and this builder defaults to `EventStore.noop()`,
  whose append succeeds while recording nothing. The built-in
  `ExecutorFactory` implementations now install an `ActionFailureHandler`
  that logs. No new builder setter: `ExecutorFactory` is a public extension
  point and a setter would have to widen its signature.
- **`PetriRunner.Builder.actionExecutor(...)` deprecated and no longer
  required.** It never ran actions. libpetri hands that pool exactly one
  task, and only under `run(Duration)`, which this runner never calls;
  actions are invoked inline on the orchestrator thread. Put your
  virtual-thread executor on `orchestratorExecutor(...)`, which is the pool
  that actually runs them. Still accepted so existing callers compile;
  removal in 2.0. Pinned by
  `PetriRunnerTest.actions_run_on_the_orchestrator_executor_not_the_action_executor`.
- **`SyncGeminiLlm` exemplar drops the Gemini 3 stream terminator.** ADK
  1.8.0 started filtering the bare empty-text part that ends a Gemini 3
  stream, but that fix lives in ADK's streaming accumulator, which the
  exemplar deliberately bypasses; unfiltered it surfaced as a spurious
  empty partial `Event`.
- **Dependencies**: libpetri floor raised `2.7.1` -> `2.12.0`
  (consumer-visible: the `deferredExecutorRef` streaming wiring and
  `PrecompiledNetExecutor` executor choice need `2.10.4`; `2.11`/`2.12`
  add the opt-in EXTENDED ν-fragment and the conflict-priority state-class
  graph, plus the P-semiflow colour-bound soundness fix, all with
  unchanged defaults). ADK floor raised `1.4.0` -> `1.7.0` (pulls genai
  `1.58.0`; the protobuf `4.33.5` floor is unchanged, ADK 1.7.0 pins the
  same). Test-scoped tooling bumped: JUnit `6.0.3` -> `6.1.2`,
  `opentelemetry-sdk-testing` `1.51.0` -> `1.64.0`. The ADK 1.4 -> 1.7 semantic
  delta, the claims re-verified against 1.7.0, and the procedure for the next
  bump are recorded in
  [ADR 0002](docs/adr/0002-adk-version-compat.md).

## 1.2.0 - 2026-06-04

### Java

Replaces Google ADK Java's orchestration core (`SequentialAgent`,
`ParallelAgent`, `LoopAgent`, `BaseLlmFlow`, `AgentTransfer`, `Runner`)
with a Coloured Time Petri Net runtime on
[libpetri](https://github.com/debe/libpetri), integrated through a
`PetriAgent extends BaseAgent` adapter. **Zero forks** of ADK or genai.

- **Typed boundary catalog** (`AdkColours`) and seven stock subnets:
  `LlmStepSubnet`, `ToolDispatchSubnet`, `PromptBuilderSubnet`,
  `RouterSubnet`, `LlmAgentSubnet` (with the structural reask-budget
  bound), `PersistStateSubnet` (single race-free legacy-session writer),
  and `TransferRouterSubnet` (`Out.xor` over compile-time-known targets;
  hallucinated names become typed error events, not NPEs).
- **ADK integration**: `PetriAgent`, `PetriRunner`, and
  `SessionExecutorRegistry` (two ownership modes: `Cleaner`-bound and
  strong-owned). Stock `InMemoryRunner` consumes a `PetriAgent` with no
  ADK source change.
- **Observability**: `EventStoreToFlowableBridge` and `OtelEventStore`
  (one OpenTelemetry span per transition fire), composed via the
  `EventStore` decorator chain.
- **Verification**: `AdkNetInvariants` ships three structural validators
  on every build, plus SMT property factories proved via libpetri's
  `SmtVerifier`. The two end-to-end demos (`MultiAgentDemoTest`,
  `VoiceSessionDemoTest`) are Z3-proved deadlock-free, with the BIDI
  demo's reachable state space confirmed finite via bounded state-class
  graph exploration.
- **Calling Gemini without `commonPool`**: the `SyncGeminiLlm` exemplar
  calls genai's synchronous API on a virtual thread; voice reads genai's
  Live session directly. ADK's `Gemini`/`GeminiLlmConnection` wrappers
  are bypassed in thin user code rather than forked.
- **Voice/BIDI** subnets (`BargeIn`, `LiveApiRecovery`,
  `LlmStreamingStep`, `Vad`) ship as composable exemplars under
  `src/test/.../demos/voice/`, not as stock library code.

173 tests across 29 test classes, all passing.
