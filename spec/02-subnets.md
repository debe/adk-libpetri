# Stock subnets (SUB)

Each stock subnet's structure is fixed by `fixtures/nets/<name>.json`.

- **SUB-001** `LlmStep`: BeforeModel (continue or short-circuit), LlmCall
  (success or typed `LlmError`), AfterModel, OnModelError (fails the
  transition without a recovery callback).
- **SUB-002** `Router`: transfer beats tool calls beats answer; exactly one
  branch per response.
- **SUB-003** `ToolDispatch`: one batch in, one results batch out; a per-call
  failure or unknown tool becomes that call's `{error, exceptionType}`
  response; an empty batch fails the transition.
- **SUB-004** `TransferRouter`: a known name lands on its own place, anything
  else on `_unknown`, which emits a typed error event.
- **SUB-005** `PersistState`: the single consumer of `legacySessionWrite`;
  each `append_event` is bounded by an action timeout (default 5 s), and
  appends are serialised.
- **SUB-006** `LlmAgent`: one turn at a time under `turnPermit`; every turn
  ends through EmitAnswer, EmitTransfer or AbortTurn, each returning the
  permit; DropAbort (priority 30) drops an abort with no turn in flight.
- **SUB-007** `LlmAgent` reask budget: BuildPrompt seeds N; ReAsk (priority
  10) consumes one; the inhibitor-guarded fallback (priority -10) answers when
  none is left; the turn's end resets the budget.
- **SUB-008** `LlmAgent` conversation: every re-ask carries the whole
  invocation, the model's call turn verbatim before its response turn.
- **SUB-009** `LlmStreamingStep`: each chunk is injected as it arrives, then
  one merged terminal response; EmitChunk (priority 20) emits partials in
  arrival order.
- **SUB-010** `StreamingLlmAgent`: the `LlmAgent` shell over `LlmStreamingStep`.
