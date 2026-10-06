# Compiled workflows (WF) (Python)

`from_workflow` compiles an ADK 2 graph `Workflow` into a net whose node
transitions run the ADK nodes through ADK's own node runner.

- **WF-001** A compiled workflow and the native `Workflow` give the same
  final output, event authors and node paths for the same input, checked on
  ADK's own workflow samples (`tests/workflow/adk_samples`). The compiled
  node adds no events of its own: the terminal node runs with
  `use_as_output`, and a failing node fails the run as it fails a
  `Workflow`.
- **WF-002** Each node's runs are serialized (a seeded idle token). Run ids
  are per workflow run, and a resumed node keeps its interrupted run's id.
- **WF-003** Unrouted edges fire on every outcome; each route is one XOR
  branch; `DEFAULT_ROUTE` is the no-match branch; a node with routed edges and
  no match reaches a named `unmatched` place.
- **WF-004** A `JoinNode` consumes one output per predecessor.
- **WF-005** `retry_config` and `timeout` stay on the node: ADK's node runner
  retries and times out inside the node's transition.
- **WF-006** A `RequestInput` parks the node; the turn ends with the pending
  interrupt ids; the next turn's function response resumes the node. Nodes
  that interrupt by construction (`auth_config`, tools requiring
  confirmation) are compiled interruptible without being named.
- **WF-007** A budgeted back edge consumes one permit per traversal and fails
  the run with `LoopBudgetExhausted` when none is left (ADR 0007).
- **WF-008** Proved for every compiled workflow: one turn at a time, the
  permit never doubles, one output per terminal node, at most one terminal
  node with output, every node serial; deadlock freedom for workflows without
  interrupts. Route coverage (unmatched routes unreachable) is a lint.
- **WF-009** What cannot be compiled faithfully is rejected: a node reading
  session state (a parameter, `ctx.state` in its body, an instruction
  template), unless `state="legacy_read"`; a `mode='task'`/`'chat'` agent;
  unknown names. What is approximated is listed in the translation report.
