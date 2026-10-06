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
> BIDI/live and `from_workflow` are `@experimental` and may change in any
> release.

## Install

```bash
pip install adk-libpetri
```

Not published to PyPI yet: until the first tagged release (`python/v0.1.0`),
install from a clone with `pip install -e python`.

Python 3.11+. Pulls `google-adk~=2.11.0` and `libpetri`. Proofs need a `z3`
binary (4.8+) on `PATH` or named by `LIBPETRI_Z3`; the runtime does not.

## Two ways in

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

`PetriWorkflow` is a drop-in for `Runner(node=workflow)`. Each ADK node still
runs through ADK's own node runner, inside the invocation, so session events,
plugins and tracing are unchanged. What moves into the net is the
scheduling, and with it the claims Z3 can prove:

| Claim | Property |
|---|---|
| one turn at a time | `place_bound(wf/turnActive, 1)` |
| the permit never doubles | `place_bound(turnPermit, 1)` |
| at most one terminal output (ADK raises at runtime) | `place_bound(wf/terminalOutput, 1)` |
| every node runs serially | `place_bound(wf/<node>/idle, 1)` |
| no route goes unmatched (ADK logs a warning) | `unreachable(wf/<node>/unmatched)` |
| no turn gets stuck | `deadlock_free` (not claimed when the workflow has `interruptible` nodes) |

Translation, in short:

- **Routes** become XOR branches, with `DEFAULT_ROUTE` as the no-match branch.
- **`JoinNode`** becomes one place per predecessor.
- **`retry_config`** is unrolled into attempts with `delayed` backoff (jitter dropped).
- **`max_concurrency`** becomes a permit place.
- **`RequestInput`** parks the node until the next turn's function response
  (pass `interruptible=["node"]`).
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

What cannot be compiled faithfully is rejected: a `FunctionNode` that reads
session state, unless you pass `state="legacy_read"`. What is approximated is
listed in the report.

Four sample workflows, in five cases (linear, the router on two inputs, fan-out with a join,
concurrent) run natively and compiled with the same final output and the same
event authors (`tests/workflow/test_runtime_parity.py`). Retry, a budgeted
loop and `RequestInput` resume each have their own test there that compares
final output only.

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
