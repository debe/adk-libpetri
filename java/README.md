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
| `org.libpetri:libpetri`              | 2.10.4 (Maven Central) |
| `com.google.adk:google-adk`          | 1.4.0  (Maven Central) |
| `io.reactivex.rxjava3:rxjava`        | 3.1.12 |
| `io.opentelemetry:opentelemetry-*`   | 1.63.0 (transitive via google-adk and libpetri) |

Z3 (`com.microsoft.z3`) comes transitively from libpetri's
`org.sosy-lab:javasmt-solver-z3`. SMT-using tests are gated via
`@EnabledIf("z3Available")` so the build passes without native Z3
libs installed.

## Quickstart: hello world

```java
import com.google.adk.runner.InMemoryRunner;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.runner.PetriAgent;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.adk.runner.SessionExecutorRegistry;
import org.libpetri.adk.runner.SessionKey;
import org.libpetri.core.PetriNet;
import java.util.concurrent.Executors;

var llm     = /* your com.google.adk.models.BaseLlm */;
var config  = LlmAgentSubnet.Config.builder("my_agent", "gemini-2.0-flash")
                .systemInstruction("Be helpful.")
                .reaskBudget(3)
                .build();

var net = PetriNet.builder("hello")
        .compose(LlmAgentSubnet.DEF)
        .build()
        .bindActions(LlmAgentSubnet.actionBindings(llm, config));

var actionExecutor = Executors.newVirtualThreadPerTaskExecutor();
var orchestratorExecutor = Executors.newSingleThreadExecutor();

// strongOwned() is the recommended default: close the registry entry from
// your session-end hook. The owner map supplies the stable identity object
// SessionExecutorRegistry requires for repeated calls in the same session.
var registry = SessionExecutorRegistry.strongOwned();
var sessionOwners = new java.util.concurrent.ConcurrentHashMap<SessionKey, Object>();

var agent = PetriAgent.of("my_agent", "Petri-backed agent",
        registry,
        key -> PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .actionExecutor(actionExecutor)
                .orchestratorExecutor(orchestratorExecutor)
                .start(),
        ctx -> sessionOwners.computeIfAbsent(SessionKey.from(ctx.session()), k -> new Object()));

// Drop into stock ADK Runner, no fork:
var runner = new InMemoryRunner(agent);
var session = runner.sessionService()
        .createSession(runner.appName(), "user", null, "session").blockingGet();

runner.runAsync(session.userId(), session.id(),
        Content.fromParts(Part.fromText("hi")),
        RunConfig.builder().build())
    .blockingForEach(e -> System.out.println(e.stringifyContent()));
```

## Streaming (SSE)

Use `StreamingLlmAgentSubnet` when the ADK turn should expose token
partials. Declare `LlmStreamingStepSubnet.Places.CHUNK` as an env place,
pass the same executor reference to the subnet config and
`PetriRunner.Builder.deferredExecutorRef(...)`, then run with
`RunConfig.StreamingMode.SSE`.

```java
var execRef = new java.util.concurrent.atomic.AtomicReference<org.libpetri.runtime.PetriNetExecutor>();
var config = StreamingLlmAgentSubnet.Config.builder("my_agent", "gemini-2.0-flash")
        .dispatchExecutor(actionExecutor)
        .chunkBudget(4)
        .executorRef(execRef)
        .build();

var net = PetriNet.builder("streaming")
        .compose(StreamingLlmAgentSubnet.DEF)
        .build()
        .bindActions(StreamingLlmAgentSubnet.actionBindings(llm, config));

var agent = PetriAgent.of("my_agent", "Streaming agent", registry,
        key -> PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .environmentPlace(LlmStreamingStepSubnet.Places.CHUNK)
                .deferredExecutorRef(execRef)
                .actionExecutor(actionExecutor)
                .orchestratorExecutor(orchestratorExecutor)
                .start(),
        ctx -> sessionOwners.computeIfAbsent(SessionKey.from(ctx.session()), k -> new Object()));

runner.runAsync(session.userId(), session.id(), userContent,
        RunConfig.builder().streamingMode(RunConfig.StreamingMode.SSE).build())
    .blockingForEach(event -> {
        if (event.partial().orElse(false)) System.out.print(event.content().get().text());
    });
```

## Live (BIDI)

Use `PetriAgent.ofLive(...)` when ADK `runLive` should pump a
`LiveRequestQueue` into a genai-backed `LiveConnection` and merge raw live
server messages with net egress.

```java
var liveConfig = new PetriAgent.LiveConfig(
        ctx -> openLiveConnection(ctx),
        (serverMessage, petriRunner) -> {
            // Decode provider frames and inject net signals/tool results here.
        });

var liveAgent = PetriAgent.ofLive("my_agent", "Live agent", registry,
        runnerFactory,
        ctx -> sessionOwners.computeIfAbsent(SessionKey.from(ctx.session()), k -> new Object()),
        liveConfig);

inMemoryRunner.runLive(session, liveRequestQueue,
        RunConfig.builder().streamingMode(RunConfig.StreamingMode.BIDI).build())
    .blockingForEach(event -> handleLiveEvent(event));
```

`PetriAgent.of(...)` still preserves the legacy egress-only `runLive`
surface. `LiveConnection` is intentionally genai Live-message typed; ship
your transport binding in application code.


## Source layout

```
src/main/java/org/libpetri/adk/
├── colours/AdkColours.java                # typed boundary catalog
├── bridge/
│   ├── EventStoreToFlowableBridge.java    # EventStore to Flowable<Event>
│   └── OtelEventStore.java                # OT span per transition fire
├── subnet/                                # stock subnets + binding helpers
│   ├── LlmStepSubnet.java                 # LLM call + Before/After/Error callbacks
│   ├── LlmStreamingStepSubnet.java        # streaming LLM call, chunk env place, final response
│   ├── StreamingLlmAgentSubnet.java       # SSE agent loop counterpart to LlmAgentSubnet
│   ├── ToolDispatchSubnet.java            # trivial parallel tool dispatch
│   ├── PromptBuilderSubnet.java           # Content + tools to LlmRequest
│   ├── RouterSubnet.java                  # LlmResponse to Out.xor(tools, transfer, final)
│   ├── LlmAgentSubnet.java                # composes the above + reask budget
│   ├── PersistStateSubnet.java            # single legacy-session writer
│   ├── TransferRouterSubnet.java          # Out.xor over compile-time agent names
│   └── SubnetActions.java                 # binding-map validator
├── runner/
│   ├── PetriRunner.java                   # per-session NetExecutor handle
│   ├── PetriAgent.java                    # normal, SSE, and live BaseAgent adapter
│   ├── BidiPetriAgent.java                # Live/BIDI pump used by PetriAgent.ofLive
│   ├── LiveConnection.java                # genai Live server-message boundary
│   ├── SessionExecutorRegistry.java       # lazy Map<SessionKey, PetriRunner>
│   └── SessionKey.java                    # (appName, userId, sessionId)
└── verify/AdkNetInvariants.java           # 3 structural + 3 SMT property factories
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
- *Z3 deadlock-free proof.* `SmtProperty.deadlockFree()` runs on the
  composed net with sinks declared. Spacer says: not violated.

### `VoiceSessionDemoTest`

`LlmStreamingStepSubnet` plus `RouterSubnet`, `BargeInSubnet`, and
`LiveApiRecoverySubnet` are composed into one long-lived per-session net
with typed env places.

- *Full streaming voice scenario.* Three streamed chunks arrive via real
  env-place injection. A terminal router event completes the ADK turn, a
  user barge-in fires mid-stream (voice-activity-gated route), and a
  silence-triggered two-stage recovery follows (nudge then reconnect). The
  budget invariant holds: `CHUNK_BUDGET` returns to K at quiescence.
- *BIDI bridge scenario.* A custom `BaseLlmConnection` records sends and
  pumps model frames back into the net.
- *Reset arc pattern.* Stale-state cleanup on new utterance.
- *SMT boundedness check.* Spacer checks the composed voice net's streaming
  chunk budget property.


See the root [`README.md`](../README.md) for the architectural
rationale, the design commitments, and the full subnet catalog.
