# Changelog

All notable changes to this project will be documented in this file.

Format: per-language sections under each version. Tags are language-
prefixed (e.g. `java/v0.4.0`).

Versioning is 0.x: the boundary colour catalog, the stock subnet set and
the adapter shape are still moving, so a minor may break API. Releases
before 0.4.0 were numbered in an unpublished 1.x line and were renumbered
down when the project was first published; no 1.x tag or artifact ever
existed.


## Java 0.4.0 - 2026-08-21

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
- **API surface**: the project is 0.x, so a minor may break API anywhere.
  The normal turn-based mode is the settled part; SSE streaming and
  BIDI/live move faster and are marked `@Experimental` in source, meaning
  they may change incompatibly in any release. `LiveConnection` remains
  genai Live-message typed by design.
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
- **Test-tree audit.** The demos are presented as documentation, so an
  audit was run over the 37 test classes as well. It found three tests
  that could not fail and one demo teaching a workaround for a problem
  that does not exist:
  - **`VoiceSessionDemoTest` taught a bypass.** An `injectVoid` helper
    reached through `runner.executor().inject(...)` at 8 sites, justified
    by a comment claiming `PetriRunner.inject()` rejects null tokens.
    `PetriRunner.signal(Place<Void>)` exists for exactly this and its own
    javadoc says "there is no reason to drop to `executor()` for
    signals". All 8 sites now use it. This mattered because design
    commitment #1 is env-place injection only, and the demo that exists
    to teach it was violating it in letter.
  - **The bounded-state proof was vacuous.** `assertThat(scg.size()).isAtMost(256)`
    asserts `StateClassGraph.build`'s own cap argument back at itself: an
    unbounded net truncates at exactly 256 and the assertion still
    passes, while the README claims this confirms a finite reachable
    state space. Now `assertThat(scg.isComplete()).isTrue()`, which is
    false precisely when exploration was truncated. The net is genuinely
    complete, so the claim was true but untested.
  - **`strong_mode_does_not_evict_when_owner_is_collected` tested
    nothing**, and could not: STRONG mode stores `OwnerRef.Strong`, so
    the registry pins the owner and it is never collected. The helper it
    waited in returned silently on timeout. Rewritten as
    `strong_mode_pins_the_owner_so_gc_can_never_evict`, asserting the
    real (stronger) guarantee; it goes red if `Strong` is swapped for
    `Weak`. Both GC helpers now throw on timeout rather than returning.
  - **The two flagship `MultiAgentDemoTest` assertions were
    `assertThat(events).isNotEmpty()`** on a stream where ADK emits the
    user-message event unconditionally, so both passed with the net
    producing nothing. They now assert the agent's actual reply and that
    the hallucinated agent name really surfaces as a typed error event.
  - **Deprecated `actionExecutor` removed from 23 call sites** across 13
    demo and test files, plus the `SyncGeminiLlm` prose. Every exemplar
    was teaching a parameter that is deprecated for removal and inert.
  - Flaky-by-construction timing removed where it was a synchronisation
    hack rather than a simulated delay: a sleep landing exactly on the
    reconnect deadline, and a 300ms sleep the same file already knew how
    to replace with an await on the marking. Simulated latency in the
    race and quorum demos is left alone; there the sleep is the scenario.
  - Leaks closed: six registries in `PetriAgentIntegrationTest` were
    never closed (teardown was left to GC, which a test JVM does not
    guarantee), and `ScrollAwareDemoTest` created an anonymous
    single-thread executor that was never shut down.
- **Implementation hygiene pass.** A static sweep (lizard, PMD, CPD) over
  `src/main` found no TODO/FIXME/HACK, no mutable static state, no raw
  types and no stray `printStackTrace`, but four implementation defects
  were worth fixing before publishing:
  - **`ToolDispatchSubnet` caught `Throwable`, so it caught `Error`.** An
    `OutOfMemoryError` was converted into a structured "the tool failed"
    response and handed to the model while the JVM was going down, and
    the one class of failure that must reach the orchestrator was
    swallowed. Now catches `Exception`, matching libpetri, which rethrows
    `Error` for the same reason.
  - **`OtelEventStore` recorded failures off-spec.** `exception.type` and
    `exception.message` were set as attributes on the span itself;
    OpenTelemetry models a failure as an `exception` span *event* carrying
    those attributes, so no backend looked where they were being written
    and a failed transition never rendered as an exception in any UI.
  - **`emitSpan` took seven positional arguments, four of them adjacent
    `String`s.** Any two could be transposed and still compile, producing
    telemetry wrong in a way nothing would catch. Replaced by a named
    `SpanRecord`.
  - **`llmCallStreamAction` was the largest method in the codebase** (60
    NLOC, CCN 8) with three levels of callback nesting and the error path
    repeated five times. Split into a guard, `streamChunks` and
    `completeStream` (CCN 2/1/6), with the completion path expressed as a
    chain. Structured concurrency would say it better but is still a
    preview API in Java 25, and this module builds without preview
    features. Behaviour is unchanged.
  - `RouterSubnet.findTransferCall` returned a `null` sentinel in a
    codebase that models absence with `Optional` everywhere else.
- **API quality pass before the first release.** A domain-split audit
  (runner; subnet; bridge+colours+verify), with every finding attacked by
  an adversarial reviewer, produced 21 verified findings. The ones that
  would have been frozen by publishing are fixed here:
  - **The turn-based path now stamps ADK's invocation id.** Only the SSE
    branch did. On the default path the id came from the subnet's
    `invocationIdSupplier`, which defaults to a fresh random UUID *per
    event*, so a reply was persisted under an id unrelated to the message
    it answered and to the span opened for the turn. Asserting "one
    distinct id per turn" does not catch this, because the net is
    consistently wrong; the regression pins that the emitted id is not the
    net-local one.
  - **`AdkColours.RAW_PROVIDER_REQUEST` / `RAW_PROVIDER_EVENT` and their
    `RawProvider*` records are removed** (breaking). They were
    `(String feature, Object payload)` bags on two shared global places:
    the exact state-bag shape the catalog's own javadoc forbids, with no
    main-source use, and two unrelated escape hatches declaring
    `In.one` on one place would each be enabled by the other's token.
    Declare a place typed to your feature instead;
    `RawProviderPassthroughDemoTest` now demonstrates that in ~15 lines.
  - **The BIDI bridge closes its `LiveConnection`.** `close()` was only
    reachable from the inbound side, so a consumer that merely cancelled
    (a user hanging up) disposed the pumps and left the websocket open
    with no remaining handle to close it.
  - **`failureSignal()` carries a typed `TransitionFailure`** instead of a
    bare `RuntimeException` whose message concatenated the details, so a
    consumer can branch on `transitionName()`, `kind()` and
    `instancePrefix()` rather than pattern-match `getMessage()`. Deadline
    timeouts (`TransitionTimedOut`) now produce a signal at all; they
    previously fell through and left a turn waiting for a terminal event
    that was never coming. `ActionTimedOut` deliberately does not: the net
    already routed those tokens down their declared timeout branch.
  - **`@Experimental` can now be applied to fields and record
    components.** With `@Target({TYPE, METHOD})` it was a compile error to
    mark a colour or a `Places`/`Transitions` constant, so the beta surface
    could not be fenced where it actually lives.
  - **`LlmAgentSubnet.Config` and `StreamingLlmAgentSubnet.Config` gained
    `callbacks` and `toolContextSupplier`** (breaking for anyone calling
    the canonical record constructor; the builders are source-compatible).
    The composites hard-coded `() -> null` for the tool context and never
    forwarded `LlmStepSubnet.Callbacks`, so through a composite no tool
    could reach state, artifacts or auth and an LLM error was
    unrecoverable. These are record components, so adding them after the
    first release would have been the breaking change instead.
  - **`AdkNetInvariants.reaskBudgetIsBounded` is renamed
    `budgetPlaceBounded(Place, int)`** (breaking): every call in the repo
    passes a chunk budget, not a reask budget. `atMostOneCommits` is
    removed; it was `SmtProperty.mutualExclusion` plus two null checks, so
    callers use the libpetri primitive directly. `eventOutBounded`'s
    javadoc no longer promises "(or any output place)" when it takes no
    `Place`.
  - **`PetriRunner.ExecutorFactory` is documented.** Two javadoc blocks sat
    back to back, so the one describing the factory's contract attached to
    nothing and the interface published undocumented, taking the
    "custom factories MUST propagate `contextProvider`" rule with it. PMD
    had been reporting this as an orphaned javadoc comment.
    `ExecutorFactory.loudActionFailure()` is now exposed so a custom
    factory can reuse the default handler rather than silently going
    without and reintroducing the silent-marking-hole fixed above.
  - **Two false doc statements fixed.** The registry's owner-conflict
    exception advised switching to `strongOwned()`, "which needs no
    external owner at all"; `getOrCreate` calls `requireNonNull(owner)` in
    both modes, and that exception is reachable in `strongOwned()`, so it
    told callers to switch to the mode they were already in.
- **A failed transition no longer kills the session's event stream**
  (behaviour change on a stable surface). `EventStoreToFlowableBridge`
  turned a `TransitionFailed` into `onError` on the runner's
  `PublishProcessor`. That processor is one per `PetriRunner`, so one per
  session, and `onError` is terminal: the first failing transition ended
  the egress permanently. libpetri contains an action failure to its own
  transition and keeps the orchestrator running (EXEC-031), so the net
  went on firing while every later turn on that session received nothing.
  Failures are now published on a separate, non-terminating
  `PetriRunner.failureSignal()`, and `PetriAgent` merges it for the life
  of a turn: a failure fails *that* turn and the session stays usable.
  Removing the `onError` alone would have been worse than the bug, since
  a non-SSE turn waits on a future that the `onError` was the only thing
  completing; it would have hung instead. Regressions:
  `PetriAgentIntegrationTest.a_failed_turn_fails_that_turn_and_leaves_the_session_usable`
  (fails both ways: hangs without the merge, wrong answer without the
  bridge fix) and three in `EventStoreToFlowableBridgeTest`.
- **First published release.** Everything before this version was
  developed in-tree and never tagged or pushed to Maven Central. This is
  the first artifact on Central: `org.libpetri:adk-libpetri:0.4.0`.

213 tests across 37 test classes, all passing, none skipped.

## Java 0.3.0 - 2026-06-04

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
