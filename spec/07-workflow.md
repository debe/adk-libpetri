# Compiled workflows (WF) (Python)

`from_workflow` compiles an ADK 2 graph `Workflow` into a net whose node
transitions run the ADK nodes through ADK's own node runner.

- **WF-001** A compiled workflow and the native `Workflow` give the same
  final output and the same event authors for the same input.
- **WF-002** Each node's runs are serialized (a seeded idle token).
- **WF-003** Unrouted edges fire on every outcome; each route is one XOR
  branch; `DEFAULT_ROUTE` is the no-match branch; a node with routed edges and
  no match reaches a named `unmatched` place.
- **WF-004** A `JoinNode` consumes one output per predecessor.
- **WF-005** `retry_config` unrolls into attempts with deterministic backoff;
  exhaustion or a non-retryable error fails the turn with a typed error event.
- **WF-006** A `RequestInput` parks the node; the turn ends with the pending
  interrupt ids; the next turn's function response resumes the node.
- **WF-007** A budgeted back edge consumes one permit per traversal and fails
  the turn when none is left (ADR 0007).
- **WF-008** Proved for every compiled workflow: one turn at a time, the
  permit never doubles, at most one terminal output, every node serial;
  deadlock freedom for workflows without interrupts; unmatched routes are
  shown reachable or proved unreachable.
- **WF-009** What cannot be compiled faithfully is rejected (a node reading
  session state, unless `state="legacy_read"`; unknown names); what is
  approximated is listed in the translation report.
