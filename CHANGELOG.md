# Changelog

All notable changes to this project will be documented in this file.

Format: per-language sections under each version. Tags are language-
prefixed (e.g. `java/v0.4.0`).

Versioning is 0.x: the boundary colour catalog, the stock subnet set and
the adapter shape are still moving, so a minor may break API. Releases
before 0.4.0 were numbered in an unpublished 1.x line and were renumbered
down when the project was first published; no 1.x tag or artifact ever
existed.


## Unreleased

### Python

First Python port (0.1.0, not yet released), for google-adk `~=2.11.0` and
libpetri-py `>=7.2,<8`. See [ADR 0006](docs/adr/0006-python-port-and-adk-python-compat.md).

- **Parity with Java.** Colours, the 9 stock subnets (as `NetSpec`s under
  the Java names), `PetriRunner`, `PetriAgent` (root agent, or a node inside an
  ADK `Workflow`), `SessionExecutorRegistry` (`strong_owned`, and
  `finalizer_owned` on `weakref.finalize`), checkpoints, the SSE pair, the BIDI
  bridge, `AdkNetInvariants` and the stock-subnet Z3 proofs.
- **`from_workflow` (experimental).** Compiles an ADK 2 graph `Workflow` into a
  net served by `PetriWorkflow`, a drop-in for `Runner(node=workflow)`.
  - Translated: routes, `DEFAULT_ROUTE`, `JoinNode`, `max_concurrency`,
    `RequestInput` interrupts (and `auth_config` / tool-confirmation
    interrupts, detected) and opt-in back-edge budgets
    ([ADR 0007](docs/adr/0007-compiled-workflow-back-edge-budgets.md)).
    `retry_config` and `timeout` stay on the node, for ADK's node runner.
  - The compiled node behaves like `Workflow` towards ADK: the terminal
    node's event is the output event, run ids are per workflow run, a
    resumed node keeps its run id, a failing node fails the run, and
    `input_schema`/`output_schema` carry over (so it works as an agent tool).
  - Rejected: nodes reading session state (parameters, `ctx.state` in the
    body, instruction templates) unless `state="legacy_read"`, and
    `mode='task'`/`'chat'` agents, whose across-turn wait is not modelled.
  - `verify_workflow` proves: one turn at a time, the permit never doubles,
    one output per terminal node and at most one terminal node with output,
    every node runs serially, and deadlock freedom for workflows without
    interrupts. Route coverage is reported as a lint.
  - Tests run ADK's own workflow samples (google/adk-python v2.11.0,
    vendored under `tests/workflow/adk_samples`) natively and compiled.
- **Cross-language fixtures.** `spec/fixtures/nets` holds every stock subnet's
  structure. Java's new `SpecFixturesTest` writes and golden-checks it, and so
  does Python's `tests/conformance`.
- **Release.** `scripts/release-python.sh` publishes to PyPI from a developer
  machine, tagging `python/v<version>`.

### Java

- **`SpecFixturesTest`** writes and golden-checks `spec/fixtures/nets/*.json`,
  the structure the Python port is checked against.
- **`ReadmeDiagramsTest` is tracked again.** A `**/docs/` rule in
  `.gitignore` hid its package from git, so CI never ran the diagram golden
  check.
- **Dependencies**: libpetri `3.0.1` -> `8.0.0` and google-adk `1.8.0` ->
  `1.10.1`. genai stays `1.58.0`, protobuf stays `4.33.5` and rxjava stays
  `3.1.12`, so the protobuf floor guidance is unchanged. ADK 1.10.x moves
  `io.modelcontextprotocol.sdk:mcp` to `2.0.0`; adk-libpetri does not use it.
  See [ADR 0004](docs/adr/0004-libpetri-8-and-adk-1.10.md).
- **Z3 is now an external binary** (libpetri 4.0). SMT verification runs a
  `z3` executable (4.8+, on `PATH` or named by `LIBPETRI_Z3`) instead of JNI
  natives. This matters only to consumers who run libpetri's verifier; the
  runtime path never needed Z3. The test probes use
  `SmtVerifier.z3Available()`, and CI installs `z3` via apt.
- **Stricter deadlock-freedom proofs.** libpetri 5.0's `deadlockFree()` treats
  any token left on a non-sink place as a stranding. The speculative-race,
  optimistic-commit demo proofs now declare their by-design leftovers
  (cancelled triggers) with `sinkPlacesWhen(marker, ...)`, which excuses
  them only while the explaining marker holds. The multi-agent proof needs
  no such excuse any more: a turn's end clears its reask budget.
- **Fix: proof tests checked only their last property.** Every test that
  chained `.property(...)` calls on one `SmtVerifier` (the multi-agent,
  voice and three pattern demos) verified only the last one, because
  `SmtVerifier.property(p)` replaces the property rather than adding one.
  The bounds in front of `deadlockFree()` were never checked, and three of
  them did not hold: both speculative-race bounds and the voice net's chunk
  budget. Each property now gets its own `verify()`
  call through the new `SmtProofs` test helper, and each asserts
  `isProven()` on its own.
- **Fix: the speculative-race demo could commit twice.** Its commits
  excluded each other with `inhibitor(RACE_WON)`, but an inhibitor reads
  the marking as of the start of an orchestrator pass, and a commit's
  deposit lands at the end of it. Two branch results ready in the same pass
  both committed, so the turn emitted two events. Each commit now consumes
  the turn's single `RACE_PERMIT`, a regression test replays the
  double-commit marking, and the one-commit and one-event bounds prove
  without `assumeAtomicFiring`. `StartRace` also resets the triggers that
  cancelled branches leave behind, and the optimistic-commit demo resets
  its triggers the same way. The optimistic-commit demo never had the
  double commit, because its validation XOR enables only one commit per
  turn. The README's speculative-race case and diagram now show the permit.
- **The streaming chunk budget is gone.** `CHUNK_BUDGET` bounded nothing:
  `EmitChunk` returned the permit it took, emission is serial anyway (the
  action is synchronous and the Java executor never restarts a transition
  in flight), and overlapping requests pushed the place to K+1. Its proof
  passed only because nothing seeded a request. `LlmStreamingStepSubnet`
  now has two transitions, `LlmCallStream` (which takes `LLM_REQUEST`
  directly) and `EmitChunk`. Its proof is now deadlock-freedom with two
  requests and the chunk stream open, checked against a variant with a
  starved emit that must come back Violated.
- **Fix: re-asks carry the whole invocation.** `LlmAgentSubnet`'s re-ask
  (shared by `StreamingLlmAgentSubnet`) sent only the function responses,
  which Gemini rejects because it pairs each response with the preceding
  call. The turns now live on an in-net `LlmAgentSubnet.CONVERSATION`
  place, the model's call turn travels verbatim (thought signatures
  included) on new `ToolCalls`/`ToolResults` `modelTurn` components, and
  function responses use the `user` role, as ADK's own flow does.
- **Fix: `LlmAgentSubnet` runs one turn at a time.** It told turns apart
  by position: a second `USER_IN` while the first turn was in its tool
  loop reset that turn's conversation and reask budget, the first turn's
  re-ask then took the second turn's conversation, and since a reset sees
  only the marking its pass started with, `CONVERSATION` could hold both,
  so the second turn replayed the first one's turns. ADK's `Runner` does
  not serialise a session's invocations, so a client retry is enough. Now
  `StartTurn` takes the session's single `AdkColours.TURN_PERMIT`, and a
  later input queues until the turn ends. Every turn end returns the
  permit: the router's answer and transfer land on agent-owned places and
  `EmitAnswer`/`EmitTransfer` emit them, the reask-exhausted fallback
  answers the same way, and each end clears the turn's conversation and
  budget. `PetriRunner` seeds the permit on a fresh start of a net that has
  the place, so existing wiring keeps working; a bare libpetri executor
  must seed it. `StreamingLlmAgentSubnet` gets the same. The permit is a
  seeded token rather than an inhibitor because only a consumed token
  proves one turn at a time without `assumeAtomicFiring`; see
  [ADR 0005](docs/adr/0005-llm-agent-turn-permit.md).
- **A failed transition no longer wedges an agent's session.** A failure
  consumes its inputs and produces nothing, which would leave the turn
  holding the permit. `AdkColours.TURN_ABORT` is a new environment place
  (`PetriRunner` declares it for a net that has it); `PetriAgent` signals
  it on every transition failure, and the agent's `AbortTurn` clears the
  turn and returns the permit (`DropAbort` drops an abort with no turn in
  flight, ahead of an input that lands in the same pass). An abort clears
  what is at rest: an action of the same turn still running when it lands
  deposits into the next turn. For `LlmAgentSubnet` that takes a failure
  outside the agent; for `StreamingLlmAgentSubnet` a failed `EmitChunk`
  also does it, since the stream is still in flight.
- **`PetriRunner.start()` serves the turn permit.** On a fresh start of a
  net that has `AdkColours.TURN_PERMIT` it seeds one token (unless
  `initialMarking` names the place), and it declares `AdkColours.TURN_ABORT`
  as an environment place for a net that has it. A fresh start whose net
  has an instantiated agent's unseeded `prefix/turnPermit` throws. New
  `PetriRunner.declaresEnvironmentPlace(Place)` tells whether a runner
  accepts injections on a place; `PetriAgent` uses it to signal aborts.
- **`ToolDispatchSubnet` fails a batch with no calls** instead of answering
  it with a model turn of zero parts, which a re-ask would have sent.
- **Fix: SSE sessions no longer share an executor handle.** The documented
  streaming wiring shared one `executorRef` across every session, so a
  second session misrouted the first one's chunks and its turn hung. Use
  `StreamingLlmAgentSubnet.runnerFactory(llm, config, customize)`, which
  binds per session; `Config.executorRef` is now optional. `customize` is a
  `BiConsumer<SessionKey, PetriRunner.Builder>`: it gets the session's key
  (for `.resumeFrom(store, key)`) and runs before the factory's own
  settings, so it cannot replace the per-session executor reference, and
  declaring `USER_IN` or `CHUNK` in it fails the start.
- **Fix: `PersistStateSubnet` now bounds `appendEvent`.** Its
  `Timing.deadline(5s)` bounded how long the transition may stay enabled,
  not how long the call runs, and under libpetri 8.0 a late orchestrator
  reaped it and stranded the write. It is now an action timeout,
  `Config.persistTimeout` (default 5 s, honoured to the nanosecond), and a
  timed-out append has its Rx subscription disposed.
- **Proofs match the README.** `StockSubnetProofsTest` proves `LlmStep`,
  `Router`, `ToolDispatch`, `TransferRouter` and `PersistState` each alone
  (`SubnetDef.verify`, `arrivals(k, k)`), and the composed
  `LlmAgentSubnet` turns every user input into exactly one answer,
  fallback or transfer; the reask budget is proved not to stack across
  inputs (design commitment 6), along with one turn and one conversation
  at a time, recovery from a failure at any step, and a permit no abort
  can duplicate; `eventOutBounded` is proved on the multi-agent net.
  Budget bounds are stated in seeds, since libpetri models an N-permit
  seed as one token. One proof, the race permit, assumes atomic firing;
  the README says why that is exact on the Java executor. Every other
  proof runs with libpetri 8.0's in-flight split.
- **Wiring helpers.** `SubnetActions.merge` and `bindComposed` bind a
  composed net's maps in one checked call. `PetriAgent.builder(...)`, with
  the owner extractor optional under `strongOwned()` (new
  `SessionExecutorRegistry.getOrCreate(key, factory)`); the `of`/`ofLive`
  overloads remain. Demos use `strongOwned()`, the documented default.
  Mixing the ownerless `getOrCreate` with an explicit owner for one key
  throws with a message that says so.
- **libpetri runtime options.** `PetriRunner.Builder` gains `restore`,
  `executionScope`, `executionEnvironment` and `deadlineTolerance`, and
  `PetriRunner.snapshot()`; `snapshot`, `restore` and `executionScope` are
  experimental, since the checkpoint format is the snapshot format.
  `OtelEventStore(tracer, delegate, net)` tags spans with
  `libpetri.transition` and `libpetri.subnet`. libpetri 7.0's terminal
  places need no new API here; `PetriRunnerTest` demonstrates one on
  `END_INVOCATION`. A terminal token ends the whole per-session run, so it
  is a session end rather than ADK's per-invocation `endInvocation`, and
  the registry keeps returning the stopped runner until the key is closed.
- **Session checkpoints** *(experimental)*: `SessionCheckpointStore`
  (`save`, `load`, `remove`); `SessionExecutorRegistry.strongOwned(store)`/
  `cleanerOwned(store)` drain a session's runner at teardown (new injects
  refused, actions in flight completed) and save the marking its run ends
  in, and `PetriRunner.Builder.resumeFrom(store, key)` restores it, taking
  precedence over `initialMarking` so one factory serves first start and
  resume. Until the save lands the key stays taken: a concurrent
  `getOrCreate` waits and then resumes from it, so a key never has two
  serving runners. A run that does not drain to quiescence within the
  checkpoint timeout (30 s by default, `strongOwned(store, timeout)`), or a
  failed save, removes the key's checkpoint instead of leaving a stale one.
  `EVENT_OUT` is never saved, so delivered events no longer pile up across
  resumes; `Builder.excludeFromCheckpoint(places...)` leaves out further
  egress places. `registry.discard(key)` ends a session without saving it
  and drops its checkpoint. Exemplar: `AgentStateCheckpointStore` keeps it
  in ADK's `EventActions.agentState`, with an append-only tombstone for
  `remove`.
- **Exemplars and tests.** `VadTapGemini` recovers the voice-activity edges
  ADK drops by wrapping ADK 1.9's `GeminiLiveTransport`, with no fork; a
  throwing signal callback is logged rather than ending the live stream.
  `ManualClock` drives timed tests on a virtual clock; the silence-recovery
  tests no longer sleep.

#### Breaking

- **libpetri `3.0.1` -> `8.0.0` arrives transitively**, five majors at
  once. Code that uses libpetri directly should read libpetri's own
  CHANGELOG. Three changes reach most such code: SMT verification needs a
  `z3` binary, and `org.sosy-lab:javasmt-solver-z3` (with
  `com.microsoft.z3`) is no longer on the classpath (4.0);
  `deadlockFree()` counts any token left on a non-sink place as a
  stranding, so a proof that passed may now come back Violated (5.0); and
  a transition whose outputs another transition tests with an inhibitor,
  reset or drain is verified as separate start and completion steps, which
  can flip a bound or mutex proof (8.0, VER-004).
- `AdkColours.ToolCalls` and `ToolResults` gained a `modelTurn` component,
  so record patterns must name two components and `equals`/`hashCode`
  now compare the model turn too. `ToolCalls` keeps its one-argument
  constructor (dispatch rebuilds the turn). `ToolResults` drops it, and
  its `modelTurn` (and `results`) must be non-null: the re-ask sends the
  model turn back, and a results token has no calls to rebuild it from.
- `LlmAgentSubnet.DEF` and `StreamingLlmAgentSubnet.DEF` changed topology:
  new transitions `StartTurn`, `EmitAnswer`, `EmitTransfer`, `AbortTurn`,
  `DropAbort`; new places `TURN_ACTIVE`, `TURN_INPUT`, `ANSWER`,
  `HANDOFF` and `AdkColours.TURN_PERMIT`; a new `turnAbort` input port
  (`AdkColours.TURN_ABORT`). `BuildPrompt` now takes `TURN_INPUT`, not
  `USER_IN`, and has no reset arcs. The composed `Router_Route`
  transition writes to `ANSWER`/`HANDOFF` and is bound by
  `LlmAgentSubnet.actionBindings`, which no longer merges
  `RouterSubnet.actionBindings`. The reask-exhausted fallback answers on
  `ANSWER`. `CONVERSATION` and `REASK_BUDGET` no longer rest between
  turns, so checkpoints no longer carry them; they carry the permit. A
  net run on a bare libpetri executor must seed `TURN_PERMIT` with one
  token, or no turn starts. An agent composed through
  `DEF.instantiate(prefix)` must have its `prefix/turnPermit` seeded in
  `initialMarking` and its `turnAbort` port bound to
  `AdkColours.TURN_ABORT`; `PetriRunner` refuses to start it unseeded.
  `SubnetDef.verify` on the agent needs a `turnAbort` generator.
- `ToolDispatchSubnet` fails the firing for a `ToolCalls` with no calls,
  where it used to produce an empty `ToolResults`.
- `PetriRunner.ExecutorFactory.build` takes one `ExecutorSpec` record
  instead of six arguments.
- `PersistStateSubnet.Config` gained `persistTimeout`, and `DEF` no longer
  carries a `Timing.deadline`. A session service that has not answered
  within `persistTimeout` (default 5 s) now fails the `Persist` transition
  with a `TimeoutException`, and that write is dropped.
- `SubnetActions` binding-mismatch messages now start with the net's name
  in quotes (`'LlmAgent' action-binding mismatch: ...`) rather than
  `Subnet 'LlmAgent' ...`, since `bindComposed` reports a composed net.
  Code matching the old text needs updating.
- *(experimental)* `StreamingLlmAgentSubnet.Config.executorRef` is no
  longer required, so `executorRef()` may return `null`; the builder no
  longer throws without one. `actionBindings` still requires it.
- `AdkNetInvariants.noFireAfterEndInvocation` is removed; use
  `SmtProperty.mutualExclusion(AdkColours.END_INVOCATION, place)`.
- *(experimental)* `LlmStreamingStepSubnet` drops `Places.CHUNK_BUDGET`,
  `Places.LLM_REQUEST_INTERNAL`, `Transitions.SEED_AND_START` and
  `Config.chunkBudget` (the record component and the builder method);
  `StreamingLlmAgentSubnet.Config` drops `chunkBudget` too. Remove the
  `.chunkBudget(n)` call; nothing replaces it.

### Docs

- **The root README covers both ports.** It gains a Java and Python names
  table, numbers the not-yet-guaranteed cases N1 and N2, and promotes
  `from_workflow` to its own section. Java-only detail moves to
  `java/README.md`: the protobuf version floor, calling Gemini without
  `commonPool`, how `PetriAgent` drives a turn, getting voice-activity
  edges and the ADK Java 1.10.1 bytecode facts. The ADK Python 2.11
  behaviour the foils lock in moves to `python/README.md`. Diagrams are
  white cards, and the compiled-workflow diagrams are generated by
  `python/tests/readme_diagrams`.


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
