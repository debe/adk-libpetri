# ADR 0003: libpetri 3.0.1 and ADK 1.8.0

- **Status:** Accepted
- **Date:** 2026-08-21
- **Scope:** Java `1.3.0-SNAPSHOT`. Bumps libpetri `2.12.0` -> `3.0.1` (a
  major, spanning 2.13.0, 2.14.0, 3.0.0 and 3.0.1), google-adk `1.7.0` ->
  `1.8.0` (which folds in 1.7.1). Test-scoped: JUnit `6.1.2` -> `6.1.3`,
  `opentelemetry-sdk-testing` `1.64.0` -> `1.65.0`, surefire `3.5.5` ->
  `3.5.6` (libpetri parity).

## Context

[ADR 0002](0002-adk-version-compat.md) wrote down a re-check procedure so the
next bump would be a checklist rather than a re-derivation. This is that
checklist executed. It worked: every step produced a specific answer, and two
of them produced findings a green build would not have surfaced.

The asymmetry this time is the reverse of last time. ADK moved barely at all.
libpetri took a major version carrying one compile-time break, five
executor-semantics changes, and a verifier that now self-checks its verdicts.

## Dependency resolution (step 2)

`./mvnw dependency:tree` confirms nothing else moved: google-genai stays
`1.58.0`, protobuf-java stays `4.33.5`, opentelemetry-api stays `1.51.0`.
ADK 1.8.0's POM differs from 1.7.0's on the `<version>` line alone, so the
protobuf floor and the README's consumer guidance are correct verbatim; only
the ADK version named in the prose changed. libpetri 3.0.1's own POM is
likewise unchanged from 2.12.0 apart from its version (Z3 4.16.0, Guava
33.6.0-jre, Jackson 2.22.0), so there was no convergence work.

genai remains deliberately undeclared and transitive, per ADR 0002's
watch-item. It did not move under us this time.

## ADK 1.7.0 -> 1.8.0: nothing on our surface

1.8.0 adds **zero new files**: 251 `.java` in both releases, and `diff -rq`
produces no `Only in` lines. No new agent types, no resumability API, no new
a2a classes. Of our touched surface, `BaseAgent`, `InvocationContext`,
`LiveRequestQueue`, `LlmAgent`, `Event`, `BaseLlm`, `BaseLlmConnection`,
`LlmRequest`, `LlmResponse`, `GeminiUtil`, `InMemoryRunner`,
`BaseSessionService`, `Session`, `BaseTool`, `ToolContext`, `BaseLlmFlow`,
`FunctionCallIds` and `Functions` are **byte-identical**. `RunConfig`'s only
diff is indentation inside a license comment.

Critically, `Runner.runLive` still ends in
`.concatMapSingle(event -> this.sessionService.appendEvent(session, event))`.
The behaviour change that forced 1.3.0's BIDI rebuild is still there, so that
work stands.

### Both bypass rationales re-verified (step 5)

Neither has a test, so both were re-confirmed by reading 1.8.0 sources:

- **`commonPool` hop in ADK's Gemini wrapper: still there.**
  `models/Gemini.java:296`, byte-identical to 1.7.0:
  ```java
  .generateContent(effectiveModelName, llmRequest.contents(), config)
      .thenApplyAsync(LlmResponse::create));
  ```
  No executor argument. `SyncGeminiLlm`'s rationale is intact.
- **VAD edges dropped by ADK's live wrapper: still there.**
  `grep voiceActivity GeminiLlmConnection.java` returns nothing in 1.8.0;
  `createServerContentResponse` maps only
  `modelTurn`/`turnComplete`/`interrupted`/transcriptions. The frame falls
  through to `logger.warn("Received unknown or empty server message")` and is
  emitted as a blank unknown `LlmResponse`, which is what
  `VoiceVadEdgeAdkFoilTest` already asserts (`errorMessage()` present,
  `content()`/`turnComplete()` empty). The foil needed no strengthening.

All four `*AdkFoil*` tests green (step 4).

### Portable lessons, checked against our own code

1.8.0's fixes are mostly in ADK's own wrapper layers. Each was checked for the
same defect class on our paths rather than assumed inapplicable:

- **`Contents.isInvisiblePart` now keeps `thoughtSignature` parts visible.**
  Inert for us, and not merely because we build requests from the marking:
  `PromptBuilderSubnet` is stateless and single-turn and passes the user's
  `Content` through without filtering parts at all, so there is no place for
  the defect to live.
- **`SyncGeminiLlm` does not inherit ADK's 1.8.0 streaming fixes, and one of
  them mattered.** The thought-signature reattribution fixes are moot (the
  exemplar has no accumulator; it maps each chunk straight through). But
  1.8.0 also started dropping the bare empty-text part Gemini 3 ends a stream
  with (`Gemini.isStreamTerminator`), and that fix lives in the accumulator we
  bypass. Unfiltered, the terminator reaches the net as a chunk and is emitted
  as a spurious empty partial `Event`. Mirrored in the exemplar as one
  `.filter(...)`; covered by
  `SyncGeminiLlmTest.gemini3_stream_terminator_is_dropped_but_real_text_is_kept`.
- **`EventActions.Builder.merge` was overwriting `endOfAgent` instead of
  OR-ing it.** Inert: our only `EventActions` use is
  `PersistStateSubnet.persistAction`, which builds a fresh `stateDelta` event
  and never merges.
- **`RequestConfirmationLlmRequestProcessor` hardened against an A2A peer
  injecting a function call that a local tool then executes**, by validating
  id + author + name + args against a call the agent actually emitted.
  `ToolDispatchSubnet` copies `call.id()` verbatim and never inspects the
  `adk-` prefix. Not on our threat path (no A2A peer authors events into the
  net), but ADR 0002 already flagged FC-id handling as the most likely future
  break, and this is the shape it would take.
- **`Plugin.onRunErrorCallback`** is 1.8.0's only new API. ADK's `Runner` owns
  `PluginManager`; `PetriAgent` sits below it as a `BaseAgent`. Inert.
- Also inert: `Runner.appendNewMessageToSession` no longer mutating the
  caller's `Content` (a fix we get for free via the stock Runner),
  `InMemorySessionService.getSession` now composing `numRecentEvents` with
  `afterTimestamp`, and `BigQueryLoggerConfig`'s changed defaults.

## libpetri 2.12.0 -> 3.0.1: what it cost us

### CORE-043 (2.14.0): the only compile-time break, and it was ours

A transition declaring an `Arc.Out` spec while carrying `passthrough()` is now
rejected, at `CompiledNet.compile`, `SmtVerifier.verify()` **and**
`StateClassGraph`. Nine tests failed on the first run, every one of them at
`SmtVerifier.verify()` or `StateClassGraph.build(...)`, and every one for the
same reason: they analysed an **unbound** net.

That is a real defect the check found, not a false positive. Verification was
being run against a skeleton in which the transitions could never have fired,
so the properties were being proven about a net nobody could execute. The fix
is uniform: bind the actions the net actually runs with, then verify.

- The three pattern demos now verify `buildNet().bindActions(buildBindings(...))`,
  the same bound net their execution tests drive.
- `MultiAgentDemoTest`'s deadlock-freedom check binds `LlmAgentSubnet` and
  `TransferRouterSubnet` actions.
- `LlmStreamingStepSubnetTest`, `VoiceSessionDemoTest` and
  `LiveApiRecoverySubnetTest` bind their composed subnets.
- `AdkNetInvariantsTest`'s two synthetic `Seed` nets bind
  `TransitionAction.fork()`, which is exactly the "moves its input token
  across" case libpetri's error message names.

Stock subnets were never at risk: `SubnetActions.bind` already requires
bindings to cover the declared transitions exactly, and CORE-043 does not fire
at `SubnetDef.builder().build()`, so the static `DEF` fields still initialise.

### EXEC-003 (3.0.1): no fallout, but it was the one to watch

Within a firing pass, tokens produced by a synchronous action are no longer
visible to the enablement recheck of transitions still waiting to fire, to
`exactly(n)`/`atLeast(n)` gates, or to a draining arc (which now takes only
the pass-start prefix). **Both Java backends were affected, including
`BitmapNetExecutor`**, the one every test here runs on.

Two adk nets sit directly on the reset-vs-producer shape:
`LlmStreamingStepSubnet`'s `SeedAndStart` resetting `CHUNK_BUDGET` that
`EmitChunk` refills, and `VoiceSessionDemoTest`'s `barge-in-drop` net
resetting the concurrently-injected `LLM_RESPONSE`. Both are green, and the
barge-in claim was re-checked as a negative control rather than assumed:
deleting the `reset` arc still turns
`barge_in_structurally_drops_the_queued_model_chunks` red. Likewise
`BidiPetriAgentTest.a_burst_streamed_turn_yields_every_partial_before_the_terminal_event`
still fails with its inhibitor arc removed, so the ordering guarantee ADR 0002
introduced still rests on the arc and not on incidental pass timing.

### Inert for us (verified, no action)

- **`NetExecutor` deleted (3.0.0).** Zero references; the runner has always
  typed against `PetriNetExecutor`. The headline removal cost nothing.
- **CORE-030, two input arcs on one place.** No transition anywhere chains
  more than one `.inputs(...)`, and every multi-arg `.inputs(...)` uses
  distinct places. Confirmed by a clean compile of every net under test.
- **MOD-021 fusion/channel arc merging.** Zero fusion-set and zero
  channel-composition call sites.
- **`SmtVerificationResult` gained a record component.** Never constructed or
  destructured here; all sites use `var`.
- **`Out.ForwardInput` now forwards one token per token consumed** (it
  forwarded one in total before, silent data loss). Unused here.
- **CORE-072, tokens on undeclared places are retained rather than dropped or
  fatal.** No test expected the old `Unknown place` throw.
- **EXEC-002 and EXEC-013 AC4** are `PrecompiledNetExecutor`-only fixes. See
  the deferral below for why that matters more than it looks.
- **`close()` only shuts down an executor it created (2.13).** Inert:
  `SessionExecutorRegistry` creates no executors (callers supply them), and
  `PetriRunner.shutdown()` drains and awaits the orchestrator task rather than
  shutting a pool down. Design commitment #8's teardown route is unaffected.

## Two findings the version numbers did not predict

### 1. `actionExecutor` never ran actions, and the README pointed at it

libpetri 2.13 corrected its own documentation: the `ExecutorService` on its
builder *"never dispatched actions; it only hosts the orchestrator loop under
`run(Duration)`"*, and actions are invoked **inline**:
`action.execute(ctx)` is called directly, never submitted.

`PetriRunner` submits the no-arg `executor::run` to its *own*
`orchestratorExecutor` and never calls `run(Duration)`. So
`PetriRunner.Builder.actionExecutor` fed a pool that, on this path, was used
for nothing at all, while every transition action, including
`SyncGeminiLlm`'s blocking genai call, ran on the `orchestratorExecutor`,
which `java/README.md` told users to make a `newSingleThreadExecutor()`.

The "no `commonPool`" argument survives intact (nothing hops to `commonPool`
either way), but the thread it named was wrong, and the recommended wiring
serialised the net on a pool the docs presented as incidental.

Rather than argue from the javadoc, this is now pinned by
`PetriRunnerTest.actions_run_on_the_orchestrator_executor_not_the_action_executor`,
which runs a net with two distinguishably-named single-thread pools and
asserts which one the action observes.

Resolution:
- `actionExecutor(...)` is `@Deprecated(since = "1.3", forRemoval = true)` and
  **no longer required**, so callers can simply drop it
  (`PetriRunnerTest.runner_starts_without_an_action_executor`). It is still
  accepted and still passed through, so existing callers keep compiling.
  Marvin is a live consumer and this is stable 1.x surface, not the
  `@Experimental` BIDI seam. Removal in 2.0.
- `orchestratorExecutor(...)`'s javadoc now states that actions run on it and
  that a virtual-thread executor is the right choice when they block.
- `README.md` and `java/README.md` were corrected accordingly, including the
  contrast with `ToolDispatchSubnet`'s `dispatchExecutor`, which is a genuine
  worker pool precisely because that action submits to it explicitly.

### 2. An action that threw was silent under our default wiring

libpetri 2.13 contains an action failure to the failing transition: the
orchestrator survives and the tokens the action consumed are lost (EXEC-031).
libpetri's default policy logs a WARNING, **but only when no `EventStore`
recorded a `TransitionFailed`** for that failure. `PetriRunner.Builder`
defaults to `EventStore.noop()`, whose append succeeds while recording
nothing, so libpetri considered every failure observed and stayed quiet, and
a throwing action became a hole in the marking with no trace anywhere.

That is precisely the failure mode design commitment #2 ("the marking IS the
state") exists to prevent. The built-in `ExecutorFactory.bitmap()` and
`.precompiled()` now install an `ActionFailureHandler` that logs a WARNING
naming the transition and the token loss. Pinned by
`PetriRunnerTest.a_throwing_action_is_reported_and_does_not_kill_the_orchestrator`,
which fails when the handler is removed.

This is deliberately **not** a new builder setter. `ExecutorFactory` is a
documented public extension point ("supply your own factory"), so giving the
handler a setter would mean widening its signature and breaking every caller
who implements it. Callers wanting different handling supply their own
factory, which is what that seam is for.

## Verifier verdicts: assertions that could not fail

libpetri 3.0.1 discharges an IC3 certificate before returning `Proven`,
replays every counterexample before returning `Violated`, and drops
P-invariants it cannot re-derive in exact arithmetic (Java had a live defect
here: invariants computed in unchecked `int` emitted corrupted weights past 32
bits). Anything that fails to re-validate is downgraded to `Unknown`.

Six of eight assertion sites read `assertThat(result.isViolated()).isFalse()`,
which **passes on `Unknown`**. So a downgrade would have left the suite green
while the README's "Z3 proves the composed net is deadlock-free" quietly
stopped being true. Every such site is now `assertThat(result.isProven()).isTrue()`.

Measuring the verdicts before tightening turned up a pre-existing problem that
had nothing to do with this bump:

```
Unknown[reason=environment places present but not modeled (mode=ignore);
        a proof would be vacuous, use EnvironmentAnalysisMode.alwaysAvailable()
        or bounded(k) to model external injection]
```

`VoiceSessionDemoTest`'s `CHUNK_BUDGET` bound and
`LlmStreamingStepSubnetTest`'s were **proving nothing at all**, and had not
been since before 2.12.0; the vacuity guard exists at that version too. The
weak assertion is what let it sit unnoticed. Both now pass
`.environmentMode(EnvironmentAnalysisMode.bounded(1))` and come back genuinely
`Proven`. That is the right fix rather than a workaround: design commitment #1
makes env-place injection *the* interaction model, so a verifier that ignores
env places is not modelling this system.

Final state: all six sites return `Proven`, and the two `AdkNetInvariantsTest`
canaries (`isProven()` true, `isViolated()` true) still hold. No verdict
anywhere needed `certificateCheck(false)` or `counterexampleReplay(false)`;
both stay at their defaults, because a downgrade means the old verdict was
never checked.

## `PrioritySemantics.CONFLICT`: still deferred, now for a stated reason

ADR 0002 deferred adopting `PrioritySemantics.CONFLICT` to prune spurious
drain-steal stalls from the Route B state-class graph. Worth recording that
`analysis.PrioritySemantics` and `analysis.FragmentMode` **already existed at
2.12.0 with identical signatures**, so this bump adds nothing there and 0002's
deferral was never contingent on it. Re-evaluated here anyway, since the
verdicts were being touched regardless: no site reports a stall, and every
property that should prove now proves under the default. Adopting it would
trade a passing assertion for a tighter one with no failure to justify it.
`FragmentMode.EXTENDED` remains unadopted; no adk net is on the ν-coloured
Route B path.

## Consequences

- The verification story is materially stronger than before the bump, and not
  because libpetri got better at proving things. Nets are now verified bound
  rather than as skeletons, env places are modelled instead of ignored, and
  the assertions fail on `Unknown` instead of accepting it. Two demos that
  claimed a proof were not making one.
- `PetriRunner` grew a deprecation and lost a required builder argument.
  Consumers can delete their `actionExecutor(...)` call and should move their
  virtual-thread executor onto `orchestratorExecutor(...)`.
- Action failures are no longer silent on the default wiring.
- ADK 1.8.0 cost nothing and required no adaptation. The one thing it did
  teach us (the Gemini 3 stream terminator) only reached us because we
  bypass the wrapper that now handles it, which is the standing trade of
  design commitment #4 and worth re-checking at each bump.
- 205 tests, all green, none skipped (Z3 present, so the SMT tests genuinely
  ran rather than silently skipping past exactly the checks that mattered).

## Deferred

- **`PrecompiledNetExecutor` still has zero test coverage**: one call site,
  `PetriRunner.java`'s opt-in `ExecutorFactory.precompiled()`. libpetri 3.0.x
  closed *five* divergences between it and the reference `BitmapNetExecutor`,
  two of which (EXEC-002, EXEC-013 AC4) are recorded above as "inert for us"
  purely because we never exercise that backend. That is a liability, not a
  reassurance: an opt-in production executor nothing tests. A differential
  test (same net, both factories, same event sequence) is the follow-up.
- Maven wrapper `3.9.11` -> `3.9.16`, in lockstep with libpetri.
- `maven-compiler-plugin` 4.x and `maven-source-plugin` 4.x once out of beta.

## Next-bump procedure

Unchanged. ADR 0002's six steps are still the procedure; this ADR is what
running them produces. One addition worth carrying forward: **step 3 should
measure verdicts, not just check that nothing turned red.** Printing the
actual `Verdict` at each `SmtVerifier` site is what exposed the vacuous
proofs, and no assertion in the suite would have caught them.
