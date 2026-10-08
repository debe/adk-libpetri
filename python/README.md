# adk-libpetri for Python

A Coloured Time Petri Net orchestration core for
[Google ADK Python](https://github.com/google/adk-python) 2.x, built on
[libpetri](https://github.com/debe/libpetri). It replaces ADK's graph
`Workflow` (and the deprecated `SequentialAgent` / `ParallelAgent` /
`LoopAgent`) with a net that Z3 can prove properties of, and runs under the
stock ADK `Runner`.

The design, the commitments and the Java port live in the
[repository README](../README.md). This page covers what is Python-specific.

> **0.x and experimental.** The turn-based path mirrors the Java port; SSE,
> BIDI/live, `from_workflow` and `PetriNet` blueprints are `@experimental` and
> may change in any release.

## Install

```bash
pip install adk-libpetri
```

Not published to PyPI yet: until the first tagged release (`python/v0.1.0`),
install from a clone with `pip install -e python`.

Python 3.11+. Pulls `google-adk~=2.11.0` and `libpetri`. Proofs need a `z3`
binary (4.8+) on `PATH` or named by `LIBPETRI_Z3`; the runtime does not.

## Three ways in

### 1. Compile an existing ADK `Workflow` (`from_workflow`)

```python
from google.adk.runners import InMemoryRunner
from google.adk.workflow import DEFAULT_ROUTE, FunctionNode, Workflow

from adk_libpetri import OrchestratorLoop
from adk_libpetri.workflow import PetriWorkflow, compile_workflow, verify_workflow

classify = FunctionNode(func=classify_fn, name="classify")
workflow = Workflow(
    name="support",
    edges=[
        ("START", classify),
        (classify, {"bug": bug_handler, DEFAULT_ROUTE: general_handler}),
    ],
)

compiled = compile_workflow(workflow)
print(compiled.report)  # what was kept, approximated, rejected
for proof in verify_workflow(compiled, k=2):  # Z3, one verify() per claim
    print(proof.result.verdict, proof.label)

loop = OrchestratorLoop()  # one per process; close() at shutdown
runner = InMemoryRunner(
    node=PetriWorkflow.from_compiled(compiled, orchestrator=loop), app_name="support"
)
```

Or declare it in ADK's own YAML agent config, so `adk web` and `adk run`
serve the compiled net:

```yaml
# root_agent.yaml
agent_class: adk_libpetri.workflow.PetriWorkflow
name: root_agent
state: legacy_read                       # compile options, as for compile_workflow
back_edge_budget: [[route_headline, generate_headline, 3]]
edges:                                   # exactly a Workflow's edges
  - [START, .agent.process_input, generate_headline.yaml, evaluate_headline.yaml, .agent.route_headline]
  - [.agent.route_headline, {unrelated: generate_headline.yaml}]
```

ADK's loader resolves the edges (code references, nested YAML files, route
maps) as for `agent_class: Workflow`, and the node compiles itself on
`OrchestratorLoop.shared()`. `PetriWorkflow.from_config(path, ...)` loads
either kind of YAML: an `agent_class: Workflow` file is compiled with the
options you pass.

`PetriWorkflow` is a drop-in for `Runner(node=workflow)`, and nests where a
`Workflow` does (inside another workflow, or as an agent's tool: it carries
the workflow's `input_schema` and `output_schema`). Each ADK node still runs
through ADK's own node runner, inside the invocation (with its own
`timeout`), so session events, node paths, plugins and tracing are
unchanged. The compiled node adds no events of its own: the
terminal node's event is the workflow's output event, and a failing node
fails the run as it fails a `Workflow` (`Runner.run_async` raises). What
moves into the net is the scheduling, and with it the claims Z3 can prove:

| Claim | Property |
|---|---|
| one turn at a time | `place_bound(wf/turnActive, 1)` |
| the permit never doubles | `place_bound(turnPermit, 1)` |
| each terminal node keeps one output (its last, as ADK) | `place_bound(wf/<node>/terminalOutput, 1)` |
| at most one terminal node outputs (ADK raises at runtime) | `unreachable(wf/terminalConflict)` |
| every node runs serially | `place_bound(wf/<node>/idle, 1)` |
| no turn gets stuck | `deadlock_free` (not claimed when the workflow has `interruptible` nodes) |
| route coverage, a lint (ADK logs a warning and ends the branch) | `unreachable(wf/<node>/unmatched)` |

Each proof has a `kind`: `"safety"`, `"deadlock"` or `"route coverage"`.
Route coverage is a lint, not a safety claim: ADK's own loop samples exit
through a route with no edge, so a violation there is expected.

Translation, in short:

- **Routes** become XOR branches, with `DEFAULT_ROUTE` as the no-match branch.
- **`JoinNode`** becomes one place per predecessor.
- **Terminal nodes** run with `use_as_output`; each keeps its last output on
  its own place, and two with output fail the run as ADK does.
- **`retry_config`** becomes a retry loop in the net: a failed attempt `i`
  lands on `wf/<node>/retry<i>`, a `delayed` backoff transition with ADK's
  delay for attempt `i` moves it to `wf/<node>/again`, and `Wf_<node>_Retry`
  runs the next attempt as the same ADK run (same node path, the next
  `ctx.attempt_count`). Whether to retry is ADK's own `_should_retry_node`.
  The node stays busy through the backoff, as under ADK's retry loop. Jitter
  is dropped: a firing window would be the TPN reading, but libpetri
  force-disables a window transition that misses its latest bound. The
  proofs run on a net where each retry loop is folded into its run
  (`CompiledWorkflow.verification_spec`): a retry token stands for the run
  still in flight, and the loop never strands a token, so the verdicts hold
  on the executed net (`test_folding_the_retry_loop_keeps_the_verdicts`
  checks one against the other).
- **`timeout`** stays on the node: ADK's node runner enforces it per attempt.
- **`max_concurrency`** becomes a permit place.
- **`RequestInput`** parks the node until the next turn's function response
  (pass `interruptible=["node"]`); the resumed run keeps its run id. Nodes
  that interrupt by construction (`auth_config`, a tool with
  `require_confirmation`) are compiled interruptible without being named.
- **Run ids** restart at `@1` with each workflow run, as ADK allocates them.
- **Conditional cycles** are kept. An unbudgeted cycle still gets deadlock
  freedom and safety, but not termination, and the report lists it as
  approximated. `back_edge_budget={("a", "b"): K}` bounds one, which makes
  termination provable
  ([ADR 0007](../docs/adr/0007-compiled-workflow-back-edge-budgets.md)).

`deadlock_free` means no reachable marking is stuck short of the turn's end;
it is not a termination proof for unbudgeted cycles. With `interruptible`
nodes the resume transition carries a ν-match guard on the interrupt id, and
interrupt ids arrive from the environment as unbounded ν-names. `verify_workflow` then proves the safety claims on
the net with the match guards dropped, an over-approximation whose bounds hold
on the real net (the proof label ends in `[on the match-free
over-approximation]`), and does not claim deadlock freedom.

What cannot be compiled faithfully is rejected:

- a node that reads session state, unless you pass `state="legacy_read"`: a
  `FunctionNode` parameter bound from state, a `ctx.state` read in its body,
  or an `{key}` placeholder in an agent's instruction (looked for inside
  parallel workers and nested workflows too);
- a `mode='task'` or `mode='chat'` agent node. Under `Workflow` it waits for
  the user across turns inside one workflow run, which the net does not
  model; run it as a `PetriAgent`, or use `mode='single_turn'`.

What is approximated is listed in the report: fan-out completion order
(siblings run concurrently and finish in scheduling order), branch scoping,
event replay, a node that takes `ctx` (children it runs through
`ctx.run_node` are invisible to the proofs), and `wait_for_output` across
turns.

`tests/workflow/adk_samples` runs ADK's own workflow samples (google/adk-python
v2.11.0, `contributing/samples`) natively and compiled, with scripted models,
and compares outputs, authors, node paths, texts, state and raised
exceptions. Of the 24 runnable samples, 22 match (10 need `state="legacy_read"`)
and 2 are rejected for their `mode='task'` agents; the Antigravity sample needs
an external SDK and is not vendored. Open gap: in a resumable app the compiled
node emits none of `Workflow`'s `agent_state` checkpoints (a strict xfail in
`node_as_tool`). `tests/workflow/test_runtime_parity.py` adds small
hand-written cases (retry, a budgeted loop, `RequestInput` resume).

### 2. Write the net yourself (`PetriAgent`)

```python
from google.adk.runners import InMemoryRunner

from adk_libpetri import OrchestratorLoop
from adk_libpetri import colours as C
from adk_libpetri.runner import PetriAgent, PetriRunner, SessionExecutorRegistry
from adk_libpetri.subnet import llm_agent

loop = OrchestratorLoop()
config = llm_agent.Config(name="assistant", model="gemini-2.5-flash", tools={"get_weather": tool})


def start(key):  # one runner per session
    return (
        PetriRunner.builder(llm_agent.DEF, llm_agent.action_bindings(llm, config))
        .environment_place(C.USER_IN)
        .orchestrator(loop)
        .astart()
    )


registry = SessionExecutorRegistry.strong_owned()
agent = PetriAgent.builder("assistant", registry, start).build()
runner = InMemoryRunner(agent=agent, app_name="app")
# from your session-end hook: await registry.aclose(key)
```

Stock subnets are `NetSpec`s: frozen, stateless definitions under the same
transition and place names as Java (`LlmAgent_BuildPrompt`, `userIn`, ...).
Compose your own with `NetSpec.compose(...)`. `NetSpec.build(actions)`
rejects a missing, unknown or doubly bound action, and every stock subnet's
structure is golden-checked against the Java net (`spec/fixtures/nets`).

### 3. Write the net in YAML (`PetriNet`)

`agent_class: adk_libpetri.net.PetriNet` writes the net itself in ADK's YAML
agent config, for what a `Workflow` cannot say (races, quorums, permits,
inhibitors, timed transitions). ADK's loader builds it, so `adk web` and
`adk run` serve it; `PetriNet.from_config(path, orchestrator=loop)` loads one
in Python. The design is
[ADR 0008](../docs/adr/0008-petri-net-blueprints.md) and the requirements are
[spec/08-blueprints.md](../spec/08-blueprints.md).

```yaml
# root_agent.yaml
agent_class: adk_libpetri.net.PetriNet
name: triage
nodes:                               # ADK node refs, resolved by ADK's loader
  - [.agent.classify]
  - [.agent.answer]
places:
  question: {type: str}              # userIn and eventOut need no declaration
  answered: {type: str}
  failed: {type: NodeError}
transitions:
  Triage_Read:     {in: [userIn], out: question, node: classify}
  Triage_Answer:   {in: [question], out: {xor: {default: answered, error: failed}}, node: answer}
  Triage_Emit:     {in: [answered], out: eventOut, action: emit}
  Triage_Fallback: {in: [failed], out: eventOut, action: emit}
prove:
  claims: [deadlock_free, {place_bound: {place: eventOut, bound: 1}}]
```

The format, in short:

- **`nodes:`** lists the ADK nodes the net runs, each entry a list
  (`- [.agent.fn]`, `- [child.yaml]`, or an inline `{agent_class: ...}`).
  The field is typed `list[EdgeItem]` on purpose: it is the only field ADK's
  loader resolves code and file references for, relative to the YAML file.
  Transitions name nodes by node name.
- **`places:`** `name: {type, seed}`. No type is a unit place. A type is an
  alias (`str`, `int`, `Content`, `Event`, `NodeError`, ...), a dotted name, or
  `.module.Name` relative to the YAML file's package (a `PetriNet` built in
  Python has no file and needs dotted names). The catalog places (`userIn`,
  `eventOut`, `turnPermit`, `turnAbort`, ...) need no declaration.
- **`transitions:`** `in` (`p`, `{place, count}`, `{place, at_least}`,
  `{place, all: true}`), `out` (`p`, `{and: [...]}`, `{xor: [...]}`,
  `{xor: {label: out, ...}}` with `default` and `error`, `{timeout: ms, child:
  out}`), `read`, `inhibit`, `reset`, `priority`, `timing` (`{delayed: ms}`,
  `{deadline: ms}`, `{exact: ms}`, `{window: [a, b]}`), and at most one action:
  - none, or `action: move`: forward the one coloured value to the coloured
    outputs, signal the unit ones;
  - `action: emit`: the same, with the value turned into an `Event`;
  - `node: name`: run the ADK node inside the invocation. Its output goes on
    the branch its route picks (`default` when none matches); a failure takes
    the `error` branch as a `NodeError`, or, with no `error` branch, fails
    the turn as it fails a `Workflow`. An xor with no node is rejected.
- **The turn** is `PetriAgent`'s: the input lands on `userIn`, the first
  non-partial `eventOut` token ends the turn (an `Event` is yielded, any other
  value becomes the node's output), and `env:` adds environment places, filled
  with `PetriNet.inject(session, place, value)` during a turn. The invocation
  stays open until the turn's node runs finish, so a race's loser runs to the
  end inside it. A session's net serves one turn at a time, and a node
  transition that fires between turns runs in the next turn's invocation. A
  `PetriNet` node cannot interrupt (`RequestInput`).
- **`ports:` and `subnets:`** compose blueprints. A child is a node ref
  (`- [race.yaml]`) mounted as `first: {net: speculative_race, bind: {userIn:
  question, eventOut: answer}}`: bound ports fuse with the parent's places,
  everything else is prefixed `first/`. `stock: llm_agent`, `llm_step`,
  `tool_dispatch` or `router` mounts a stock subnet configured from an ADK
  `LlmAgent` named in `from:`. A mounted child's `turnAbort` fuses with the
  net's own, which the runner signals on a failure.
- **`prove:`** `{options, claims, on_load}`: `deadlock_free`, `place_bound`,
  `unreachable` and `mutual_exclusion`, one `verify()` each, on the composed
  net. `on_load: true` fails the load when a claim is not proven.

Every load error names the file, the YAML key path and a fix:

```text
root_agent.yaml: transitions.Triage_Answer.out.xor.default: unknown place 'answerd'. Fix: did you mean 'answered'?
```

The JSON Schema is `adk_libpetri/net/schema.json`, and
`adk_libpetri/net/AUTHORING.md` is a short guide with the motifs (permit race,
quorum, inhibitor fallback, budget) as YAML.

```bash
adk-libpetri check root_agent.yaml                     # parse and build, no Z3
adk-libpetri verify root_agent.yaml --k 2 --recursive  # run prove:, children's too
```

`verify` prints each claim's verdict and exits nonzero on a violated or
unknown claim;
`--json` prints the verdicts as data, a violated claim's counterexample
included.
`PetriNet.verify(k=2)` returns the same verdicts in Python. The proofs are on the net's
structure, untimed, with every xor a free choice, so a claim holds whatever
the nodes return. By default the user's inputs come turn by turn, the next
after the previous answer: a safety claim covers one turn, `deadlock_free`
two, and `--k` sets the number for both. Safety claims also let `turnAbort`
and every `env:` place arrive; `deadlock_free` covers runs where nothing
fails. Running a `{timeout: ...}` output is broken in libpetri-py
for now (ADR 0008).

### In ADK's web UI

```bash
adk-libpetri web agents/        # ADK dev UI: http://127.0.0.1:8000/dev-ui/
```

This is ADK's own dev UI, used as you use it today; nothing in it is
patched. The server answers the UI's own requests with Petri-aware data:

- **The graph panel** (Info, and the fullscreen "Agent Structure") draws
  the net as a Petri net in the UI's light or dark theme: places with their
  seed tokens, transitions with what they run, inhibitor and read arcs. Mounted
  blueprints are clusters and a stock subnet is one node; in a net of more
  than 15 places each mounted blueprint is one node too, and clicking it in
  "Agent Structure" opens it (a stock subnet opens as a compact drawing).
  Run a turn and each `node:` transition lights up as its node answers,
  with the path back to `userIn`; select the net's answer and the
  transition that answered lights with the branch that won (the answer is
  emitted under that transition, `race@1/Race_Commit@1`, and shows as a
  message, not as JSON); a failed turn lights the transition or subnet that
  failed. A place nothing in the net produces is dotted. A function node in a mounted blueprint runs as
  `<mount>·<node>` (`second·fast`), so each mount lights for its own runs.
- **The builder assistant** (the pencil button) is ADK's own, with tools to
  read `AUTHORING.md` and the schema, to write blueprints (nothing is
  written unless every blueprint loads), and to check and verify them. It
  fixes a net from the counterexample until every claim is proven, and its
  replies end with the net's size and each claim's verdict. When the builder
  opens on a net it proves the claims and greets you with the net's size and
  verdicts, and `verify` or `check` answers any time, all without a model
  call; with no `GOOGLE_API_KEY` it says where to put one. `verify` lists
  each claim, the step where a violated one breaks and the steps before it,
  and its counterexample as a picture of what each step changed; click it
  for full size, with the net drawn at the bad step. The net drawing needs
  Graphviz `dot` on `PATH` (or `ADK_LIBPETRI_DOT`); without it the full
  picture has the steps only.
- **The builder canvas** shows the net's root (a `PetriNet` or a
  `PetriWorkflow`) with the functions and agents it runs, updated after each
  reply. It is read-only for a net: the YAML it saves before each message is
  ignored for a net (even one with a syntax error), what was added there is
  dropped and the assistant says so (Save with a sub-agent added there
  writes nothing and keeps the builder open), and Save ships the
  assistant's latest net to the graph panel and to chat. (In ADK alone, Save on a net app fails
  with 400.) A file you edit in an editor while the builder is open survives
  Save; one changed both there and by the assistant stops it, and the
  assistant tells you which.

Stock `adk web` serves the same blueprints; its graph view draws the net's
places and transitions as plain boxes, each `node:` transition beside the
ADK node it runs. To get the assistant there, serve it as an app:
`root_agent = create_petri_builder_assistant()` from
`adk_libpetri.web.builder`.

For hand-editing and debugging, the server also has `/petri`, an unlisted
power tool: a YAML editor beside the drawn net, a counterexample you step
across the drawing, and a replay of any session's markings, which ADK's
graph panel cannot show. It loads CodeMirror and viz.js from CDNs;
`build_app(petri_page=False)` leaves it out. See
[ADR 0009](../docs/adr/0009-web-ui-and-builder-assistant.md).

## What is different from Java

- **Actions run on libpetri's Tokio threads, with no asyncio loop.** Await ADK
  coroutines through `adk_libpetri.on_loop(coro)`; the stock subnets do.
- **One orchestrator loop per process.** libpetri captures one asyncio loop for
  running executors, so every session runner starts on a caller-owned
  `OrchestratorLoop`, never on whatever loop a request arrived on.
- **Owner-bound lifetime is `finalizer_owned()`** (`weakref.finalize`), the
  counterpart of Java's `cleanerOwned()`. `strong_owned()` stays the default.
- **ADK Python keeps the voice-activity edges** that ADK Java 1.10.1 drops, so
  there is no `VadTapGemini` counterpart. The `GenaiLiveConnection` exemplar
  stays as the full-control route (see below).
- **libpetri-py may re-enter an async transition** while an earlier firing is
  in flight. Java's executor never does. Ordering-sensitive actions are
  therefore sync (`EmitChunk`), and `PersistState` serialises its appends.

See [ADR 0006](../docs/adr/0006-python-port-and-adk-python-compat.md) for
the spikes behind these points, and for the procedure for bumping ADK.

## ADK Python 2.11 behaviour the foils lock in

The ADK-only foils (`pytest -k foil`) run stock google-adk 2.11 with no net
and assert what it does today. If an ADK release changes one of these, its
foil goes red and the claim here gets updated. Where the Java foil does not
carry over, the Python foil says so and asserts the Python behaviour.

**Unknown transfer target**
(`tests/demos/test_transfer_unknown_target_adk_foil.py`).
`find_agent` / `find_sub_agent` return `None` for an unknown name, and the
`transfer_to_agent` tool records any string unvalidated (the `enum` that
`TransferToAgentTool` adds to the declaration is a hint to the model, not a
check). At run time the dynamic node scheduler raises a bare
`ValueError("Transfer target agent '<name>' not found.")`. It escapes
`Runner.run_async` and ends the invocation: no error `Event` reaches the
caller, and the session keeps the transfer the model asked for. It is loud,
not a silent no-op, but untyped and fatal. The net's `TransferRouter` turns
the same input into a typed error `Event` and ends the turn normally.

**Fan-out and join in `Workflow`**
(`tests/demos/patterns/test_pattern_{a,b,c}_adk_only_foil.py`).

- `JoinNode` waits for every predecessor to complete. There is no first-of or
  K-of-N barrier, and no field sets a cardinality.
- A plain successor fires once per completed predecessor, so a commit node fed
  by N branches commits N times, and the workflow's output is the last commit.
- Losers are not cancelled. `Workflow._run_loop` returns only when every
  pending task is done; the only cancelling paths are a node error and the
  invocation-wide abort, and both discard the winner's result too.
- Nothing under `google/adk/workflow/` reads `event.actions.escalate`.
- The deprecated `ParallelAgent` is first-wins, but only when a branch
  escalates: that ends the merge and cancels the other branches. No proof says
  the commit happens at most once.
- Conditional fallback works through routes (`Event(route=...)` with
  `DEFAULT_ROUTE`), but sequentially: the slow path starts only after the
  cheap one fails. `RetryConfig` reruns the same node and has no fallback
  target.

**Voice-activity edges**
(`tests/demos/test_voice_vad_edge_adk_foil.py`). The verdict is the reverse of
Java's. ADK Java 1.10.1's `GeminiLlmConnection` drops the VAD speech-activity
edges; ADK Python 2.11's `GeminiLlmConnection.receive` yields
`LlmResponse(voice_activity=...)` for a VAD frame, and the live flow copies it
onto `Event.voice_activity` for the `Runner.run_live` caller. The foil runs
ADK's real `GeminiLlmConnection` over a fake genai session. Python therefore
has no `VadTapGemini`. `GenaiLiveConnection`
(`tests/demos/voice/genai_live_connection.py`) is the exemplar for raw frames
and one stream across turns, not a workaround.

## Development

```bash
cd python
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check . && ruff format --check . && pyright
REQUIRE_Z3=1 pytest
```
