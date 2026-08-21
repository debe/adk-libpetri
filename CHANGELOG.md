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
