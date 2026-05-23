# adk-libpetri: Java

Java implementation of adk-libpetri. Replaces Google ADK Java's
orchestration core with a Coloured Time Petri Net built on
[libpetri](https://github.com/debe/libpetri). Stock ADK `Runner`
consumes the result via a `PetriAgent extends BaseAgent` adapter.
There is no ADK fork.

## Build

```bash
./mvnw verify                                    # 173 tests, ~5s
./mvnw test                                      # tests only
./mvnw test -Dtest="MultiAgentDemoTest"          # single class
```

Java 25, Maven 3.9.x via wrapper.

## Dependencies

| | Version |
|---|---|
| `org.libpetri:libpetri`              | 2.5.0  (Maven Central) |
| `com.google.adk:google-adk`          | 1.3.0  (Maven Central) |
| `io.reactivex.rxjava3:rxjava`        | 3.1.12 |
| `io.opentelemetry:opentelemetry-*`   | 1.51.0 (transitive via google-adk and libpetri) |

Z3 (`com.microsoft.z3`) comes transitively from libpetri's
`org.sosy-lab:javasmt-solver-z3`. SMT-using tests are gated via
`@EnabledIf("z3Available")` so the build passes without native Z3
libs installed.

## Quickstart: hello world

```java
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.runner.PetriAgent;
import org.libpetri.adk.runner.PetriRunner;
import org.libpetri.adk.runner.SessionExecutorRegistry;
import org.libpetri.core.PetriNet;
import com.google.adk.runner.InMemoryRunner;

var llm     = /* your com.google.adk.models.BaseLlm */;
var config  = LlmAgentSubnet.Config.builder("my_agent", "gemini-2.0-flash")
                .systemInstruction("Be helpful.")
                .reaskBudget(3)
                .build();

var net = PetriNet.builder("hello")
        .compose(LlmAgentSubnet.DEF)
        .build()
        .bindActions(LlmAgentSubnet.actionBindings(llm, config));

var registry = new SessionExecutorRegistry();

// Lifetime owners: one stable identity object per session.
// When an owner is collected, a Cleaner tears the per-session runner
// down (orchestrator thread, hot processor, marking state). Pick an
// object whose GC corresponds to "session ended". In a real app this
// is typically your websocket-session or connection handler.
var sessionOwners = new java.util.concurrent.ConcurrentHashMap<SessionKey, Object>();

var agent = PetriAgent.of("my_agent", "Petri-backed agent",
        registry,
        key -> PetriRunner.builder(net).start(),
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

## Source layout

```
src/main/java/org/libpetri/adk/
├── colours/AdkColours.java                # typed boundary catalog
├── bridge/
│   ├── EventStoreToFlowableBridge.java    # EventStore to Flowable<Event>
│   └── OtelEventStore.java                # OT span per transition fire
├── subnet/                                # 7 stock subnets + validator
│   ├── LlmStepSubnet.java                 # LLM call + Before/After/Error callbacks
│   ├── ToolDispatchSubnet.java            # trivial parallel tool dispatch
│   ├── PromptBuilderSubnet.java           # Content + tools to LlmRequest
│   ├── RouterSubnet.java                  # LlmResponse to Out.xor(tools, transfer, final)
│   ├── LlmAgentSubnet.java                # composes the above + reask budget
│   ├── PersistStateSubnet.java            # single legacy-session writer
│   ├── TransferRouterSubnet.java          # Out.xor over compile-time agent names
│   └── SubnetActions.java                 # binding-map validator
├── runner/
│   ├── PetriRunner.java                   # per-session NetExecutor handle
│   ├── PetriAgent.java                    # BaseAgent adapter for stock ADK Runner
│   ├── SessionExecutorRegistry.java       # lazy Map<SessionKey, PetriRunner>
│   └── SessionKey.java                    # (appName, userId, sessionId)
└── verify/AdkNetInvariants.java           # 3 structural + 3 SMT property factories
```

Voice-specific demo subnets (`BargeInSubnet`, `LiveApiRecoverySubnet`,
`LlmStreamingStepSubnet`) live under
`src/test/java/org/libpetri/adk/demos/voice/`. They are not part of
the shipped library. They exist as exemplars of how BIDI and
Live-API patterns compose on top of the seven stock subnets.

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

`LlmStreamingStepSubnet` plus `BargeInSubnet` plus
`LiveApiRecoverySubnet` are composed into one long-lived
per-session net with 5 env places.

- *Full BIDI scenario.* Three streamed chunks arrive via real
  env-place injection. A user barge-in fires mid-stream
  (voice-activity-gated route). A silence-triggered two-stage
  recovery follows (nudge then reconnect). The budget invariant
  holds: `CHUNK_BUDGET` returns to K at quiescence.
- *Inhibitor proof.* The model resumes mid-recovery-window. Both
  recovery transitions are blocked atomically.
- *Reset arc pattern.* Stale-state cleanup on new utterance.
- *Z3 deadlock-free proof.* Spacer on the composed BIDI net.
- *SCG bounded exploration.* `StateClassGraph.build(net, initial,
  256)` terminates within the bound. The reachable state space is
  finite.

See the root [`README.md`](../README.md) for the architectural
rationale, the design commitments, and the full subnet catalog.
