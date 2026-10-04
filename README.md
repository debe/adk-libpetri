# adk-libpetri

[![CI](https://github.com/debe/adk-libpetri/actions/workflows/ci.yml/badge.svg)](https://github.com/debe/adk-libpetri/actions/workflows/ci.yml)
[![Maven Central](https://img.shields.io/maven-central/v/org.libpetri/adk-libpetri)](https://central.sonatype.com/artifact/org.libpetri/adk-libpetri)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

<p align="center">
  <img src="docs/assets/best-of-both-worlds-cover.svg"
       alt="Two worlds fusing: Google's ADK ecosystem (Vertex AI, Cloud Run, A2A, OpenTelemetry) on the left and a libpetri Petri-net runtime on the right, joined by a token crossing the seam"
       width="1000">
</p>

adk-libpetri replaces Google ADK Java's orchestration core with a
Coloured Time Petri Net runtime built on
[libpetri](https://github.com/debe/libpetri). It replaces
`SequentialAgent`, `ParallelAgent`, `LoopAgent`, `BaseLlmFlow`,
`AgentTransfer`, and the `Runner` that drives them over an RxJava
pipeline. In their place, a session's whole control flow is one coloured
Petri net you compose from typed subnets.

- **Drop-in for ADK.** Stock ADK `Runner` consumes the net through
  `PetriAgent`, an ordinary `BaseAgent`. Live/BIDI sessions use the
  provider-neutral `BidiPetriAgent` bridge over `LiveConnection`. There
  is no fork of ADK and no fork of genai.
- **Concurrency, cancellation and loop bounds live in the topology.**
  Races, joins, barge-in and runaway tool loops are arcs and places, not
  flags checked in callbacks.
- **Proved on every CI build.** Z3 proves both demo nets deadlock-free,
  and proves turn-level properties of `LlmStep`, `Router`,
  `ToolDispatch`, `TransferRouter` and the composed `LlmAgent` and
  `StreamingLlmAgent`, on every `mvn verify` where `z3` is installed.
  CI installs `z3` and a gate test fails the build if the binary is
  missing, so the proofs cannot quietly turn into skips.

## Contents

- [Project status](#project-status)
- [Install](#install)
- [A quick look](#a-quick-look)
- [Why a Petri net](#why-a-petri-net)
- [Four cases the sequence shape gets wrong](#four-cases-the-sequence-shape-gets-wrong)
- [How it works](#how-it-works)
- [ADK integration](#adk-integration)
- [Design commitments](#design-commitments)
- [Verification](#verification)
- [Development](#development)
- [Consuming from a project: protobuf version floor](#consuming-from-a-project-protobuf-version-floor)
- [Relationship to libpetri](#relationship-to-libpetri)

## Project status

This is early-stage. The runtime, the stock subnets and both verified
demos are real and pass on every build. The scope of the ADK integration
is still being worked out: which slices of the ADK surface a
Petri-driven agent should own, and where exactly the net-to-ADK seam
belongs. Expect the boundary colour catalog, the stock subnet set and
the adapter shape to move as that scope is found. Releases are working
snapshots of that exploration, not a frozen API.

Versioning is **0.x**, and a minor version may break API. The turn-based
path is the settled part: `PetriAgent.builder` and the `PetriAgent.of`
shorthands, the stock non-streaming subnets, and
`SessionExecutorRegistry`. The SSE-streaming and BIDI/live surfaces are
marked `@Experimental` in source and move faster.

## Install

```xml
<dependency>
    <groupId>org.libpetri</groupId>
    <artifactId>adk-libpetri</artifactId>
    <version>0.4.0</version>
</dependency>
```

```groovy
implementation 'org.libpetri:adk-libpetri:0.4.0'
```

Java 25 or later. ADK and libpetri come transitively. Before pinning
protobuf yourself or using an enforced platform BOM, read the
[protobuf version floor](#consuming-from-a-project-protobuf-version-floor).

## A quick look

One `LlmAgentSubnet` (prompt, model call, routing, tool dispatch, a
bounded re-ask loop) composed into a net, wrapped in a `PetriAgent`, and
run by stock `InMemoryRunner`:

```java
var llm = /* your com.google.adk.models.BaseLlm */;

var config = LlmAgentSubnet.Config.builder("my_agent", "gemini-2.5-flash")
        .systemInstruction("Be helpful.")
        .reaskBudget(3)                                // bounds the tool loop
        .dispatchExecutor(Executors.newVirtualThreadPerTaskExecutor())
        .build();

// bindComposed rejects a missing, unknown or doubly-bound transition.
var net = SubnetActions.bindComposed(
        PetriNet.builder("hello").compose(LlmAgentSubnet.DEF).build(),
        LlmAgentSubnet.actionBindings(llm, config));

var registry = SessionExecutorRegistry.strongOwned();  // one runner per session
var agent = PetriAgent.builder("my_agent", registry,
                key -> PetriRunner.builder(net)
                        .environmentPlace(AdkColours.USER_IN)
                        .orchestratorExecutor(Executors.newVirtualThreadPerTaskExecutor())
                        .start())
        .build();

var runner = new InMemoryRunner(agent);                // stock ADK, no fork
// ... runner.runAsync(userId, sessionId, content, runConfig)
// from your session-end hook: registry.close(SessionKey.from(session));
```

[`java/README.md`](java/README.md) has the complete program with
imports, plus SSE streaming, Live/BIDI wiring and the two end-to-end
demos.

## Why a Petri net

ADK Java expresses an agent process as a sequence, with concurrency
layered on at runtime (`BaseLlmFlow` over RxJava 3). That shape gives you
*before* and *after*. It has no semantics for what flows along the
edges, and no native concurrency, inhibition, mutual exclusion, time,
structural loops or proof. Each arrives as a separate operator chain or
annotation, and past a certain complexity the diagram on the whiteboard
stops matching the artefact in production.

A Coloured Time Petri Net makes each of those a structural property of
one artefact:

- **Concurrency is the default.** Two transitions with disjoint
  preconditions fire independently. Parallelism is a property of the
  runtime, not an operator the caller composes.
- **Loops are cycles in the topology**, not a `LoopAgent` wrapped around
  a sequence.
- **Branching is `Out.xor`; joins are multi-arc `In.one`.** The diagram
  says what happens.
- **Inhibitor, read and reset arcs, and time, are first-class.** "Fire
  only if X is absent," "snapshot upstream state without consuming it,"
  "fire after T units of silence" and "reset this whole region in one
  firing" are arcs, not guard code hidden in callbacks.
- **The marking is the state.** Tokens carry typed domain colours
  (`Place<LlmRequest>`, `Place<Content>`, `Place<ToolCalls>`). No
  external state object races against itself.
- **Causality is structural.** A transition fires when its preconditions
  are present, not when a previous step "called" it.

Coloured Petri nets with inhibitor arcs are Turing-complete, so the
expressive ceiling is a property of the shape, not of the library. The
formal model is well established and research tools exist (CPN Tools,
TINA, LoLA), but a fast, deployable runtime did not. libpetri supplies
one: a fast executor, structural composition with type inference, a
state-class graph for bounded-reachability checks, and IC3/SMT
verification via Z3. adk-libpetri is the first applied showcase of that
runtime against a real orchestration problem.

### Relation to ADK 2.0's workflow runtime

ADK's own direction makes the same argument. ADK 2.0 (the Python line;
the Java SDK stays on the 1.x contract this project targets) replaces
the nested `SequentialAgent` / `ParallelAgent` / `LoopAgent` executor
with a *workflow runtime* that evaluates agents, tools and functions as
nodes in an execution graph. Leaving the tree concedes that nesting
sequence and parallel shapes projects a concurrent process onto a
structure too narrow to hold it.

A coloured timed Petri net is a superset of that graph model on the axes
that govern orchestration correctness. Concurrency is the firing rule,
not a coordinator layered onto the graph: a transition with several
input places *is* an AND-join, and a shared place feeding competing
transitions *is* a race. Synchronisation, choice, mutual exclusion,
bounded loops and pre-emption are input, inhibitor and read arcs and
priorities, not imperative checks inside node bodies.

The decisive difference is *when* correctness is established. A graph
runtime tracks graph state at run time and reports it afterwards; a
marking lets the same properties be proved before execution. The cost is
modelling discipline: a plain graph is simpler to author for linear or
fan-out flows, and a managed runtime supplies retries, telemetry and
hosting out of the box. Where the orchestration's ordering, exclusion
and cancellation guarantees are load-bearing, a verifiable net is the
stronger foundation, and `PetriAgent` keeps it inside the ADK contract.

## Four cases the sequence shape gets wrong

The bug classes that haunt agent frameworks at scale are not
implementation bugs in those frameworks. They are shape mismatches
between the runtime and the process it models. Three of the four cases
below are the same failure: a check-and-act race, because the check runs
at one boundary (a callback, a `session.state` read, a flag at the top
of a step) and the act at another. A single transition firing makes the
check and the act one atomic step, so the window between them does not
exist. The fourth case covers full-duplex failures that are hard to
express in any sequence-shaped runtime. Each case names the closest
one-liner in ADK or RxJava and why it does not survive composition.

Cases 3 and 4 are backed by executable demos with machine-checked
properties (`PatternA_SpeculativeRaceDemoTest`, `VoiceSessionDemoTest`).
Each demo proves a reduced version of the net its case describes, and
the case says what the demo covers. Cases 1 and 2 are illustrative: there is not yet a dedicated
`COLLECTOR` / `LATEST_GENERATION` demo with a paired ADK foil. The
closest executable head-to-heads today are the
[composition-pattern demos](#composition-patterns-adk-cannot-express).
[ADR 0001](docs/adr/0001-pre-port-design-gate.md) tracks backing cases 1
and 2 with their own demo, foil and SMT property.

### 1. Concurrent fan-out with batch-scoped state

A retrieval-heavy agent dispatches a batch of tagged jobs whose count
varies per request (an intent classifier picks 1 or 3 strategies).
Results accumulate into a record carrying the expected count, the
per-strategy buckets, and a merge rule derived from the intent. While
the batch is in flight, an orthogonal subnet wants to read the partial
state (to skip a redundant computation, or to send an "I'm still
searching" placeholder). A new user turn must abort the in-flight
workers and the partial accumulator before the next batch.

**The one-liner**, `Flowable.zip(a, b, c)`, handles fan-in for a fixed
arity only. It exposes the accumulator to nothing else, offers no single
reset point for new-turn cancellation, and has no home for the per-batch
merge rule. The ADK accumulator pattern (`ParallelAgent` plus a
`ConcurrentHashMap` plus a `CountDownLatch`) leaves TOCTOU windows
between the accumulator update, the latch decrement and the downstream
emit.

**In the net**, the accumulator is the token in a typed `COLLECTOR`
place. `CollectResult` consumes one result plus the `COLLECTOR`, merges,
and uses `Out.xor` to return either the collector alone or the collector
together with the downstream signal. `OrthogonalRead` reads `COLLECTOR`
through a read arc, so orthogonal subnets get atomic snapshots without
disturbing the merge. `OnNewUserTurn` consumes `USER_NEW_TURN` and resets
`COLLECTOR` and every job place in one firing, so the whole in-flight
batch cancels atomically.

<p align="center">
  <img src="docs/diagrams/svg/stateful-monitor.svg"
       alt="SpawnJobs initialises the COLLECTOR; three Workers emit tagged SEARCH_RESULT; CollectResult merges and XOR-routes; OrthogonalRead snapshots COLLECTOR via a read arc; OnNewUserTurn resets the in-flight batch"
       width="1000">
</p>

### 2. Stale-result detection across many commit sites

A long-running session has streaming chunks, parallel tool results and
placeholder replies in flight at once. When the user takes a new turn,
the generation counter advances, and every in-flight result that lands
after the advance must be discarded before it enters conversation
history or triggers another model call.

**The one-liner** is an `AtomicReference<Generation>` in `session.state`
with an `if (current.get() != myGen) discard()` at each commit site. The
read is atomic. The problem is that the check is not one site but one per
result-emitting transition, and missing any one leaks stale data. This is
exactly how ADK's advisory `endInvocation` flag fails: it is checked at
the top of each step while an in-flight `Flowable.merge` of parallel tool
calls fires past it.

**In the net**, `LATEST_GENERATION` holds the current generation token.
The commit transition reads it and XOR-routes to a committed or a
discarded leaf. `BumpGeneration` consumes `USER_NEW_TURN`, resets
`LATEST_GENERATION` and emits a fresh token, invalidating every in-flight
commit at once. Each further commit site is one more transition with the
same `read(LATEST_GENERATION)` arc, so the discipline can become a
build-time invariant: a freshness-read validator, built like the
structural validators in [Verification](#verification), would reject any
composed net whose commit transitions omit the read arc. ADK's "remember
to check everywhere" would become a compile-time refusal. No such
validator ships yet; ADR 0001 tracks it.

<p align="center">
  <img src="docs/diagrams/svg/stale-result-validation.svg"
       alt="INCOMING_RESULT enters CommitResult, which reads LATEST_GENERATION and XOR-routes to COMMITTED or DISCARDED; BumpGeneration consumes USER_NEW_TURN, resets LATEST_GENERATION, and emits a fresh generation token"
       width="880">
</p>

### 3. Speculative race with composable cancellation

Two paths fire concurrently: a slow, accurate computation and a fast,
approximate one. The slow path wins if it returns within a 2-second
deadline; otherwise the fast result commits at the deadline. The user
can barge in at any point, and both in-flight paths must then cancel
atomically, so nothing from the cancelled race can still answer and the
next turn starts clean. A third tier (a cached default at five seconds)
must slot in without disturbing the at-most-once invariant or the
cancellation.

**The one-liner** is `Mono.first(slow, fast.delayElement(ofSeconds(2)))`.
Barge-in then needs `takeUntil` on each path, at-most-once needs an
external flag tracked alongside the chain, the SLA disappears into a
`delayElement` indistinguishable from a debounce, and a third tier means
rewriting the composition. None of these properties reaches a formal
checker.

**In the net**, `StartBoth` forks the request into `SLOW_INFLIGHT`,
`FAST_INFLIGHT` and `TIMER_PENDING`, and seeds one `RESPONSE_PERMIT`.
`CommitSlow` fires as soon as `SLOW_DONE` lands and takes the permit;
`CommitFastOnTimeout` needs `FAST_DONE`, `TIMER_EXPIRED` and the same
permit. The at-most-once invariant `PlaceBound(RESPONSE, 1)` is visible
in the diagram and SMT-checkable. `OnBargeInOrNewTurn` resets every race
place, permit included, in one firing. The third tier is one more
transition consuming the same permit with a longer timer. The invariant
and the cancellation stay unchanged, because both are properties of the
topology.

The permit is load-bearing. Guarding each commit with an inhibitor on a
"response already sent" marker looks equivalent and is not: an inhibitor
reads the marking as of the start of an orchestrator pass, so two
commits that become ready in the same pass both fire.

`PatternA_SpeculativeRaceDemoTest` proves an untimed, three-branch
version of this net. `Race_Start` seeds one `RACE_PERMIT` per turn, each
branch's commit consumes it and marks `RACE_WON`, and Z3 proves
`placeBound(RACE_WON, 1)` and `placeBound(EVENT_OUT, 1)` per turn. The
demo first guarded its commits with `inhibitor(RACE_WON)` and now
replays the resulting double commit as a regression test.

<p align="center">
  <img src="docs/diagrams/svg/speculative-race.svg"
       alt="StartBoth forks REQUEST into SLOW_INFLIGHT, FAST_INFLIGHT, and TIMER_PENDING; TimerFires after 2s produces TIMER_EXPIRED; CommitSlow and CommitFastOnTimeout compete for one RESPONSE_PERMIT that StartBoth seeds; OnBargeInOrNewTurn resets the entire race, permit included, on USER_INTERRUPT"
       width="1000">
</p>

### 4. Voice and full-duplex failure modes

A voice agent streams partial responses, detects barge-in mid-stream,
runs a two-stage silence-recovery timer, and resets stale state on each
new utterance. The four modes interact: a barge-in must suppress further
chunks, recovery must not fire while the model is speaking, and a new
utterance must wipe in-flight tool requests and pending recovery flags.

**The one-liner** is a `Flux` with `timeout(silence)`,
`takeUntil(bargeIn)` and `switchMap(newUtterance)`, each handling one
mode in isolation. The composition is where it breaks: the silence
timeout fires against a chunk the barge-in had logically cancelled but
the stream had not yet seen, and the new-utterance reset races the
in-flight recovery.

**In the net**, each mode is one arc. An inhibitor on `MODEL_ACTIVE`
gates recovery. A read arc on `VOICE_ACTIVITY_OPEN` gates barge-in
dispatch. Each chunk enters through its own `CHUNK` env-place injection
and leaves through one emit transition, in arrival order. Reset arcs on
the new-utterance transition wipe in-flight state. They compose because
they are all properties of the marking.

`VoiceSessionDemoTest` composes streaming, barge-in and silence recovery
into one long-lived per-session net, and Z3 proves it deadlock-free. A
separate test in the same class demonstrates the new-utterance reset
arc, which wipes the in-net `CURRENT_INTENT`. `LiveApiRecoverySubnetTest`
builds the state-class graph of the composed streaming, barge-in and
recovery subnets and confirms it completes within a 256-marking cap. The
composed topology is in
[`bidi-composition.svg`](docs/diagrams/svg/bidi-composition.svg).

## How it works

Each subnet declares typed ports such as `Place<LlmRequest>` and
`Place<ToolCalls>`. `PetriNet.Builder.compose(...)` binds them by
`(name, tokenType)` structural match, so wiring an `LlmResponse` port into
a `Content` port fails when the net is built, not at runtime. Because
places carry domain colours rather than an opaque `InvocationToken`, a
DOT export of the net reads as the agent process itself. Every diagram
in this README is a libpetri DOT export of a topology that mirrors the
Java subnets (see [`docs/diagrams/`](docs/diagrams/)).

### Runtime model

- **One `PetriNet` per session**, built at session start and kept alive
  for the session's lifetime. A new user message is
  `inject(USER_IN, content)` into the already-running net.
- **One way in, one way out.** Input is `inject(envPlace, token)` (or
  `signal(place)` for a `Place<Void>`) from any thread, on any number of
  typed env places. Output is the `EVENT_OUT` bridge plus the
  `EventStore` decorator chain. There is deliberately no generic
  `observe(Place<T>)`, so egress stays narrow and auditable.
- **Executors are caller-supplied.** `PetriRunner.Builder` requires an
  explicit `orchestratorExecutor`; the library has no shared executor
  singleton. libpetri invokes actions inline rather than submitting them,
  so that one pool is also where every transition action runs. Make it
  virtual-threaded if actions block. Actions that need real fan-out take
  their own pool, the way `ToolDispatchSubnet` takes a
  `dispatchExecutor`.

[Design commitments](#design-commitments) states the rules these follow.

### Boundary colour catalog (`AdkColours`)

A fixed set of typed places that all stock subnets share. Composition
fuses them by `(name, tokenType)` structural equality, so you write port
mappings only when you want to.

| Colour | Type | Use |
|---|---|---|
| `USER_IN`              | `Place<Content>`             | user message inbound (env place) |
| `EVENT_OUT`            | `Place<Event>`               | agent event outbound |
| `LLM_REQUEST`          | `Place<LlmRequest>`          | prepared LLM request |
| `LLM_RESPONSE`         | `Place<LlmResponse>`         | LLM response (merged for streaming) |
| `TOOL_CALLS`           | `Place<ToolCalls>`           | list of FunctionCalls to dispatch |
| `TOOL_RESULTS`         | `Place<ToolResults>`         | list of FunctionResponses |
| `LEGACY_SESSION_WRITE` | `Place<LegacySessionWrite>`  | write-only bridge to ADK Session.state |
| `TRANSFER`             | `Place<TransferTarget>`      | agent-transfer routing token |
| `END_INVOCATION`       | `Place<Void>`                | termination signal (inhibitor source) |
| `TURN_PERMIT`          | `Place<Void>`                | one-turn-at-a-time permit, seeded by `PetriRunner` |
| `TURN_ABORT`           | `Place<Void>`                | clears a turn a failure stranded (env place, signalled by `PetriAgent`) |

Add your own colours for in-net state, such as
`Place<ConversationHistory>` or `Place<CurrentProduct>`: one typed place
per domain concept, never a single `Place<Map<String, Object>>` bag.

### Stock subnet catalog

Each is a `SubnetDef` or factory you compose with
`PetriNet.builder().compose(SubnetDef)` and port inference. They are
convenience templates, not the framework. The framework is the
composition primitives together with `SubnetDef.fromNet(...)`.

| Subnet | Input ports | Output ports | What it does |
|---|---|---|---|
| `LlmStepSubnet`         | `LLM_REQUEST` | `LLM_RESPONSE`                 | Calls `BaseLlm.generateContent`. `BeforeModel` can short-circuit via `Out.xor(continue, LLM_RESPONSE)`; `LlmCall` splits success from error with `Out.xor`; `AfterModel` / `OnModelError` callbacks feed `LLM_RESPONSE` |
| `ToolDispatchSubnet`    | `TOOL_CALLS`  | `TOOL_RESULTS`                 | Per-call task on a virtual-thread executor, AND-join of results. Per-call errors captured in the response payload |
| `PromptBuilderSubnet`   | `USER_IN`     | `LLM_REQUEST`                  | Builds `LlmRequest` (model, system instruction, tools) |
| `RouterSubnet`          | `LLM_RESPONSE`| `Out.xor(TOOL_CALLS, TRANSFER, EVENT_OUT)` | Routes by response shape. `transfer_to_agent` takes precedence |
| `LlmAgentSubnet`        | `USER_IN`, `TURN_ABORT` | `EVENT_OUT`, `TRANSFER` | Its own `StartTurn`/`BuildPrompt` plus `LlmStep`, `Router`'s route and `ToolDispatch`, with a reask-budget loop that bounds the autonomous tool loop. Each re-ask replays the invocation's conversation from an in-net `CONVERSATION` place. Runs one turn at a time under `TURN_PERMIT` |
| `PersistStateSubnet`    | `LEGACY_SESSION_WRITE` | terminal | One transition draining `StateDelta` to `BaseSessionService.appendEvent`, bounded by `persistTimeout` (default 5 s). Race-free by construction: the only writer transition in the net |
| `TransferRouterSubnet`  | `TRANSFER`    | `target/<name>*`, `target/_unknown`, `EVENT_OUT` | `Out.xor` over compile-time-known target places. A hallucinated name routes to a typed error Event, not an NPE |
| `LlmStreamingStepSubnet` *(experimental)* | `LLM_REQUEST` | `LLM_RESPONSE`, `EVENT_OUT` | SSE counterpart of `LlmStep`: each model chunk becomes a partial `Event` through a `CHUNK` env place, in arrival order; the merged response continues to `LLM_RESPONSE` |
| `StreamingLlmAgentSubnet` *(experimental)* | `USER_IN`, `TURN_ABORT` | `EVENT_OUT`, `TRANSFER` | `LlmAgentSubnet` over `LlmStreamingStep`, turn permit included. Wire it with `StreamingLlmAgentSubnet.runnerFactory(...)`, which gives each session its own executor handle |

Voice-specific subnets (`BargeIn`, `LiveApiRecovery`, `Vad`) are not part
of the shipped library. They live under
`src/test/java/org/libpetri/adk/demos/voice/` as composable exemplars of
[case 4](#4-voice-and-full-duplex-failure-modes).

### The canonical composition: `LlmAgentSubnet`

`LlmAgentSubnet` runs prompt build, LLM call, routing, tool dispatch and
the re-ask loop, one turn at a time:

1. **`StartTurn`** takes the session's single `TURN_PERMIT` together with
   the `USER_IN`. An input that arrives mid-turn (a client retry, say)
   waits until the turn ends instead of trampling it.
2. **`BuildPrompt`** seeds K reask-budget tokens (K is per session, via
   `Config.Builder.reaskBudget(int)`) and the conversation's user turn.
3. **`ReAsk`** consumes one budget token per tool round and extends the
   conversation with the model's function-call turn and the tool
   responses, so every continuation request carries the whole
   invocation.
4. **`EmitAnswer` / `EmitTransfer`** emit the turn's answer (the
   router's, or `ReAskExhaustedFallback`'s once the budget is spent) or
   its transfer, clear the turn's conversation and budget, and return the
   permit.
5. **`AbortTurn`** recovers a turn a failed transition stranded:
   `PetriAgent` signals `TURN_ABORT` on every transition failure, and the
   turn is cleared and the permit returned. When no turn is in flight,
   **`DropAbort`** consumes the signal instead, ahead of `StartTurn`, so a
   stray abort never wipes a fresh turn.

`PetriRunner` seeds the permit and declares `TURN_ABORT`, so wiring is
unchanged. [ADR 0005](docs/adr/0005-llm-agent-turn-permit.md) explains
why the permit is a seeded token rather than an inhibitor. Persisting to
ADK's `Session.state` is a separate `PersistStateSubnet` you compose
alongside.

<p align="center">
  <img src="docs/diagrams/svg/llm-agent-subnet.svg"
       alt="LlmAgentSubnet topology: StartTurn taking the TURN_PERMIT, BuildPrompt, LlmStep, Router with Out.xor, ToolDispatch, the reask-budget loop replaying the CONVERSATION place, EmitAnswer and EmitTransfer returning the permit, and AbortTurn/DropAbort on TURN_ABORT"
       width="900">
</p>

#### Bounding autonomous loops: the reask budget

An LLM-and-tool loop that re-asks the model after every tool batch is
the classic autonomous-runaway risk. The net bounds it structurally, not
with a counter in application state. `BuildPrompt` seeds K tokens into a
`Place<Void> REASK_BUDGET`, and each re-ask consumes one. Once the budget
is empty, an inhibitor-guarded, lower-priority fallback transition is the
only one still enabled, so the loop ends with a graceful answer instead
of spinning. The bound is visible in the diagram and SMT-checkable.

<p align="center">
  <img src="docs/diagrams/svg/reask-budget.svg"
       alt="The reask-budget pattern: priority plus inhibitor on a Place<Void> REASK_BUDGET; when the budget is exhausted the fallback transition fires"
       width="720">
</p>

### Composition patterns ADK cannot express

Three patterns sit outside what `SequentialAgent`, `ParallelAgent`,
`LoopAgent` and `AgentTransfer` can express, because ADK's agent
vocabulary has no primitive for them (first-wins, K-of-N, path
switching). Each takes roughly 40 to 50 lines of `PetriNet.builder()`
user code and is paired with an ADK-only foil test that locks in the
broken behaviour.

| Pattern | How the net does it | Proved | Tests |
|---|---|---|---|
| **Speculative race** with structural cancellation | One `RACE_PERMIT` per turn that every commit consumes; the first result takes it and losers cannot commit. A `RACE_WON` marker cancels branches not yet started and drains late results | `PlaceBound(RACE_WON, 1)`, `PlaceBound(EVENT_OUT, 1)` per turn | `PatternA_SpeculativeRaceDemoTest` / `PatternA_AdkOnlyFoilTest` |
| **Late-join / K-of-N quorum** | `Arc.In.exactly(K, RESULT)` fires the instant K branches return; late arrivals drain to a typed `DISCARDED` sink. Runtime is bounded by the K-th-fastest branch, not the slowest | one synthesis, one event per turn | `PatternB_QuorumDemoTest` / `PatternB_AdkOnlyFoilTest` |
| **Optimistic commit** with structural fallback | Cheap and slow paths run concurrently; an XOR validation enables exactly one commit path, and a `COMMITTED` marker cancels the slow path once either commits | `PlaceBound(COMMITTED, 1)` per turn | `PatternC_OptimisticCommitDemoTest` / `PatternC_AdkOnlyFoilTest` |

All six tests live under
`java/src/test/java/org/libpetri/adk/demos/patterns/`. Each foil test is
a passing assertion of the broken behaviour. If a future ADK release
fixes one, the assertion turns red and this table gets updated.

## ADK integration

### `PetriAgent`: stock `Runner`, no source change

`PetriAgent extends BaseAgent` (in
`java/src/main/java/org/libpetri/adk/runner/`) is the turn-based
integration seam. Its two run paths, abridged from `PetriAgent.java`
(the real methods also open an OpenTelemetry invocation span when tracing
is wired):

```java
@Override
protected Flowable<Event> runAsyncImpl(InvocationContext ctx) {
    SessionKey key = SessionKey.from(ctx.session());
    PetriRunner runner = runnerFor(ctx, key);           // registry.getOrCreate
    Content userContent = ctx.userContent().orElse(null);
    if (userContent == null) return Flowable.empty();

    // One ADK invocation id across the turn, and a transition failure
    // fails the turn (failureSignal is control flow, not observability).
    Flowable<Event> egress = runner.adkEvents()
            .map(e -> e.toBuilder().invocationId(ctx.invocationId()).build());

    if (ctx.runConfig().streamingMode() == StreamingMode.SSE) {
        // Partials through to the first non-partial event.
        var turn = Flowable.merge(egress, turnFailureSignal(runner))
                .takeUntil(e -> !e.partial().orElse(false))
                .replay();
        turn.connect();                                 // subscribe BEFORE inject
        runner.inject(AdkColours.USER_IN, userContent);
        return turn;
    }

    // Default: the turn's single terminal (non-partial) event.
    CompletableFuture<Event> next = new CompletableFuture<>();
    Flowable.merge(egress.filter(e -> !e.partial().orElse(false)), turnFailureSignal(runner))
            .take(1)
            .subscribe(next::complete, next::completeExceptionally);
    runner.inject(AdkColours.USER_IN, userContent);
    return Single.fromCompletionStage(next).toFlowable();
}

@Override
protected Flowable<Event> runLiveImpl(InvocationContext ctx) {
    PetriRunner runner = runnerFor(ctx, SessionKey.from(ctx.session()));
    if (liveConfig == null) return runner.adkEvents();  // egress only
    return BidiPetriAgent.bridge(ctx.liveRequestQueue().orElseThrow(),
            liveConfig.connectionFactory().apply(ctx), runner, liveConfig.onServerMessage());
}
```

`runAsyncImpl` replaces ADK orchestration: the net is the brain for the
whole request/response. `runLiveImpl` is, by default, only the egress
half, exposing the net's ADK event stream to `Runner`. An agent built
with `PetriAgent.builder(...).live(liveConfig)` runs full Live/BIDI
instead (see [BIDI](#bidi-and-voice)).

When a transition fails mid-turn, `PetriAgent` fails the turn rather
than waiting forever. For a net that has `TURN_ABORT` (any net with an
`LlmAgentSubnet` or `StreamingLlmAgentSubnet`) it also signals
`TURN_ABORT` so the net lets go of the turn and serves the next.
Stock `InMemoryRunner(agent)` consumes a `PetriAgent` like any other
`BaseAgent`, so existing apps swap orchestrators without touching
anything else.

### Session lifetime: `SessionExecutorRegistry`

The registry lazily creates one `PetriRunner` per
`(appName, userId, sessionId)`, so consecutive `runAsync` calls reuse the
same long-lived executor. It has two modes:

- **`strongOwned()`**, the recommended default. A runner lives until an
  explicit `close(SessionKey)` (or `closeAll()` at shutdown). A forgotten
  close is a *visible* leak: `size()` grows monotonically.
- **`cleanerOwned()`**, opt-in, for callers that hold a stable strong
  owner whose GC tracks session end. A `Cleaner` tears the runner down
  when the owner is collected, so an orphaned runner (orchestrator
  thread, hot processor, marking state) cannot leak. The catch is why it
  is opt-in: an owner held too weakly is collected mid-session, and the
  runner is torn down *silently*, turning every later `inject(...)` into
  a no-op. `ctx.session()` with `InMemorySessionService` is such an
  owner, because it returns defensive copies.

Neither mode can register a runner without a teardown route. Wiring is
one builder call; under `strongOwned()` the owner is only an identity,
so you can leave it out:

```java
var registry = SessionExecutorRegistry.strongOwned();
var agent = PetriAgent.builder("assistant", registry,
        key -> PetriRunner.builder(SubnetActions.bindComposed(net, agentBindings, routerBindings))
            .environmentPlace(AdkColours.USER_IN)
            .orchestratorExecutor(executor)
            .start())
    .description("Routes to the right specialist")
    .build();
// from the session-end hook:
registry.close(SessionKey.from(session));
```

`SubnetActions.bindComposed` binds several subnets' action maps at once
and rejects a missing, unknown or doubly-bound transition. A bare
`PetriNet.bindActions(Map)` would silently bind `passthrough()` instead.

#### Checkpoints *(experimental)*

A registry built with a `SessionCheckpointStore` (`strongOwned(store)`)
checkpoints each session when it is torn down:

- **Drain, then save.** Teardown refuses new injects, lets actions in
  flight finish, then saves the final marking without `EVENT_OUT`
  (delivered events are egress, not state).
- **Resume.** A runner factory that calls `.resumeFrom(store, key)`
  starts from that checkpoint, or from its `initialMarking` when there is
  none. Until the save lands, a `getOrCreate` for the key waits, so the
  replacement always resumes from what its predecessor left.
- **No stale checkpoints.** A run that does not drain within the
  checkpoint timeout loses its checkpoint rather than keep a stale one.
  `registry.discard(key)` ends a session without saving it.

The marking stays the state: the store is written at session end and
read before a runner starts, never during execution. The
`AgentStateCheckpointStore` exemplar keeps the checkpoint in ADK's own
session history, as an event's `EventActions.agentState`.

### Calling Gemini without `commonPool`

The async genai client and ADK's own `Gemini.generateContent` schedule
continuations on `ForkJoinPool.commonPool()`, the shared singleton this
project bans. The fix needs no fork: call genai's synchronous API on a
virtual thread. genai's sync facade (`client.models.generateContent` /
`generateContentStream`) and OkHttp's synchronous `execute()` both run on
the calling thread. A `BaseLlm` that calls genai synchronously therefore
keeps the whole call (HTTP I/O, parsing, mapping) on the thread that
invoked it, which is the orchestrator thread, since libpetri runs actions
inline. A blocking action holds that thread and serialises the net while
it runs, so a virtual-thread `orchestratorExecutor` is what keeps
blocking cheap.

```java
// build once, share across sessions, close() on shutdown
Client client = Client.builder().apiKey(apiKey).build();
BaseLlm llm  = new SyncGeminiLlm("gemini-2.0-flash", client); // small adapter; see demos/

var bound = SubnetActions.bindComposed(net, LlmStepSubnet.actionBindings(llm));
runner = PetriRunner.builder(bound)
    .environmentPlace(AdkColours.USER_IN)
    // actions run inline on this pool, so blocking the call is cheap here
    .orchestratorExecutor(Executors.newVirtualThreadPerTaskExecutor())
    .start();
```

`SyncGeminiLlm` is an exemplar under `demos/`, not library code. It
reuses ADK's public `GeminiUtil` / `LlmResponse.create` mappers, and its
server-streaming path drains genai's sync `ResponseStream` chunk by chunk
on the same virtual thread.

### BIDI and voice

*Experimental.* The BIDI plumbing splits into a shipped half and a
consumer half.

**The shipped half** is `BidiPetriAgent.bridge(liveRequestQueue,
connection, runner, onServerMessage)`. It owns the generic bidirectional
pump: it forwards inbound `LiveRequest` frames to the connection and taps
the raw server stream into your `onServerMessage` callback, which decodes
and injects. The connection is a `LiveConnection` (a shipped interface:
`BaseLlmConnection` plus `rawReceive()`, the raw `LiveServerMessage`
stream ADK's `LlmResponse` drops). Voice signals reach the net through
`runner.signal(place)`, the unit-token injection for every `Place<Void>`
edge (speech start/stop, barge-in, `END_INVOCATION`).

The bridge authors **no** events; it returns only the net's egress
(`adkEvents()`). Model content enters through the same seam as every
other signal, `runner.inject(modelChunkPlace, content)`, and a net
transition authors each outbound `Event`, setting `partial` and
`turnComplete` from the marking. Turn shape is therefore a marking-level
decision, which is what lets barge-in drop queued chunks structurally. A
bridge that mapped frames straight to events would keep model content
outside the marking, where no transition can cancel it.

Egress *ordering* becomes a marking-level decision too. A burst of
frames enters the marking in one pass, and each enabled transition then
fires at most once per pass, so a terminal transition enabled alongside
still-queued chunks would emit in between them. The fix is an arc, not a
callback convention: inhibit the terminal transition on the chunk place.

```java
Transition.builder("T_EmitFinal")
        .inputs(Arc.In.one(TURN_COMPLETE))
        .inhibitor(MODEL_CHUNK)          // no terminal while chunks are queued
        .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
        .build();
```

With that arc the decode callback stays fire-and-forget and never blocks
the transport's reader thread. This is the general argument in small: a
property that a stream-merging orchestrator can only document as a rule
for callers is, in a net, an arc that makes the violation unreachable.

**The consumer half** stays caller-side, because signal names, tool
routing and reconnect policy vary per transport. The VAD and barge-in
signals (`LiveServerContent.interrupted()`,
`LiveServerMessage.voiceActivity()`) are public on genai, but ADK's
`GeminiLlmConnection` drops the voice-activity edges. Two exemplars under
`demos/` recover them without a fork:

- **`VadTapGemini`** (preferred) keeps ADK's own `GeminiLlmConnection`
  and wraps the live transport under it through the
  `connectLiveTransport` seam ADK 1.9 added, so each voice-activity edge
  reaches your callback before ADK sees the message.
- **`SyncGeminiLiveConnection`** (full control) is a copy-and-adapt
  `LiveConnection` that reads genai's Live session directly
  (`client.async.live.connect` plus `AsyncSession.receive`), with a
  `voiceSignals(LiveServerMessage)` decoder and explicit `turnComplete`
  control. It is genai-SDK-specific, down to the websocket close quirk,
  so it is yours to own.

**Do not shadow-fork ADK to get a live connection.** Never drop copies of
`com.google.adk.models.Gemini` / `GeminiLlmConnection` onto the classpath
at ADK's own fully-qualified names to fix the `commonPool` hops or
surface the VAD edges. That is a fork by classpath shadowing and breaks
[design commitment 4](#design-commitments). Implement `LiveConnection`
and call `bridge(...)` instead.

### Why ADK and not pure libpetri?

libpetri alone can drive a composed net through its native executor, and
for some projects that is the right choice. The ADK layer adds:

- **Session model.** ADK's `Session`, `SessionService` and their
  in-memory and persistent variants provide per-user conversation state
  and the session lifecycle the ADK ecosystem expects.
- **Wire protocol.** `Content`, `Part`, `Event`, `FunctionCall` and
  `FunctionResponse` are the envelope shared with the Gemini API, the
  Agent-to-Agent (A2A) protocol and the managed deploy targets.
- **Tool ecosystem.** `BaseTool` and the adapters for the Model Context
  Protocol (MCP), A2A clients and built-in tool families work from a
  Petri-driven agent exactly as from any `BaseAgent`.
- **Deploy targets.** Vertex Agent Engine and Cloud Run agent hosting
  accept the ADK contract, so a `PetriAgent` deploys with no extra
  transport adapter.
- **Composition with non-Petri agents.** A `SequentialAgent`,
  `ParallelAgent` or `LoopAgent` can hold a `PetriAgent` as a child, so
  existing deployments can adopt the Petri runtime incrementally.
- **Evaluation.** When ADK Java grows an evaluator, a `PetriAgent` is
  already a `BaseAgent` and plugs in.

Pure libpetri solves the orchestration shape problem; the ADK layer adds
the protocol, deploy and ecosystem integration the Google agent platform
expects. Projects that need none of that can drive libpetri directly.

## Design commitments

The rest of the design rests on these. Each exists to make a class of bug
impossible to express, not merely discouraged.

1. **Interaction is env-place injection only.** Every external signal (a
   user message, a scroll event, a sensor reading, a webhook, an audio
   frame) enters a running net through `inject(envPlace, token)` on its
   own typed place. There are no method calls into transitions and no
   side channels.
2. **The marking is the state.** State lives in tokens. External stores
   (ADK `Session.state`, a database, a cache) are write-only legacy
   bridges, never read from inside the net during execution. Read arcs
   snapshot typed in-net places instead.
3. **Typed colours per domain concept.** A place carries a typed colour
   (`Place<LlmRequest>`, `Place<Content>`), never a single
   `Place<Map<String,Object>>` bag. The one bag-shaped colour,
   `LegacySessionWrite`, is named loudly and used only for the Session
   export bridge.
4. **Zero forks.** ADK is integrated through the `PetriAgent extends
   BaseAgent` adapter, never by forking it. Where a defect lives in ADK's
   wrapper over genai (the `commonPool` hops, the dropped VAD signals),
   thin user code wraps or bypasses the wrapper; nobody patches a fork
   of genai or ADK. `SyncGeminiLlm` calls genai directly for the LLM
   path. For VAD the preferred route is `VadTapGemini`, which wraps ADK's
   own live transport through the `connectLiveTransport` seam ADK 1.9
   added; `SyncGeminiLiveConnection`, a direct read of genai's Live
   session, remains for full control.
5. **Observability is an `EventStore` decorator chain.**
   `OtelEventStore`, `EventStore.logging()` and any structured-logging or
   debug-recording store wrap each other via the delegate pattern. Side
   effects live in transition actions; there is no second observability
   channel and no `observe(Place<T>)`. `PetriRunner.failureSignal()` is
   control flow, not observation: it carries `TransitionFailure`s (the
   failing transition's name, whether the action threw or blew its
   deadline, and its instance prefix when composed) so a caller can end
   an in-flight unit of work. It never carries net state, and every
   failure it reports is already on the `EventStore` chain.
6. **Autonomous loops are bounded structurally.** The reask-budget
   pattern (`Place<Void>` plus priority plus inhibitor fallback) bounds
   the LLM-and-tool loop in the topology, not with a counter in
   application state.
7. **Stock subnets are templates, not the framework.** The framework is
   the composition primitives (`PetriNet.builder().compose()` plus
   `SubnetDef.fromNet(...)`). The nine stock subnets are convenient
   starting points; you are expected to compose your own.
8. **Per-session executor lifetime is caller-owned.** A session runner
   lives in `strongOwned()` until the caller invokes `close(SessionKey)`,
   or in `cleanerOwned()` until a stable caller-owned lifetime object is
   collected. There is no shared executor singleton; runner shutdown
   drains the net and completes the hot ADK event stream.

## Verification

Every `mvn verify` runs the following. Each SMT row is a test that
asserts `isProven()`, so a claim that stops holding fails the build
instead of quietly turning into `Unknown`.

**Structural validators** (`AdkNetInvariants`, no solver needed):

- `singleLegacySessionWriter` catches parallel writes to `Session.state`,
  and `transferDemuxHasUnknownFallback` catches dead-letter accumulation.
  Both run on the multi-agent demo net. There the writer check passes
  vacuously, since that net has no `PersistStateSubnet` and so no writer;
  its own tests run it on a net with one writer and on one with two.
- `endInvocationInhibitsAll` catches advancing transitions that ignore
  the end signal. The stock subnets do not use `END_INVOCATION`, so this
  check is for your own nets; its test runs it on synthetic ones.

**SMT proofs** (libpetri's `SmtVerifier`, needs `z3`):

| What is proved | Net | Test |
|---|---|---|
| `LlmStep`, `Router`, `ToolDispatch` and `TransferRouter` are each deadlock-free and turn k inputs into exactly k outcomes; `PersistState` is deadlock-free, so it takes every write. `PromptBuilder` has no proof of its own, and the streaming pair is proved in the rows below | each of those five subnets alone, via `SubnetDef.verify` with `arrivals(k, k)` | `StockSubnetProofsTest` |
| The composed `LlmAgent` is deadlock-free, comes to rest holding only its permit, and turns k user inputs into exactly k outcomes (one answer, fallback or transfer each) | `LlmAgentSubnet` composed, `arrivals(k, k)` | `StockSubnetProofsTest` |
| One turn at a time: at most one turn in flight, one permit and one conversation; for `LlmAgentSubnet`, also a reask budget that never stacks across user inputs (commitment 6) | `LlmAgentSubnet`, two arrivals; `StreamingLlmAgentSubnet` with its chunk stream open (budget not proved there) | `StockSubnetProofsTest` |
| A failure at any step of a turn is recovered: still deadlock-free, one turn, one conversation; aborts at any moment never mint a second permit | `LlmAgentSubnet` with a failure model, and with `TURN_ABORT` arrivals | `StockSubnetProofsTest` |
| Deadlock-free with the chunk stream open: every request is taken and every chunk drains to an event or the merged response | `LlmStreamingStepSubnet`, two requests | `LlmStreamingStepSubnetTest` |
| Deadlock-free; at most one egress event per turn (`eventOutBounded`) | multi-agent demo net | `MultiAgentDemoTest` |
| Deadlock-free | voice demo composition without its Router (streaming step, barge-in, Live-API recovery, `StartStream`) | `VoiceSessionDemoTest` |
| One winner per turn: one race commit and one race event; one quorum synthesis and one quorum event; one optimistic commit, with mutually exclusive validation verdicts. Each net is deadlock-free | the three pattern demos, one turn | `Pattern{A,B,C}_*DemoTest` |
| The race permit never stacks across turns | Pattern A, two arrivals | `PatternA_SpeculativeRaceDemoTest` |

**State space** (no solver needed): `StateClassGraph.build(net, initial, 256)`
completes within its exploration cap on the composed BIDI subnets
(`LlmStreamingStep`, `BargeIn` and `LiveApiRecovery`, in
`LiveApiRecoverySubnetTest`), confirming a bounded reachable state space.

The tests assert the proof result rather than relying on example traces.

### How to read the proofs

- **One property per `verify()`.** Each row's properties are proved one
  call at a time, through the test helper `SmtProofs` or libpetri's
  `VerificationHarness`. `SmtVerifier.property(p)` replaces the property
  rather than adding one, so a chain of `.property(...)` calls checks
  only the last.
- **In-flight split, with one exception.** Every proof but one runs with
  libpetri 8.0's in-flight split. Whenever another transition tests a
  transition's output with an inhibitor, reset or drain, the verifier
  models that transition as a start step and a completion step, because
  the executor fires other transitions in between. A synchronous action
  does not close that gap: its outputs land at the end of the firing
  pass, and an inhibitor or reset earlier in the pass does not see them.
  The exception is the race permit, which assumes atomic firing. For it
  the assumption is exact: without it, the only counterexample restarts
  the seed transition while an earlier firing of it is still in flight,
  which the Java executor never does (libpetri CONC-002), and libpetri's
  report says so. The reask budget used to be a second exception; under
  the turn permit, which `StartTurn` consumes, no second seed can start,
  and its bound proves with the split.
- **Pattern bounds are per turn.** The demos do not tag branch results
  with the turn that started them, so a turn that starts while the
  previous one is still committing can see that commit land after its
  reset. Only the permit bound is claimed across turns.
- **Budget bounds are stated in seeds.** libpetri has no weighted output
  arc, so a seed transition that writes N permits is modelled as writing
  one, and a bound of N would hold trivially. The property that matters
  is that the place never holds more than one seed's worth, which fails
  when a second seed can land before the first is cleared.
- **Primitives stay primitives.** Anything already expressible as a
  libpetri primitive stays one: mutual exclusion is
  `SmtProperty.mutualExclusion`, not a wrapper that only adds null
  checks.

## Development

```bash
cd java
./mvnw verify
```

The Java suite covers unit, integration, demo and verification tests.
The SMT proof tests need a `z3` binary (4.8 or later) on `PATH` or named
by `LIBPETRI_Z3`; libpetri runs it as an external process. Those tests
carry `@EnabledIf("z3Available")`, so the build passes without Z3. The
state-class-graph check needs no solver and always runs. That skip is a convenience for contributors, not for
CI: the workflow installs `z3` and sets `REQUIRE_Z3`, which makes
`Z3NativeGateTest` fail hard if the binary is missing. Without that gate
the verification suite could vanish while the badge stayed green.

The diagrams are generated from [`docs/diagrams/`](docs/diagrams/) with
the libpetri npm package's `dotExport` (`npm install && npm run build`; needs Node.js
20+ and graphviz `dot`). Design decisions and version-compatibility
records are in [`docs/adr/`](docs/adr/).

### Languages

| | Status |
|---|---|
| **Java** | Working. See [`java/`](java/) |
| TypeScript | Reserved |
| Rust       | Reserved |
| Python     | Reserved (matches `adk-python` SDK reach) |

The repo follows [libpetri](https://github.com/debe/libpetri)'s
multi-language layout: a port goes in a sibling subdirectory
(`typescript/`, `rust/`, `python/`).

## Consuming from a project: protobuf version floor

ADK 1.10.1's transitives (the `com.google.cloud` clients such as
`google-cloud-aiplatform` and `google-cloud-storage`, and
`com.google.api.grpc:proto-google-common-protos`) ship protobuf gencode compiled against 4.33.x.
The protobuf runtime contract is "runtime at least linked gencode," so a
consumer that pins protobuf-java to an older version hits
`ProtobufRuntimeVersionException` at first class-load, typically inside
an apparently unrelated dependency. The failure stays silent until that
load, and the stack trace points at the consumer's code rather than at
the version pin that caused the downgrade.

adk-libpetri pins protobuf-java to 4.33.5 via `dependencyManagement` in
its own POM, so direct consumers get the right version transitively.
Consumers using an enforced platform BOM (Helidon's `enforcedPlatform`,
Spring Boot's BOM in strict mode) must add an explicit override to undo
the BOM's downgrade. Gradle:

```groovy
configurations.all {
    resolutionStrategy {
        force 'com.google.protobuf:protobuf-java:4.33.5'
        force 'com.google.protobuf:protobuf-java-util:4.33.5'
    }
}
```

Maven consumers using a BOM should add an explicit
`<dependencyManagement>` entry for the same coordinates, since the
nearest-wins rules favour the consumer's BOM over a transitive's. The
same applies to any other gencode-bumped dependency. Guava is one to
watch: it resolves to 33.7.x but is commonly managed to 32.x.

The version-compatibility ADRs record which ADK version each claim here
was verified against, what changed between ADK releases, and how to
re-check on the next bump. The most recent is
[ADR 0004](docs/adr/0004-libpetri-8-and-adk-1.10.md); the re-check
procedure lives in [ADR 0002](docs/adr/0002-adk-version-compat.md).

## Relationship to libpetri

adk-libpetri consumes libpetri from Maven Central
(`org.libpetri:libpetri:8.0.0`). It is a sibling project, not a fork.
The shared design principles (env-place-only interaction, typed colours
per concept, marking-as-state, `EventStore`-decorated observability) come
from libpetri and apply identically here.

## License

Apache 2.0. See [`LICENSE`](LICENSE).
