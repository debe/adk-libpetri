# ADR 0002: ADK version compatibility and the re-check procedure

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** Java `1.3.0-SNAPSHOT`. Bumps libpetri `2.10.4` -> `2.12.0`,
  google-adk `1.4.0` -> `1.7.0` (which pulls google-genai `1.58.0`).

## Context

The dependency bump from ADK 1.4.0 to 1.7.0 compiled clean and the full suite
stayed green (198 tests, before the regressions this release adds) on the first
try. That is misleading: adk-libpetri
replaces ADK's orchestration core but keeps ADK's *host* contract
(`BaseAgent`, `Runner`, `Event`, `RunConfig`, `LiveRequestQueue`), and this
release changed the semantics of one of those contracts without changing its
signature. A green build proves we still compile, not that our claims still
hold.

Three things needed writing down: what actually changed under us, which
project claims were re-verified against 1.7.0, and the procedure to answer
both questions cheaply at the next bump instead of re-deriving it.

## The touched surface

adk-libpetri imports these host types (from `src/main` plus the test-tree
exemplars). Anything outside this list can change freely without affecting us:

- `com.google.adk.agents`: `BaseAgent`, `InvocationContext`, `RunConfig`,
  `LiveRequestQueue`, `LlmAgent` (demos only)
- `com.google.adk.events`: `Event`, `EventActions`
- `com.google.adk.models`: `BaseLlm`, `BaseLlmConnection`, `LlmRequest`,
  `LlmResponse`, `GeminiUtil`, `GeminiLlmConnection` (foil only)
- `com.google.adk.runner`: `InMemoryRunner` (demos only)
- `com.google.adk.sessions`: `BaseSessionService`, `Session`, `InMemorySessionService`
- `com.google.adk.tools`: `BaseTool`, `ToolContext`
- `com.google.genai`: `Client`, `AsyncSession`, `ResponseStream`, plus
  `types.{Content, Part, FunctionCall, FunctionResponse, LiveServerMessage,
  LiveServerContent, VoiceActivity, Blob, ...}`
- `org.libpetri`: `core`, `event`, `runtime`, `smt.SmtProperty`

**Watch-item:** genai is imported directly but resolved *transitively* through
ADK (`1.58.0`). It is deliberately not declared, so we never pin above ADK's
own pin, but that means an ADK bump silently moves the genai floor. Check it
on every bump (step 2 below).

## What changed 1.4.0 -> 1.7.0, and what it cost us

Established by diffing the two `-sources.jar`s over the surface above.

### Load-bearing: `Runner.runLive` stopped being fire-and-forget

1.4 dropped the append's result (`.doOnNext(event -> sessionService.appendEvent(...))`).
1.7 is `.concatMapSingle(event -> sessionService.appendEvent(...))`, so every
event the BIDI path emits is really persisted and back-pressures the stream.

Combined with a second fact this exposed a real defect: `BidiPetriAgent`
authored its own merged model-content `Event` and set neither `partial` nor
`turnComplete`, while ADK's own `GeminiLlmConnection.createServerContentResponse`
sets `partial`/`turnComplete`/`interrupted` and `BaseLlmFlow.runLive` branches
on them. Our live events were therefore indistinguishable from finals, and each
one now costs an `appendEvent`.

**Fix (this release, breaking a beta API):** `BidiPetriAgent.bridge` lost its
`String author` parameter and no longer maps frames to events at all. It returns
`PetriRunner.adkEvents()` alone; model content enters the net through the
consumer's `onServerMessage` callback (`runner.inject(...)` /
`runner.signal(...)`) and a net transition authors the `Event`, setting
`partial`/`turnComplete` from the marking. Turn shape is now a marking-level
decision, which is both the correct fix and the design commitment (#1,
env-place injection only; #2, the marking is the state). It also puts model
content inside the marking, so barge-in can structurally drop queued chunks the
old merged path could not touch (fourth finding below). See `BidiPetriAgentTest`
and `VoiceSessionDemoTest`'s `Bidi_EmitTurnEnd`.

**Secondary finding, fixed structurally:** moving turn shape into the net moved
egress *ordering* there too, and the first cut got the mechanism wrong. It is
not an acceptance-ordering problem: `BitmapNetExecutor`'s external-event queue
is a `ConcurrentLinkedQueue` drained in full each pass, and the decode callback
runs serially on the transport's reader thread, so injection order is preserved.
The reordering is in *firing*. A burst is admitted to the marking in one pass,
after which `fireReadyImmediate` fires each enabled transition at most once, in
transition-id order. A terminal transition enabled alongside still-queued chunks
therefore emits between them: three chunks and a turn-complete arriving together
come out as `[a, b, TURN_COMPLETE, c]`, reproduced 3/3.

Awaiting each acceptance future does fix it, but only incidentally, by capping
the queue at one pending token so no pass ever sees a burst. That is a
concurrency rule every consumer has to re-implement, and on the `join()` variant
it blocks the transport's reader thread. The actual fix is one arc: inhibit the
terminal transition on the chunk place, so it cannot fire while content is
queued. Ordering then holds with a pure fire-and-forget callback. Regressions:
`BidiPetriAgentTest.a_burst_streamed_turn_yields_every_partial_before_the_terminal_event`
and `VoiceSessionDemoTest`'s `Bidi_EmitTurnEnd`; both fail with the arc removed.

This is the design's own argument applied to itself. A property a stream-merging
orchestrator can only document as a rule for callers is, in a net, an arc that
makes the violation unreachable.

**Third finding, fixed in the same change:** dropping the merge would have
dropped the transport's terminal signals with it. The server stream is therefore
still merged into the returned `Flowable`, as an element-less `Completable`
(`rawReceive().doOnNext(callback).ignoreElements()`), so a `rawReceive` error
still surfaces as `onError` to the caller instead of stranding a consumer
subscribed to a net egress with nothing left feeding it. `adkEvents()` is hot and
never completes, so a completed server stream cannot end the turn by itself.
Regression: `BidiPetriAgentTest.a_transport_error_terminates_the_returned_stream`.

**Fourth finding, now covered:** the claim that net-authored egress lets barge-in
drop queued chunks was asserted in four places and demonstrated in none. It is
now green-locked by
`VoiceSessionDemoTest.barge_in_structurally_drops_the_queued_model_chunks`:
stock `BargeInSubnet` decides the interrupt is real, and the drop transition
hangs a `reset(LLM_RESPONSE)` off its `BARGE_IN_SENT` verdict, wiping the
backlog before it can be emitted. The pre-1.3 merged bridge could not satisfy
that assertion at any marking, because chunks became `Event`s the moment they
left the transport.

### Inert for us (verified, no action)

- **`Event.finalResponse()`** is now also true on a pending long-running (HITL)
  tool call. We author our own `EVENT_OUT` events and gate SSE on `partial()`,
  never on `finalResponse()`.
- **`RunConfig`** gained `avatarConfig`, `customMetadata`, and a deprecated
  `groupFunctionResponsesInHistoryOverride` (Gemini-3 function-call ordering,
  applied in `Contents`, not on our path). `PetriAgent` reads only
  `streamingMode()`; additive.
- **`InvocationContext.InvocationCostManager`** counter became an `AtomicInteger`.
  This confirms `maxLlmCalls` counts *ADK-driven* calls: our net drives the LLM
  itself, so the ADK budget is **inert for net turns**. Autonomous-runaway
  protection comes from the reask-budget subnet pattern (design commitment #6),
  not from `RunConfig.maxLlmCalls`.
- **`Runner.processLastEvent`** switched to
  `checkArgument(newMessage.parts().isPresent())`; equivalent for our inputs.
- **`Functions` / `GeminiUtil` / new `FunctionCallIds`**: client-generated
  function-call ids are now guarded by `isClientGeneratedFunctionCallId` (the
  `adk-` prefix), and streamed FC chunks may carry no name. `ToolDispatchSubnet`
  copies ids straight from `call.id()` and never inspects that prefix, so no
  change, but it is the most likely place a future ADK FC-id change bites.
- **`Runner`/`InvocationContext` resumability + `PersistBarrier`**: new
  machinery for ADK's own multi-step flow (`BaseLlmFlow` waiting for the Runner
  to persist a step before building the next request from `session.events()`).
  We replace that flow, and our state is the marking, not `session.events()`, so
  the whole class of staleness it fixes does not exist for us.
- **`Gemini` shared `OkHttpClient` / `httpExecutorService`**: dispatcher-side,
  not continuation-side. Does not affect the foil below.

## Claims re-verified against ADK 1.7.0

Each of these is a claim the README makes about *stock ADK*. They were
re-checked against 1.7.0 sources, not assumed:

- **`commonPool` hop in ADK's Gemini wrapper**: still there:
  `models/Gemini.java` `generateContent` ends in
  `.thenApplyAsync(LlmResponse::create)` with no executor argument. The
  `SyncGeminiLlm` exemplar's rationale (call genai's sync facade on a virtual
  thread; no fork) is intact.
- **VAD edges dropped by ADK's live wrapper**: still there:
  `GeminiLlmConnection.createServerContentResponse` maps only
  `modelTurn`/`turnComplete`/`interrupted`/transcriptions, so
  `LiveServerMessage.voiceActivity()` never reaches an `LlmResponse`.
  Green-locked by `VoiceVadEdgeAdkFoilTest`.
- **Hallucinated transfer target has no typed-error surface**:
  `BaseAgent.findAgent` still returns `Optional.empty()` with no error routing.
  Green-locked by `TransferUnknownTargetAdkFoilTest`.

## libpetri 2.11 / 2.12: adopted floor, unadopted features

The floor moved to `2.12.0`. The only new public classes on our surface are
`analysis.PrioritySemantics` and `analysis.FragmentMode`; defaults are
unchanged, so the bump is behaviour-neutral for us.

**Follow-up (not done here):** `PrioritySemantics.CONFLICT` prunes the
interleavings the eager priority-ordered executor never produces, removing
spurious drain-steal stalls from the Route B state-class graph. The candidate
site is `VoiceSessionDemoTest`'s bounded-exploration assertion. Deferred
because the demo is already green under the default and the change would trade
a passing assertion for a tighter one with no current failure to justify it.

## Next-bump re-check procedure

Cheap, ordered, and stops early when nothing on our surface moved.

1. **Diff the sources jars over our surface.** Fetch
   `google-adk-<old>-sources.jar` and `google-adk-<new>-sources.jar` from
   Central, unzip both, `diff -rq` them, and intersect the changed-file list
   with "The touched surface" above. Files outside it need no reading.
2. **Re-resolve the transitive floors.** `./mvnw dependency:tree` and confirm
   the resolved `google-genai` and `protobuf-java` versions. protobuf must stay
   at or above ADK's own pin (gencode contract; see the README section). ADK
   1.7.0 pins `4.33.5`, which is why we do too.
3. **Read the diffs for behaviour, not signatures.** A signature-compatible
   change to *when* something happens (this release: `doOnNext` ->
   `concatMapSingle`) is the failure mode a green build cannot catch. Pay
   specific attention to `Runner`, `BaseLlmFlow`, and `Event`'s
   partial/turnComplete/finalResponse predicates.
4. **Re-run the foils.** `./mvnw test -Dtest="*AdkFoil*"`. These assert that
   stock ADK still behaves the way the README's argument requires. A foil going
   red is good news about ADK and means the README claim needs updating, not
   the test silencing.
5. **Re-confirm the two bypass rationales by grep**, since neither has a test:
   `Gemini.java` still `thenApplyAsync`-hops in `generateContent`, and
   `GeminiLlmConnection` still has no `voiceActivity` mapping.
6. **Full suite**, then update this ADR's "what changed" section with the new
   delta and the version in Scope.

## Consequences

- The BIDI/live surface took a source-breaking change inside `@Experimental`
  (`bridge` lost a parameter). Marvin is the first consumer and adopts the new
  seam; the beta fencing is what made this affordable.
- The net now owns live turn shape end to end. `partial`/`turnComplete` are set
  by whichever transition fired, which is the property the design wanted and
  the merged-egress shortcut had quietly bypassed.
- Consumers get no new concurrency obligation. Turn ordering is an inhibitor arc
  in the consumer's own net, not a rule about how to await injections, so the
  decode callback stays fire-and-forget on the transport's reader thread. A
  consumer that wires a terminal transition without that arc will see a
  reordered turn under burst, which is why both live tests fail with the arc
  removed rather than merely documenting it.
- Version-drift review has a written procedure, so the next bump is a
  checklist rather than a re-derivation.
