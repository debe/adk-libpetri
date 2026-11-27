# ADR 0012: ADK Java 1.15.0 and libpetri 11.1

- **Status:** Accepted
- **Date:** 2026-11-27
- **Scope:** Java, unreleased. Bumps google-adk `1.13.0` -> `1.15.0`
  (spanning 1.14.0) and libpetri `10.0.0` -> `11.1.0` (spanning 10.0.1 and
  11.0.0). genai stays `1.77.0`, protobuf `4.33.5`, rxjava `3.1.12`,
  OpenTelemetry `1.51.0`.

## Context

[ADR 0002](0002-adk-version-compat.md)'s re-check procedure, run against
1.15.0. A `javap -protected` diff over every ADK class we import shows no
removed public or protected member. The only additions on our extension
points are concrete: `BaseLlm.capabilities()` and the no-op
`BaseNode.validateOutgoingEdges`. The full build passes on 1.15.0 and
libpetri 11.1.0 (312 tests, z3 proofs included, none skipped) after the
foil change below.

The three bytecode claims in
[java/README.md](../../java/README.md#adk-java-1150-behaviour-the-argument-relies-on)
still hold: `ParallelAgent` is byte-identical to 1.13.0 and still
`Flowable.merge(...).takeUntil(...)`; `endInvocation` is still a private,
non-volatile `boolean` read only in `BaseAgent.runWithCallbacks`,
`BaseLlmFlow.runOneStep` and `BaseLlmFlow.runLive`; `Functions` is
byte-identical and never reads it.

## What moved under us

- **`GeminiLlmConnection.convertToServerResponse` takes a second argument**
  (1.15), the connection's `PendingText` buffer. `VoiceVadEdgeAdkFoilTest`
  reflects on it and now passes a fresh buffer. A VAD-only frame still falls
  through to the unknown-message error, so the foil stays green and
  `VadTapGemini` and `SyncGeminiLiveConnection` stay.
- **Live `partial` means "streams text"** (1.15). `GeminiLlmConnection`
  buffers text, re-emits it as one non-partial response before the end of
  the turn, an interruption or a tool call, and marks audio chunks
  non-partial. The net path reads `LiveConnection.rawReceive()`, not
  `LlmResponse`, so it is unaffected. A `VadTapGemini` user reading
  `receive()` now sees text twice: partial, then in full.
- **`Runner.runLive` no longer appends partial events** (1.15). ADR 0002's
  "each one now costs an `appendEvent`" now holds for non-partial live
  events only. `BaseSessionService.appendEvent` already dropped partials, so
  nothing changes with `InMemorySessionService`.
- **`BaseLlmFlow` defers `generateContent` and cancels the model step on
  dispose** (1.14 `cancellableCache`, 1.15 `Flowable.defer`). A cancelled
  `runAsync`, and an escalation in `ParallelAgent`/`LoopAgent`, now reach the
  sibling's `BaseLlm`; whether the request stops still depends on that
  `BaseLlm`, as `PatternA_AdkOnlyFoilTest` says.
- **`Event.finalResponse()` is also true for an error event** (1.14). We call
  neither it nor `errorCode()`.
- **libpetri 11.0's build-time checks** (an ambiguous `Xor`, a nested
  `Timeout`, an `Xor` inside a `Timeout` child, a bare `ForwardInput`, an
  input and a read arc on one place) reject none of our nets.
  `EncodedScripts.witness` and the bounded witness phase change no verdict
  of ours.
- **libpetri 11.1 drops a write made after the action's future completed**
  (CONC-003). Every `ctx.output` in our subnets runs before the returned
  future completes.

## Defects the bump surfaced, fixed with it

- **A model that throws skipped `OnModelError`.** `LlmStepSubnet`'s `LlmCall`
  called `baseLlm.generateContent(...)` bare, so a `BaseLlm` that threw
  instead of returning `Flowable.error` failed `LlmCall` and never reached
  the recovery callback. It now defers the call, as ADK 1.15's `BaseLlmFlow`
  does. `LlmStreamingStepSubnet` fails the transition either way and is
  unchanged.

## Inert for us

- `RunConfig.dollarEscapedPlaceholders`, `Event.isolationScope`, node-path
  and agent-mode filtering on `InvocationContext`, `BaseAgent.runAsNode`,
  `Context.emit`/`runNode`, node-root resume on `Runner`.
- `GeminiLlmConnection.sendHistory` dropping audio and `_adk_live` parts:
  nothing of ours calls it.
- The `Tracing` default for `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`, now
  `false`: nothing of ours reads ADK span content.
- `LoopAgent` with `maxIterations <= 0` running zero times, and `LlmAgent`
  no longer saving a transfer target's reply under the parent's
  `outputKey`.
- A typed rejection of an unknown transfer target was added and reverted
  within 1.14; `BaseAgent.findAgent` still returns `Optional.empty()`, so
  `TransferUnknownTargetAdkFoilTest` stands.
- libpetri 11.1's EventStore query helpers (`placeEvents`,
  `filter(predicate, offset, limit)`, `counters()`): available, not needed.

## Workarounds that stay

- `SyncGeminiLlm`: `Gemini.sendRequest` still ends in `thenApplyAsync` with
  no executor, and `Runner.Builder.scheduler` still does not cover model
  clients.
- `VadTapGemini` and `SyncGeminiLiveConnection`: no `voiceActivity` handling
  anywhere in 1.15.0. The `connectLiveTransport` seam is unchanged.

## Next-bump procedure

Unchanged ([ADR 0002](0002-adk-version-compat.md#next-bump-re-check-procedure)).
