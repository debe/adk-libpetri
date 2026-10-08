# ADR 0006: The Python port and ADK Python compatibility

- **Status:** Accepted
- **Date:** 2026-10-06
- **Scope:** Python `0.1.0`. google-adk `2.11.0` (pinned `~=2.11.0`), google-genai
  `>=2.19` (resolved transitively, `2.28.0` at the time of writing), libpetri-py
  `>=7.2,<8`, the first release with the Python event-store protocol, token
  capture, the stepped clock and `action_on_loop` (see below).

## Context

ADK's flagship is `google/adk-python`, about 21.7k GitHub stars against 1.7k
for adk-java. Its 2.0 release (GA 2026-05-19) replaced the
`SequentialAgent`/`ParallelAgent`/`LoopAgent` trio with a graph `Workflow`
(`google.adk.workflow`): nodes, routed edges, `JoinNode`, `RetryConfig`,
`RequestInput` interrupts. That graph is the orchestration core adk-libpetri
replaces in Python, and it adds a second integration the Java port does not
have: compiling an existing `Workflow` into a net (`from_workflow`).

libpetri's Python port is a PyO3 binding over the Rust runtime, not a fourth
implementation, so its semantics are Rust's. Several of its properties drive
the design and are recorded here with the spikes that established them.

## Spike findings (Phase 0)

**S1, actions have no running asyncio loop.** An async libpetri action runs on
a Tokio thread. Inside it:

| Call | Result |
|---|---|
| `asyncio.sleep(0.02)` | `RuntimeError: no running event loop` |
| `httpx.AsyncClient().get(...)` | `AsyncLibraryNotFoundError` (sniffio finds no loop) |
| genai through ADK's `Gemini.generate_content_async` | works by accident (about 850 ms; genai falls back to a thread) |
| any of the above through a hop onto a real loop | works; about 0.7 ms p50 per hop |

So every ADK coroutine a stock action awaits goes through `on_loop`, which
is `libpetri.action_on_loop` (or `run_coroutine_threadsafe` when the caller
names another loop, such as the invocation's). libpetri
declined to set the running loop on Tokio threads, rightly: asyncio would then
believe it was on the loop thread when it was not.

**S2, one captured loop per process.** Starting a second executor from a
different asyncio loop while one is in flight raises ("concurrent multi-loop
usage is not supported"). Long-lived session executors therefore all start on
one caller-owned `OrchestratorLoop` (a loop on its own daemon thread). ADK's
sync `Runner.run` creates a fresh loop per call, so a caller loop is never a
stable home for anything that outlives a turn.

**S4, running ADK nodes outside `Workflow`.** `Context._run_node_internal(node,
node_input=..., return_ctx=True, run_id=..., skip_run_id_validation=True)`,
called on the invocation's own loop, runs any node through ADK's `NodeRunner`.
The child context carries `output`, `route`, `interrupt_ids` and `error`; the
node's events go into the invocation's event queue with native paths
(`host@1/A@1`) and are appended to the session before the node continues. The
calling node must have `rerun_on_resume=True`. A compiled workflow's node
transitions therefore hop to the caller's loop and use exactly this, so every
ADK node keeps ADK's session, plugin and tracing behaviour.

**S5, PetriAgent as a pydantic `BaseAgent`.** Non-field state goes in
`PrivateAttr`s. A `BaseAgent` root still runs on ADK 2.11's legacy agent path
(`run_async(InvocationContext)`), which has no node `Context` and no event
queue. That is fine for `PetriAgent`, whose net emits its own events, but not
for a compiled workflow, which needs to run child nodes. So the compiled
workflow is `PetriWorkflow(BaseNode)`, the drop-in for `Runner(node=workflow)`.

**Concurrent firings (found by the ported `PersistState` test).** The Rust
executor may start an async transition again while an earlier firing of it is
still running. Java's executor never does (CONC-002). Two consequences:
`PersistState_Persist` serialises `append_event` with a per-loop lock, and
`LlmStreamingStep_EmitChunk` is a *sync* action, which completes inside its
firing, so chunks leave in arrival order.

**Python ADK forwards voice activity.** `GeminiLlmConnection.receive` maps
`voice_activity` onto `LlmResponse` (`gemini_llm_connection.py`), so the Java
VAD foil (stock live receive drops the VAD edges) is false in Python. The
`SyncGeminiLlm`/`VadTapGemini` exemplars exist in Java to work around ADK Java
wrappers; their Python counterparts are optional.

## Upstream libpetri-py changes (7.2)

Agreed with the libpetri maintainers' session and reviewed there:

1. **Python `EventStore` protocol**: `event_store=` takes any object with
   `append(event)`, optional `is_enabled()` and `captures_tokens`; delivery is
   ordered over an unbounded channel, batched per GIL acquisition, and drained
   before the run's awaitable resolves. A failing `append` is logged and
   exposed on `ExecutorHandle.event_store_error`, not fatal.
2. **Token capture on the store**: `InMemoryEventStore(capture_tokens=...)` and
   `NetEvent.token`, aliased (not copied), so the egress bridge treats
   `EVENT_OUT` tokens as read-only.
3. **Clocks as opaque Rust classes**: `ExecutorOptions(clock=SteppedClock())`,
   host-stepped with `advance_ms` and `settle(action)`, matching the Java
   test `ManualClock`. Action timeouts (`timeout(...)` outputs) are not
   virtualised under any clock (TIME-015 scope limit).
4. `libpetri.action_on_loop(coro)`.

Released as libpetri-py 7.2.0, which is the port's floor. Before that, an
interim egress shim (an appended `Runner_EgressPublish` transition) stood in
for item 1; it was removed when the floor moved to 7.2, and with it the
real-time stand-ins for the stepped-clock tests.

## The touched ADK surface

- `google.adk.agents`: `BaseAgent`, `InvocationContext`, `RunConfig`,
  `StreamingMode` (from `run_config`), `LiveRequestQueue`, `Context`
- `google.adk.events`: `Event`, `EventActions`, `RequestInput`
- `google.adk.models`: `BaseLlm`, `BaseLlmConnection`, `LlmRequest`, `LlmResponse`
- `google.adk.runners`: `Runner`, `InMemoryRunner`
- `google.adk.sessions`: `BaseSessionService`, `Session`, `InMemorySessionService`
- `google.adk.tools`: `BaseTool`, `ToolContext`, `FunctionTool`
- `google.adk.workflow`: `Workflow`, `BaseNode`, `FunctionNode`, `JoinNode`,
  `DEFAULT_ROUTE`, `RetryConfig`, `NodeTimeoutError`, `START`
  (`_base_node`), `Workflow.graph` (`nodes`, `edges`, `Edge.route`)
- **Private, pinned by the `~=2.11.0` range**: `Context._run_node_internal`,
  `Context.resume_inputs`, `Context.event_author`, `Context._interrupt_ids`
  (through events' `long_running_tool_ids`), `InvocationContext._abort_signal`,
  `BaseNode._requires_all_predecessors`, `FunctionNode._sig`.

## Behaviour claims to re-check on every bump

The compiler reproduces `Workflow` scheduling, so these are pinned by tests
(`tests/workflow/test_runtime_parity.py` runs each sample natively and
compiled) and must be re-read in `workflow/_workflow.py` and `_graph.py`:

- unrouted edges always fire; `DEFAULT_ROUTE` fires only when no specific
  route matched; an emitted list matches every edge sharing a value; no match
  ends the branch with a log warning;
- one node's runs are serialized (triggers queue FIFO while it runs);
- `JoinNode` waits for every predecessor's COMPLETED status, which is sticky;
- more than one terminal output raises at finalize;
- `RetryConfig` defaults (5 attempts, 1 s, x2, 60 s cap, jitter 1) and
  exception matching by class name over the MRO;
- `RequestInput` becomes an `adk_request_input` function call with
  `long_running_tool_ids`, resumed by a function response whose id is the
  interrupt id;
- a `BaseAgent` root runs on the legacy agent path, a `BaseNode` root on the
  node path (`runners.py`, `run_async`);
- `Runner.run` (sync) creates a loop per call;
- `GeminiLlmConnection.receive` forwards voice activity;
- ADK's transfer resolution falls back to `root_agent.find_agent`;
- the web UI seams `adk-libpetri web` uses (the duck-typed `graph` field, the
  builder assistant's name and tools, `get_fast_api_app(agent_loader=)`): see
  [ADR 0009](0009-web-ui-and-builder-assistant.md#re-check-on-an-adk-bump).

## Re-check procedure

1. `pip download google-adk==<old> google-adk==<new> --no-deps -d /tmp/adk`,
   unzip both and `diff -rq` the two `google/adk` trees.
2. Intersect the diff with the touched surface above; read every hit for
   behaviour, not only for signatures.
3. `pytest -k foil` (the ADK-only foils green-lock ADK's current behaviour; a
   red foil means ADK changed) and `pytest tests/workflow`.
4. Full `REQUIRE_Z3=1 pytest`.
5. Update this ADR's scope line and any claim that moved.

## Consequences

- One process-wide orchestrator loop is the caller's to create and close; the
  library ships no singleton (`OrchestratorLoop.shared()` is opt-in).
- Typed places are checked by name *and* type when composing `NetSpec`s,
  which libpetri-py does not do; token types at runtime are checked only with
  `ADK_LIBPETRI_CHECK_TOKENS=1`.
- Every stock subnet's structure is golden-checked against the Java net
  (`spec/fixtures/nets`), so the two ports cannot drift silently.
