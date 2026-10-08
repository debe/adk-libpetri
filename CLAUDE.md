# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when
working with the adk-libpetri repository.

## Project overview

adk-libpetri replaces Google ADK Java's orchestration core
(`SequentialAgent`, `ParallelAgent`, `LoopAgent`, `BaseLlmFlow`,
`AgentTransfer`, `Runner` driving an RxJava pipeline) with a
Coloured Time Petri Net runtime built on
[libpetri](https://github.com/debe/libpetri). Stock ADK `Runner`
consumes turn-based sessions through the `PetriAgent extends
BaseAgent` adapter; Live/BIDI paths bridge via `BidiPetriAgent`
over `LiveConnection`. There is no ADK source fork.

The repo follows libpetri's multi-language layout: `java/` and `python/`
(ADK Python 2.x, `adk_libpetri`), eventually `typescript/` and `rust/`. The
Python port mirrors the Java runtime and adds `from_workflow`, which compiles
an ADK 2 graph `Workflow` into a net. Shared contracts and the subnet
structure fixtures both ports golden-check live in `spec/`.

## Build and test commands

### Java (`java/`)

```bash
cd java
./mvnw verify                                   # Full build + tests
./mvnw test                                     # Tests only
./mvnw test -Dtest="MultiAgentDemoTest"        # Single class
./mvnw test -Dtest="*Streaming*"                # Wildcard
```

Java 25 (no preview features needed). Maven 3.9.x via wrapper. SMT
verification (libpetri 4.0+) runs an external `z3` binary (4.8+, on
`PATH` or via `LIBPETRI_Z3`). Tests requiring it use
`@EnabledIf("z3Available")` (delegating to `SmtVerifier.z3Available()`)
so the build does not fail without it.

### Python (`python/`)

```bash
cd python
python3.12 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
REQUIRE_Z3=1 pytest                      # full suite (Z3 gate on)
pytest tests/workflow                    # from_workflow: structure, proofs, parity
pytest -k foil                           # ADK-only foils (green-lock ADK behaviour)
ruff check . && ruff format --check . && pyright
```

Python 3.11+, google-adk `~=2.11.0`, libpetri-py `>=7.2,<8`. `REQUIRE_Z3=1`
makes `tests/test_z3_gate.py` fail without the `z3` binary. Never name a test
directory `docs/` (the TypeDoc rule in `.gitignore` would hide it).

### Cross-language fixtures (`spec/fixtures/nets/`)

```bash
cd java && ./mvnw test -Dtest=SpecFixturesTest -Dspec.fixtures.write=true
```

Java writes them; `SpecFixturesTest` and Python's `tests/conformance` both
golden-check them. Change a stock subnet in both ports, regenerate, commit.

### Diagrams (`docs/diagrams/`)

```bash
cd java && ./mvnw test -Dtest=ReadmeDiagramsTest -Dreadme.diagrams.write=true
cd ../python && READMEDIAGRAMS_WRITE=1 pytest tests/readme_diagrams
cd ../docs/diagrams && npm install && npm run build
```

Three steps. `ReadmeDiagramsTest` exports every Java-net diagram from
the nets the tests run (whole nets or named-transition views) into
`docs/diagrams/dot/`; without
`-Dreadme.diagrams.write=true` it is a golden check, and a drifted DOT
file fails `mvn verify`. `python/tests/readme_diagrams` does the same for
the Python-net diagrams (the compiled-workflow `workflow-*.dot` views and
the README hero's `hero-race.dot`, plus the hero's YAML copies and
`adk-libpetri verify` excerpts in `docs/diagrams/hero/`): with
`READMEDIAGRAMS_WRITE=1` it writes them, otherwise drift fails `pytest`.
`npm run build` then writes the illustrative `dot/sketch-*.dot` files from
`src/index.ts`, renders every DOT file to SVG, and composes the README's
opening figure `svg/hero.svg` (`src/hero.ts`) from those generated files.
Only that last step needs Node.js 20 or later and graphviz `dot`; CI needs
neither. Hand-drawn SVGs live in `docs/assets/`. Keep every diagram a
white card with no
`prefers-color-scheme` block: inside `<img>` the media query follows the
OS, not the GitHub theme.

### ADK version bumps

Follow the re-check procedure in
[ADR 0002](docs/adr/0002-adk-version-compat.md). Also re-check, with
`javap -c -p` on the new `google-adk` jar, the bytecode claims in
[java/README.md's "ADK Java 1.10.1 behaviour the argument relies on"](java/README.md#adk-java-1101-behaviour-the-argument-relies-on),
which no test pins: `ParallelAgent.runAsyncImpl` is
`Flowable.merge(...).takeUntil(escalate)`; `InvocationContext.endInvocation`
is a non-volatile field read only in `BaseAgent` and `BaseLlmFlow`; and
`Functions` uses `concatMapEager`/`concatMapMaybe` and never reads
`endInvocation`. Update the README's "ADK 1.10.1" mentions with them.

## Architecture

The architectural pitch (sequential-projection critique, runtime
model, stock subnet catalog, ADK Runner integration) and the
project's design commitments all live in the root
[`README.md`](README.md) — it is the single source of truth. Read it
before making structural changes. The diagrams it embeds are
generated from [`docs/diagrams/`](docs/diagrams/).

### Source layout (`java/src/main/java/org/libpetri/adk/`)

- **`colours/`**: `AdkColours`, the typed boundary catalog
  (`Place<Content>`, `Place<Event>`, `Place<LlmRequest>`, etc.). Has
  wrapper records for generic-typed places (`ToolCalls`,
  `ToolResults`, `LegacySessionWrite`, `TransferTarget`).
- **`bridge/`**: `EventStoreToFlowableBridge` (EventStore decorator
  to RxJava `Flowable<Event>`, requires an explicit downstream
  `EventStore`) and `OtelEventStore` (OT spans per transition fire).
  Executors are caller-supplied. The library carries no shared
  executor singleton.
- **`subnet/`**: 9 stock subnets plus `SubnetActions` (per-subnet
  `bind` validation, and `merge`/`bindComposed` for binding a composed
  net's maps in one checked call).
  `LlmStep`, `ToolDispatch`, `PromptBuilder`, `Router`, `LlmAgent`,
  `PersistState`, `TransferRouter`, plus the beta SSE-streaming pair
  `LlmStreamingStep` and `StreamingLlmAgent` (both `@Experimental`).
  Voice-specific demo subnets (`BargeIn`, `LiveApiRecovery`, `Vad`)
  are not part of the shipped library. They live under
  `src/test/java/org/libpetri/adk/demos/voice/` as exemplars of
  BIDI and Live-API composition. `Vad` turns speech-activity edges
  into the `VOICE_ACTIVITY_OPEN` window `BargeIn` reads; the edges come
  from `demos/VadTapGemini` (wraps ADK's live transport, preferred) or
  `demos/SyncGeminiLiveConnection` (reads genai's Live session directly).
  `LlmAgentSubnet` keeps the invocation's turns on an in-net
  `CONVERSATION` place so every re-ask carries the whole exchange.
- **`runner/`**: `PetriRunner` (libpetri-native handle; its builder
  takes libpetri's `restore`, `executionScope`, `executionEnvironment`
  and `deadlineTolerance` options through `ExecutorSpec`),
  `PetriAgent` (ADK `BaseAgent` adapter, built with
  `PetriAgent.builder(...)`), `SessionExecutorRegistry` (lazy
  per-session executor map), `SessionCheckpointStore`
  (`@Experimental` drain-then-save on teardown / `resumeFrom`
  checkpoints) and `SessionKey`.
- **`verify/`**: `AdkNetInvariants`. 3 structural validators plus 2
  SMT property factories (`budgetPlaceBounded`, `eventOutBounded`).
  Which property is proved on which net is listed in the README's
  Verification section; `StockSubnetProofsTest` proves `LlmStep`,
  `Router`, `ToolDispatch`, `TransferRouter` and `PersistState` each
  alone via `SubnetDef.verify` with `arrivals(k, k)`, and the composed
  `LlmAgent` and `StreamingLlmAgent` as whole nets. Budget
  bounds are stated in seeds (libpetri models an N-permit seed as one
  token).
- **Test support**: `ManualClock` (`src/test/.../adk/`) is a
  thread-safe virtual clock for libpetri's `ExecutionEnvironment`;
  `settle(action)` makes timed tests deterministic. Prefer it to
  sleeps for anything driven by `delayed`/`deadline` timings.

### Two demo programs (`java/src/test/java/org/libpetri/adk/demos/`)

- **`MultiAgentDemoTest`**. Planner LlmAgent plus TransferRouter
  composed, driven via stock `InMemoryRunner`, observability via
  `OtelEventStore`, structural invariants asserted before execution.
  Z3 proves the composed net is deadlock-free and that a turn emits at
  most one event.
- **`VoiceSessionDemoTest`**. Streaming plus barge-in plus silence
  recovery composed into one long-lived per-session net with
  multi-direction env places. Z3 proves it deadlock-free, with the env
  places modelled via `environmentMode(bounded(1))`. Without that the verifier returns
  `Unknown`, because a proof that ignores env places would be vacuous.
  Its silence-recovery timers run on `ManualClock`.
  SCG bounded exploration lives next door in `LiveApiRecoverySubnetTest`:
  the composed BIDI net's state-class graph completes from one
  `LLM_REQUEST` seed with `MODEL_QUIET` as a `bounded(1)` env place.
  `LiveApiRecoverySubnet` cancels on activity: an answer
  (`MODEL_ACTIVE`) consumes the pending rung, and the caller's injected
  `MODEL_QUIET` clears `MODEL_ACTIVE` in-net.

## Load-bearing design principles

These are the project's structural commitments (the README states
them as "Design commitments"). Violating any of them introduces the
bug classes the design is meant to eliminate.

1. **The interaction model with a running net is env-place
   injection only.** No method calls into transitions. No side
   channels. This applies to *every* external signal, not just
   `USER_IN`. Scroll events, sensor readings, webhooks, and the
   rest all enter via their own typed env place. See
   `ScrollAwareDemoTest`.
2. **The marking IS the state.** External stores (ADK
   `Session.state`, DB, cache) are write-only legacy bridges, never
   read from inside the net during execution. Read arcs on typed
   in-net places are fine (the in-net conversation-place pattern).
3. **Typed colours per domain concept.** Never collapse
   heterogeneous state into a single `Place<Map<String, Object>>`
   "bag." The only bag-shaped colour in the catalog is
   `LegacySessionWrite`, named loudly and used only for the ADK
   Session export bridge.
4. **No ADK source fork — and no dependency fork either. Zero
   forks.** Integration is via the `PetriAgent extends BaseAgent`
   adapter in `runner/PetriAgent.java`. The orchestration core
   (`SequentialAgent`/`LoopAgent`/`BaseLlmFlow`/`AgentTransfer`/`Runner`)
   is replaced, never forked. Where a defect lives in *ADK's wrapper
   over genai* — the `commonPool` hops in `Gemini.generateContent`,
   the VAD/barge-in signals dropped by `GeminiLlmConnection` — the
   wrapper is wrapped or bypassed in thin user code, never patched in a
   fork of genai or ADK. The `SyncGeminiLlm` exemplar calls genai
   directly for the LLM path. For voice the preferred route is the
   `VadTapGemini` exemplar, which keeps ADK's `GeminiLlmConnection` and
   wraps its live transport through ADK 1.9's `connectLiveTransport`
   seam; `SyncGeminiLiveConnection`, a direct `client.async.live` read,
   is the fallback for full control.
5. **Observability via `EventStore` decorator chain.**
   `OtelEventStore`, `EventStore.logging()` (libpetri-provided),
   and any future structured-logging or debug-recording stores
   wrap each other via the delegate pattern. Never reach for
   `ExecutionContextProvider` for observability. That is only for
   action-side ambient-context propagation.
   `PetriRunner.failureSignal()` is control flow, not observability: it
   exists so a caller can fail the turn that was in flight without
   killing the session's egress, and it emits `TransitionFailure` only.
   Route anything you want to *record* through the `EventStore` chain.
6. **Reask budgets bound autonomous LLM-and-tool loops
   structurally.** Use `Place<Void>` with a
   priority-and-inhibitor exhaustion-fallback transition (the
   reask-budget pattern). This is not a generic loop bound. It is
   only for autonomous-runaway protection inside `LlmAgentSubnet`, and
   (Python, ADR 0007) for a back edge of a compiled workflow that the
   caller budgets explicitly via `back_edge_budget`.
7. **Stock subnets are convenience templates, not the framework.**
   Users compose their own subnets directly via
   `PetriNet.builder().compose()` plus `SubnetDef.fromNet(...)`.
   The framework IS the composition primitives. Stock subnets are
   examples that happen to work for common cases.
8. **Per-session executor lifetime is caller-bound, with an
   explicit-close default.** `SessionExecutorRegistry.strongOwned()`
   is the documented default: the runner lives until the caller
   invokes `close(SessionKey)`/`closeAll()` from a session-end hook.
   `cleanerOwned()` is opt-in for callers that hold a stable strong
   owner whose GC tracks session end — when that owner is collected,
   `java.lang.ref.Cleaner` tears the runner down. Neither mode has an
   API path that registers a runner without a teardown route, so
   leaks (orphan orchestrator threads, hot processors, marking state)
   are structural non-options. `ctx.session()` is NOT a safe
   `cleanerOwned()` owner with `InMemorySessionService` (defensive
   copies); supply a stable identity (websocket session, holder map).

## Key conventions

- Java 25, `record`s for immutable types, `sealed` interfaces for
  discriminated unions.
- Subnet definitions are stateless `SubnetDef<Void>` instances.
  Per-session action closures bind via `SubnetActions.bindComposed(net,
  maps...)` for a composed net, or a stock subnet's `actionBindings`
  (validated by `SubnetActions.bind`). Never a bare
  `PetriNet.bindActions(Map)` with an unchecked map: it binds a silent
  `passthrough()` for every transition the map misses.
- All subnet transition names are `<NAME>_<Verb>` (for example
  `LlmAgent_BuildPrompt`) so `SubnetActions.bind` validates keys
  reliably.
- Boundary `Place<T>` constants live in `AdkColours`. A subnet's
  internal places are public constants on the owning subnet class:
  in a nested `Places` holder (`LlmStepSubnet.Places`,
  `LlmStreamingStepSubnet.Places`) or directly on the class
  (`LlmAgentSubnet.REASK_BUDGET`, `TransferRouterSubnet.UNKNOWN_TARGET`
  and its `targetPlace(name)` factory).
- Tests use `BitmapNetExecutor.builder(net, initial).run()` for
  synchronous test patterns. Long-lived BIDI tests use `runAsync`
  plus drain.

## Python port (`python/src/adk_libpetri/`)

Same packages as Java (`colours`, `bridge`, `subnet`, `runner`, `verify`),
plus `workflow/` (`compile_workflow`, `verify_workflow`, `PetriWorkflow`) and
`net/` (`PetriNet`, Petri-net blueprints written in ADK YAML, ADR 0008).
Read [ADR 0006](docs/adr/0006-python-port-and-adk-python-compat.md) before
structural changes. Python-specific rules:

- **Stock subnets are `NetSpec`s** (`_spec.py`): frozen `TransitionSpec`s
  over typed `Place`s under the Java names. `NetSpec.compose` is flat and
  fuses places by name and type; `NetSpec.build(actions)` rejects missing,
  unknown or doubly bound actions. Use `lp_actions(...)` only when binding
  through libpetri's own APIs.
- **Actions have no asyncio loop** (Tokio threads). Await every ADK coroutine
  through `on_loop(coro)` (or `on_loop(coro, loop=...)` for the invocation's
  loop); plain `asyncio.sleep(x > 0)` raises inside an action.
- **One `OrchestratorLoop` per process**: libpetri captures one loop for
  running executors. Runners always start on it, never on a request's loop.
- **libpetri-py may re-enter an async transition** while an earlier firing is
  in flight (Java never does). Ordering-sensitive actions are sync; shared
  side effects are serialised (see `PersistState`).
- **Blueprints (`net/`)**: `blueprint.py` parses the YAML into one flat
  `NetSpec` plus action plans and `prove:` claims (pure, no ADK loader);
  `node.py` is `PetriNet(BaseNode)`; `proofs.py` runs the claims. `nodes` is
  `list[EdgeItem]` on purpose: it is the only field type ADK's loader resolves
  `.agent.fn` and `x.yaml` refs for, relative to the YAML file. Do not retype
  it. Subnets mount by YAML ref, bound ports fused, the rest prefixed `inst/`.
  Tests and fixture blueprints are in `tests/net/`.
- **`web/`** (`adk-libpetri web`, ADR 0009) wraps ADK's dev server, never
  patches it or its JS. ADK's dev UI is the product: `PetriAgentLoader`
  (traced nets, the Petri builder assistant under
  `__adk_agent_builder_assistant`) plus routes inserted at
  `app.router.routes[0]` that answer the UI's own requests for a net and call
  ADK's handler for the rest: `graph_view` (graph panel DOT), `builder_guard`
  (canvas saves cannot overwrite a net), `canvas_view` (the canvas card),
  `proof_view` (counterexample SVGs). Load a builder draft
  (`<app>/tmp/<app>`) only through `web/staging.staged` (a copy under a
  package name of its own), never under its app's package name: that races
  ADK's loader for `sys.modules`. Make drafts with `web/drafts.make_draft`
  (it records the baseline `reconcile` needs so Save never puts back an
  older app file). Every event under a net carries the net's name as its
  author, so the drawing never titles or labels anything with it (ADK's
  highlighting would light that node on every event). ADK's dev UI is the
  only UI: do not add a page of our own. `net/report.py`, `net/graph.py`
  and `net/counterexample.py` are the data the CLI, the routes and the
  builder tools share; change output there, not in each caller. A UI change
  needs a real-browser check (ADR 0009's re-check list).
- **`_net_node.py`** (`NetNodeBase`) holds the turn and per-session runner
  logic both `PetriWorkflow` and `PetriNet` subclass; change it once.
- **A compiled workflow is a `BaseNode`** (`PetriWorkflow`), not a
  `BaseAgent`: ADK 2.11 runs a `BaseAgent` root on its legacy path, which has
  no node `Context` to run child nodes with.
- **Timed tests run on `lp.SteppedClock`** (Java's `ManualClock`):
  `.clock(c).deadline_tolerance(timedelta(0))` on the runner builder, then
  `asettle_after(action)` / `advance_ms`. One clock per run. Action timeouts
  (`timeout(...)` outputs) are not virtualised, so keep those real and small.
- Frozen dataclasses for colours, `Literal`/`Union` + `match` for sum types,
  keyword `Config` dataclasses; builders only for `PetriRunner`/`PetriAgent`.

## Multi-language readiness

A further port (TypeScript, Rust) mirrors this layout under a sibling
top-level subdir, calls into the matching libpetri port, and golden-checks
`spec/fixtures/nets`.

## Versioning and release

Each language has its own version, tagged with the language prefix
(`java/v0.4.0`, `python/v0.1.0`).

**Python**: PyPI `adk-libpetri`, published by `scripts/release-python.sh
[--dry-run] <version>` (stamps `pyproject.toml` and `__version__`, commits
`release: python <version>`, builds, `twine check`s, tests the installed wheel
from outside `python/`, uploads, tags `python/v<version>`, creates the GitHub
release from the `## Python <version> - YYYY-MM-DD` CHANGELOG section). Local
publishing only, as for Java. Bump google-adk with ADR 0006's re-check
procedure.

**Versioning is 0.x.** A minor may break API. The turn-based path is the
settled part; `@Experimental` surfaces (SSE, BIDI/live) may change in any
release. Do not describe anything as "stable for 1.x" anywhere in the
repo: that phrasing predates the first real release and was removed.

**No `-SNAPSHOT`.** Mirroring libpetri, `java/pom.xml` carries a bare
release version on `main` between releases. There is no post-release bump
step and no snapshot publishing.

**Maven Central**: `org.libpetri:adk-libpetri`, published by
`scripts/release-java.sh <version>`. The `release` profile in
`java/pom.xml` (source/javadoc JARs, GPG signing, central-publishing with
`autoPublish=true` and `waitUntil=published`) does the actual work; the
script only drives it. Publishing is local, from a developer machine.
There is deliberately no publish-on-tag workflow and no signing key in
GitHub secrets, which is also how libpetri does it.

Do **not** add `flatten-maven-plugin`. libpetri uses it, but
`flattenMode=ossrh` drops `dependencyManagement`, and this project's
protobuf floor is delivered through exactly that block on a non-direct
dependency (see the README's protobuf section). Flattening would silently
break it.

The release ritual, in order:

1. Commit the CHANGELOG dating: rename the unreleased heading to
   `## Java <version> - YYYY-MM-DD`, and bump the version in the README
   install blocks. The script reads the CHANGELOG and never writes it.
2. `scripts/release-java.sh --dry-run <version>` to rehearse
   (`mvn clean verify -Prelease`, which really signs), then
   `git reset HEAD~1` to drop the version commit it leaves behind.
3. `scripts/release-java.sh <version>`.

It stamps the version, commits `release: java <version>`, runs
`clean deploy -Prelease` blocking until Central reports published, tags
`java/v<version>`, pushes commit and tag, and creates the GitHub release
from the CHANGELOG section.

Prerequisites, all checked by the script's preflight: clean tree, on
`main` and not behind `origin/main`, a GPG secret key in the agent,
`<server id="central">` in `~/.m2/settings.xml`, `gh` authenticated, a
CHANGELOG section for the version, and a tag that does not yet exist.
