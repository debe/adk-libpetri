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

0.x: a minor may break API. The turn-based path is the settled part; the
SSE and BIDI surfaces below are `@Experimental`.

## Quickstart: hello world

```java
import com.google.adk.agents.RunConfig;
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

var llm = /* your com.google.adk.models.BaseLlm */;

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
  Event flows back. There is no NPE.
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
- *Z3 deadlock-free proof.* The composed voice net, with its six env places
  modelled as `bounded(1)`, is proved deadlock-free.


See the root [`README.md`](../README.md) for the architectural
rationale, the design commitments, and the full subnet catalog.
