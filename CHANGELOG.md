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
