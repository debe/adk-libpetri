# adk-libpetri: Java

Java implementation of adk-libpetri. Replaces Google ADK Java's
orchestration core with a Coloured Time Petri Net built on
[libpetri](https://github.com/debe/libpetri). Stock ADK `Runner`
consumes the result via a `PetriAgent extends BaseAgent` adapter.
There is no ADK fork.

## Build

```bash
./mvnw verify                                    # tests + verification
./mvnw test                                      # tests only
./mvnw test -Dtest="MultiAgentDemoTest"          # single class
```

Java 25, Maven 3.9.x via wrapper.

## Dependencies

| | Version |
|---|---|
| `org.libpetri:libpetri`              | 8.0.0  (Maven Central) |
| `com.google.adk:google-adk`          | 1.10.1 (Maven Central) |
| `com.google.genai:google-genai`      | 1.58.0 (transitive via google-adk) |
| `io.reactivex.rxjava3:rxjava`        | 3.1.12 |
| `io.opentelemetry:opentelemetry-api` | 1.51.0 (transitive via google-adk and libpetri); tests pin `opentelemetry-sdk-testing` 1.65.0 |

Since libpetri 4.0, SMT verification runs an external `z3` binary
(4.8 or later, on `PATH` or named by `LIBPETRI_Z3`); there are no JNI
natives and no Z3 Maven artifact. SMT-using tests are gated via
`@EnabledIf("z3Available")`, which delegates to
`SmtVerifier.z3Available()`, so the build passes without Z3 installed.
CI does not take that shortcut: it installs `z3` and sets `REQUIRE_Z3`,
so `Z3NativeGateTest` fails the build rather than letting the
verification suite skip silently.

### Protobuf version floor

ADK 1.10.1's transitives (the `com.google.cloud` clients such as
`google-cloud-aiplatform` and `google-cloud-storage`, and
`com.google.api.grpc:proto-google-common-protos`) ship protobuf gencode
compiled against 4.33.x. The protobuf runtime contract is "runtime at
least linked gencode," so a consumer that pins protobuf-java to an older
version hits `ProtobufRuntimeVersionException` at first class-load,
typically inside an apparently unrelated dependency. The failure stays
silent until that load, and the stack trace points at the consumer's code
rather than at the version pin that caused the downgrade.

adk-libpetri pins protobuf-java and protobuf-java-util to 4.33.5 via
`dependencyManagement` in its own POM, so direct consumers get the right
version transitively. Consumers using an enforced platform BOM (Helidon's
`enforcedPlatform`, Spring Boot's BOM in strict mode) must add an
explicit override to undo the BOM's downgrade. Gradle:

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

The re-check procedure in
[ADR 0002](../docs/adr/0002-adk-version-compat.md#next-bump-re-check-procedure)
checks the resolved protobuf-java version on every ADK bump.

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

Not published yet: `org.libpetri:adk-libpetri` is not on Maven Central.
Until the first tagged release (`java/v<version>`), build from source with
`./mvnw install` in this directory, which installs these coordinates into
your local Maven repository.

0.x: a minor may break API. The turn-based path is the settled part; the
SSE and BIDI surfaces below are `@Experimental`.

## Quickstart: hello world

```java
import com.google.adk.agents.RunConfig;
import com.google.adk.models.BaseLlm;
import com.google.adk.runner.InMemoryRunner;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import java.util.Map;
import java.util.concurrent.Executors;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.runner.PetriAgent;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.adk.runner.SessionExecutorRegistry;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.subnet.SubnetActions;
import org.libpetri.core.PetriNet;

BaseLlm llm = yourModel();  // any com.google.adk.models.BaseLlm

// Actions are invoked inline on the orchestrator thread, so this is the
// pool that runs them: make it virtual-threaded when actions block.
var orchestratorExecutor = Executors.newVirtualThreadPerTaskExecutor();
// Required: the pool tool dispatch fans out on, inside an action.
var dispatchExecutor = Executors.newVirtualThreadPerTaskExecutor();

var config = LlmAgentSubnet.Config.builder("my_agent", "gemini-2.5-flash")
        .systemInstruction("Be helpful.")
        .reaskBudget(3)
        .dispatchExecutor(dispatchExecutor)
        .build();

// bindComposed checks the bindings against the composed net's transitions;
// a bare bindActions(Map) would turn a missing binding into a silent no-op.
var net = SubnetActions.bindComposed(
        PetriNet.builder("hello").compose(LlmAgentSubnet.DEF).build(),
        LlmAgentSubnet.actionBindings(llm, config));

// strongOwned() is the documented default: a session's runner lives until
// registry.close(key) from your session-end hook (closeAll() at shutdown).
// It needs no owner object.
var registry = SessionExecutorRegistry.strongOwned();

var agent = PetriAgent.builder("my_agent", registry,
                key -> PetriRunner.builder(net)
                        .environmentPlace(AdkColours.USER_IN)
                        .orchestratorExecutor(orchestratorExecutor)
                        .start())
        .description("Petri-backed agent")
        .build();

// Drop into stock ADK Runner, no fork:
var runner = new InMemoryRunner(agent);
var session = runner.sessionService()
        .createSession(runner.appName(), "user", (Map<String, Object>) null, "session")
        .blockingGet();

runner.runAsync(session.userId(), session.id(),
        Content.fromParts(Part.fromText("hi")),
        RunConfig.builder().build())
    .blockingForEach(e -> System.out.println(e.stringifyContent()));
```

## How PetriAgent drives a turn

`PetriAgent` (`src/main/java/org/libpetri/adk/runner/PetriAgent.java`) is
the turn-based integration seam. Its two run paths, abridged (the real
methods also open an OpenTelemetry invocation span when tracing is wired,
and fail the turn if the egress completes without a terminal event):

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

Both `runAsyncImpl` branches attach their subscriber to the hot egress
before injecting `USER_IN`, then return the `Flowable` at once; the
turn's events arrive through it later, as net transitions emit them to
`EVENT_OUT`.

The registry wiring for a composed net. Under `strongOwned()` the owner is
only an identity, so the builder needs no `ownerExtractor`; a
`cleanerOwned()` registry needs `.ownerExtractor(...)`, and `build()`
rejects it otherwise:

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
The root README explains the two registry modes in
[Session lifetime](../README.md#session-lifetime-sessionexecutorregistry).

## Calling Gemini without `commonPool`

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
BaseLlm llm = new SyncGeminiLlm("gemini-2.5-flash", client); // small adapter; see demos/

// A bare LlmStep: inject an LlmRequest, read the reply on LLM_RESPONSE.
// For a full agent, pass the same llm to LlmAgentSubnet.actionBindings.
var bound = SubnetActions.bindComposed(
        PetriNet.builder("llm-step").compose(LlmStepSubnet.DEF).build(),
        LlmStepSubnet.actionBindings(llm));
var runner = PetriRunner.builder(bound)
        .environmentPlace(AdkColours.LLM_REQUEST)
        // actions run inline on this pool, so blocking the call is cheap here
        .orchestratorExecutor(Executors.newVirtualThreadPerTaskExecutor())
        .start();
```

[`SyncGeminiLlm`](src/test/java/org/libpetri/adk/demos/SyncGeminiLlm.java)
is an exemplar under `demos/`, not library code. It reuses ADK's public
`GeminiUtil` / `LlmResponse.create` mappers, and its server-streaming path
drains genai's sync `ResponseStream` chunk by chunk on the same virtual
thread.
[`SyncGeminiLlmTest`](src/test/java/org/libpetri/adk/demos/SyncGeminiLlmTest.java)
pins the threading (`synchronous_call_runs_on_action_thread_not_commonPool`,
`sync_gemini_llm_runs_real_genai_call_on_caller_thread`) and the stream
handling (`gemini3_stream_terminator_is_dropped_but_real_text_is_kept`).

## Streaming (SSE)

> **Beta.** `LlmStreamingStepSubnet` and `StreamingLlmAgentSubnet` are
> `@Experimental`: they may change incompatibly within a 0.x minor.


Use `StreamingLlmAgentSubnet` when the ADK turn should expose token
partials, and wire it with `StreamingLlmAgentSubnet.runnerFactory(...)`.
Chunks reach the net through the session's own executor, so each session
needs its own executor reference; the factory creates one per session,
binds the actions to it, declares `USER_IN` and
`LlmStreamingStepSubnet.Places.CHUNK` as env places and registers the
reference with `deferredExecutorRef`. One reference shared by every
session's runner routes all chunks into whichever runner started last.
Then run with `RunConfig.StreamingMode.SSE`.

```java
var streamingConfig = StreamingLlmAgentSubnet.Config.builder("my_agent", "gemini-2.5-flash")
        .dispatchExecutor(dispatchExecutor)
        .build();

var streamingAgent = PetriAgent.builder("my_agent", registry,
                StreamingLlmAgentSubnet.runnerFactory(llm, streamingConfig,
                        // Runs before the factory's own settings, with the
                        // session's key: the place for an event store or
                        // .resumeFrom(store, key).
                        (key, b) -> b.orchestratorExecutor(orchestratorExecutor)))
        .description("Streaming agent")
        .build();

new InMemoryRunner(streamingAgent).runAsync(session.userId(), session.id(), userContent,
        RunConfig.builder().streamingMode(RunConfig.StreamingMode.SSE).build())
    .blockingForEach(event -> {
        if (event.partial().orElse(false)) System.out.print(event.content().get().text());
    });
```

## Live (BIDI)

> **Beta.** `BidiPetriAgent`, `LiveConnection`, `PetriAgent.LiveConfig` and
> `PetriAgent.ofLive` are `@Experimental`: they may change incompatibly
> within a 0.x minor. `bridge` already took one such break in 0.4.0 (see
> the CHANGELOG).


Give the builder a `LiveConfig` when ADK `runLive` should pump a
`LiveRequestQueue` into a genai-backed `LiveConnection`. The bridge
authors no events: it hands each raw server message to your callback,
which injects model content and signals into the net, and `runLive`
returns the net's egress alone. Every `Event`, including its `partial` and
`turnComplete` flags, is authored by a net transition.

```java
var liveConfig = new PetriAgent.LiveConfig(
        ctx -> openLiveConnection(ctx),
        (serverMessage, petriRunner) -> {
            // Decode provider frames: inject model content, signal turn and
            // voice-activity edges, route tool calls.
        });

var liveAgent = PetriAgent.builder("my_agent", registry, runnerFactory)
        .description("Live agent")
        .live(liveConfig)
        .build();

inMemoryRunner.runLive(session, liveRequestQueue,
        RunConfig.builder().streamingMode(RunConfig.StreamingMode.BIDI).build())
    .blockingForEach(event -> handleLiveEvent(event));
```

Without a `LiveConfig`, `runLive` keeps the legacy egress-only surface:
it returns the net's egress and pumps nothing. `LiveConnection` is
intentionally genai Live-message typed; ship your transport binding in
application code.

### Getting voice-activity edges

The VAD and barge-in signals (`LiveServerContent.interrupted()`,
`LiveServerMessage.voiceActivity()`) are public on genai, but ADK Java
1.10.1's `GeminiLlmConnection` maps a VAD-only frame to an "Unknown server
message" error, so the speech-activity edges never reach a `receive()`
consumer
([`VoiceVadEdgeAdkFoilTest`](src/test/java/org/libpetri/adk/demos/VoiceVadEdgeAdkFoilTest.java)).
Two exemplars under `src/test/java/org/libpetri/adk/demos/` recover them
without a fork:

- **[`VadTapGemini`](src/test/java/org/libpetri/adk/demos/VadTapGemini.java)**
  (preferred) keeps ADK's own `GeminiLlmConnection` and wraps the live
  transport under it through the `connectLiveTransport` seam ADK 1.9
  added, so each voice-activity edge reaches your callback before ADK sees
  the message. It extends `Gemini` and is not a `LiveConnection`, so it
  does not go through `bridge`: its callback signals the net's env places
  directly.
  [`VadTapGeminiTest`](src/test/java/org/libpetri/adk/demos/VadTapGeminiTest.java)
  covers it.
- **[`SyncGeminiLiveConnection`](src/test/java/org/libpetri/adk/demos/SyncGeminiLiveConnection.java)**
  (full control) is a copy-and-adapt `LiveConnection` that reads genai's
  Live session directly (`client.async.live.connect` plus
  `AsyncSession.receive`), with a `voiceSignals(LiveServerMessage)`
  decoder and explicit `turnComplete` control. It is genai-SDK-specific,
  down to the websocket close quirk, so it is yours to own.

Do not shadow-fork ADK to get a live connection: copies of
`com.google.adk.models.Gemini` / `GeminiLlmConnection` at ADK's own
fully-qualified names are a fork by classpath shadowing and break
[design commitment 4](../README.md#design-commitments). Implement
`LiveConnection` and call `bridge(...)` instead.

## ADK Java 1.10.1 behaviour the argument relies on

The root README's ADK-side paragraphs rest on these facts about the
`google-adk` 1.10.1 bytecode. No test pins them; re-check them with
`javap -c -p` on the new jar at every ADK bump, as part of the procedure
in [ADR 0002](../docs/adr/0002-adk-version-compat.md#next-bump-re-check-procedure),
and update the "ADK 1.10.1" mentions with them.

- **`ParallelAgent.runAsyncImpl` is
  `Flowable.merge(branches).takeUntil(escalate)`.** The first branch to
  escalate ends the merge and disposes the rest: first-escalation-wins.
  Escalate gives no preference order, no K-of-N without a check-and-act
  counter in `session.state`, and no provable at-most-once commit
  ([G4](../README.md#g4-at-most-one-commit-per-turn-race-optimistic-commit-quorum)).
- **`InvocationContext.endInvocation` is a plain non-volatile `boolean`.**
  It is copied per agent run (`toBuilder`) and again per `ParallelAgent`
  branch, so `setEndInvocation(true)` in one branch is invisible to
  siblings and parent. Only `BaseAgent.run`, `BaseLlmFlow.runOneStep` and
  `BaseLlmFlow.runLive` read it; `BaseLlmFlow.run` does not
  ([N1](../README.md#n1-not-yet-guaranteed-staleness-across-turns)).
- **`Functions` runs tool calls with `Observable.concatMapEager` or
  `concatMapMaybe`**, chosen by `RunConfig.toolExecutionMode`, and never
  reads `endInvocation` ([N1](../README.md#n1-not-yet-guaranteed-staleness-across-turns)).
- **Fan-out width is fixed before the run.** ADK's idiom is
  `SequentialAgent(ParallelAgent(sub-agents with outputKey), synthesizer)`,
  whose branch count is fixed at build time. RxJava's
  `Flowable.zip(Iterable, fn)` takes a variable count, but fixes it at
  subscription ([N2](../README.md#n2-not-yet-guaranteed-variable-n-fan-out)).

[ADR 0004](../docs/adr/0004-libpetri-8-and-adk-1.10.md) records the move
to ADK 1.10.

## Source layout

```
src/main/java/org/libpetri/adk/
├── Experimental.java                      # marks surfaces that may change in any 0.x minor
├── colours/AdkColours.java                # typed boundary catalog
├── bridge/
│   ├── EventStoreToFlowableBridge.java    # EventStore to Flowable<Event>
│   ├── OtelEventStore.java                # OT span per transition fire
│   └── TransitionFailure.java             # a failed transition, as PetriRunner.failureSignal() emits it
├── subnet/                                # stock subnets + binding helpers
│   ├── LlmStepSubnet.java                 # LLM call + Before/After/Error callbacks
│   ├── LlmStreamingStepSubnet.java        # streaming LLM call, chunk env place, final response
│   ├── LlmRequests.java                   # package-private LlmRequest factory for the stock subnets
│   ├── StreamingLlmAgentSubnet.java       # SSE agent loop counterpart to LlmAgentSubnet
│   ├── ToolDispatchSubnet.java            # trivial parallel tool dispatch
│   ├── PromptBuilderSubnet.java           # Content + tools to LlmRequest
│   ├── RouterSubnet.java                  # LlmResponse to Out.xor(tools, transfer, final)
│   ├── LlmAgentSubnet.java                # composes the above + reask budget + turn permit
│   ├── PersistStateSubnet.java            # single legacy-session writer
│   ├── TransferRouterSubnet.java          # Out.xor over compile-time agent names
│   └── SubnetActions.java                 # binding-map validator, merge, bindComposed
├── runner/
│   ├── PetriRunner.java                   # per-session NetExecutor handle
│   ├── PetriAgent.java                    # normal, SSE, and live BaseAgent adapter
│   ├── BidiPetriAgent.java                # Live/BIDI pump used by PetriAgent.ofLive
│   ├── LiveConnection.java                # genai Live server-message boundary
│   ├── SessionExecutorRegistry.java       # lazy Map<SessionKey, PetriRunner>
│   ├── SessionCheckpointStore.java        # drain-then-save checkpoints for resumeFrom (experimental)
│   └── SessionKey.java                    # (appName, userId, sessionId)
└── verify/AdkNetInvariants.java           # 3 structural checks + 2 SMT property factories
```

Voice-specific demo subnets (`BargeInSubnet`, `LiveApiRecoverySubnet`,
`VadSubnet`) live under `src/test/java/org/libpetri/adk/demos/voice/`.
They are exemplars. `LlmStreamingStepSubnet` is now shipped library code
under `src/main/java/org/libpetri/adk/subnet/` because `StreamingLlmAgentSubnet`
uses it for SSE.

## Two end-to-end demos

Both live in `src/test/java/org/libpetri/adk/demos/` and run on
every `mvn verify`.

### `MultiAgentDemoTest`

Planner `LlmAgentSubnet` composed with `TransferRouterSubnet`
(compile-time-known specialists), driven through stock
`InMemoryRunner` with full OpenTelemetry observability.

- *Happy path.* Planner text routes through the Router and out to
  `EVENT_OUT`. Spans for BuildPrompt, LlmCall, and Route are
  captured.
- *Hallucinated agent name.* The planner emits a transfer to a
  garbage name, which demuxes to `UNKNOWN_TARGET`. A typed error
  Event flows back.
- *Z3 proofs.* `SmtProperty.deadlockFree()` and
  `AdkNetInvariants.eventOutBounded(1)` are each proved on the composed
  net, one `verify()` per property, and the test asserts `isProven()`.

### `VoiceSessionDemoTest`

`LlmStreamingStepSubnet` plus `RouterSubnet`, `BargeInSubnet`, and
`LiveApiRecoverySubnet` are composed into one long-lived per-session net
with typed env places.

- *Full streaming voice scenario.* Three streamed chunks arrive via real
  env-place injection. A terminal router event completes the ADK turn, a
  user barge-in fires mid-stream (voice-activity-gated route), and a
  silence-triggered two-stage recovery follows (nudge then reconnect).
- *BIDI bridge scenario.* A custom `BaseLlmConnection` records sends and
  pumps model frames back into the net.
- *Reset arc pattern.* Stale-state cleanup on new utterance.
- *Z3 deadlock-free proof.* The composed voice net, with its seven env places
  modelled as `bounded(1)`, is proved deadlock-free.

#### Silence-ladder timing tests

The recovery ladder's timers are tested on `ManualClock` in
[`LiveApiRecoverySubnetTest`](src/test/java/org/libpetri/adk/demos/voice/LiveApiRecoverySubnetTest.java),
so each timer fires at an exact logical instant. Most cases use an 80 ms
config; one uses the 3 s defaults.

- `silent_model_triggers_nudge_then_reconnect`: nothing at 79 ms, the
  nudge at 80 ms, the reconnect 80 ms after the nudge.
- `model_active_inhibits_nudge`,
  `model_active_resumes_mid_window_blocks_both_recovery_stages` and
  `model_active_appears_between_nudge_and_reconnect_stops_recovery`:
  activity holds off one rung or both.
- `model_answered_then_silence_then_fresh_response_awaited_nudges_after_3s`
  (default config): an answer cancels the rung, `MODEL_QUIET` clears
  activity, a fresh `RESPONSE_AWAITED` nudges at exactly 3 s, and an
  answer inside the reconnect window cancels the second rung.
- `stale_response_awaited_after_a_reply_never_nudges`: a
  `RESPONSE_AWAITED` that arrives while the model talks is cancelled at
  once.
- `model_quiet_clears_model_active`: two activity tokens clear in one
  firing, and repeated quiet signals leave one `QUIET_IGNORED` token.


See the root [`README.md`](../README.md) for the architectural
rationale, the design commitments, and the full subnet catalog.
