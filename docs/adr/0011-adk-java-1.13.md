# ADR 0011: ADK Java 1.13.0

- **Status:** Accepted
- **Date:** 2026-10-28
- **Scope:** Java, unreleased. Bumps google-adk `1.10.1` -> `1.13.0`
  (spanning 1.11.0 and 1.12.0). genai moves `1.58.0` -> `1.77.0`
  transitively; protobuf stays `4.33.5`, rxjava `3.1.12`.

## Context

[ADR 0002](0002-adk-version-compat.md)'s re-check procedure, run against
1.13.0. A `javap -protected` diff over every ADK class we import shows no
removal except members that moved to a superclass, so the bump is source and
binary compatible. The full build passes unchanged on 1.13.0 (297 tests, z3
proofs included), and so do the foils.

## What moved under us

- **`BaseAgent extends com.google.adk.workflow.BaseNode`** (1.12), and
  `name()`/`description()` now live on `BaseNode`. `ToolContext`'s
  `functionCallId`, `toolConfirmation`, `requestConfirmation`, `actions` and
  `searchMemory` moved to `agents.Context`. `PetriAgent`'s 5-argument
  `BaseAgent` constructor is unchanged.
- **`ParallelAgent`'s escalate predicate** (1.11) now also requires a direct
  sub-agent as author: a nested agent's escalate no longer stops the
  siblings. Still `Flowable.merge(...).takeUntil(...)`, so the G4 argument
  holds; [java/README.md](../../java/README.md#adk-java-1130-behaviour-the-argument-relies-on)
  is updated.
- **`BaseAgent.run` became `runWithCallbacks`**. It still reads the
  non-volatile `endInvocation`, with `BaseLlmFlow.runOneStep`/`runLive`. The
  new `workflow` package never reads it.
- **`Gemini.generateContent`'s `commonPool` hop** moved to the private
  `Gemini.sendRequest` (`thenApplyAsync` with no executor). The new
  `Runner.Builder.scheduler` does not cover it, so `SyncGeminiLlm` stays.
- **`GeminiLlmConnection` still drops VAD edges** (no diff in
  `convertToServerResponse`), so `VadTapGemini` and
  `SyncGeminiLiveConnection` stay.
- **Live sends gained `partial`** (1.13). `LiveRequest.isPartial()`,
  `LiveRequestQueue.content(content, partial)` and
  `BaseLlmConnection.sendContent(content, partial)` (default: ignore the
  flag) let a live client leave its turn open; `GeminiLlmConnection` sends
  `turnComplete = !partial`. `BidiPetriAgent.bridge` dropped the flag by
  calling the one-argument `sendContent`; it now forwards it, and
  `SyncGeminiLiveConnection` implements the two-argument form.
- **`Session.events()` is deprecated** (1.11) in favour of
  `immutableEvents()`. Only the test tree called it.

## Defects the bump surfaced, fixed with it

- **Net-authored events had no id.** ADK's `Event.build()` assigns none, and
  `PetriAgent` stamped only the invocation id. ADK reads ids back from the
  session: `InvocationContext` compares them when a stock `LlmAgent` resumes
  in the same invocation, so a `PetriAgent` event there would fail. The
  adapter now stamps `ctx.newUuid()` where the net left none.
- **Every tool call shared one `ToolContext`.** `ToolDispatchSubnet` took a
  `Supplier<ToolContext>`, which cannot tell calls apart, so a tool never saw
  its own `functionCallId`, and `Context.requestConfirmation` throws without
  it. Tool dispatch now takes a `Function<FunctionCall, ToolContext>`
  (`LlmAgentSubnet.Config.toolContext`); the `Supplier` forms remain as
  adapters. Confirmation flows still need more: the dispatch does not
  surface the context's `EventActions`.

## Inert for us

- **Invocation pause/resume** (`isResumable`, agent-state checkpoints). Our
  resume is the marking (`SessionCheckpointStore`), and ADK's checkpoint
  hooks are package-private.
- **`Runner.Builder.scheduler`**. It schedules ADK's flows, which the net
  replaces.
- **The Gemini context-cache manager** and Kotlin interop.
- **`InMemorySessionService.DuplicateSessionIdBehavior`**. The no-argument
  constructor keeps `OVERWRITE` and is not deprecated; no test creates a
  session id twice.
- **The graph `Workflow`** (`Workflow`, `Edge`, `JoinNode`, `NodeConfig`,
  `Runner.node()`). It is enough for a Java `from_workflow` like Python's
  ([ADR 0006](0006-python-port-and-adk-python-compat.md)); that port is
  future work with its own ADR.

## Watch items

- **OTel parenting.** Since 1.11 ADK composes its trace context with
  `Tracing.withContext` instead of `span.makeCurrent()`. `PetriAgent` opens
  `petri.invocation` from `Context.current()`, so the span's parent should
  still be ADK's `invoke_agent` span. No test pins this: ADK traces through
  the global OpenTelemetry instance, and setting that in one test leaks into
  every other.

## Next-bump procedure

Unchanged ([ADR 0002](0002-adk-version-compat.md#next-bump-re-check-procedure)).
