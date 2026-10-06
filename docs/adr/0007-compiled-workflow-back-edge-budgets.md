# ADR 0007: Back-edge budgets in compiled workflows

- **Status:** Accepted
- **Date:** 2026-10-06
- **Scope:** Python `0.1.0`, `from_workflow` (`@experimental`). Amends design
  commitment 6.

## Context

Design commitment 6 reserves the budget-place pattern (a `Place<Void>` of
permits, a consuming transition, and a priority-and-inhibitor exhaustion
fallback) for one job: bounding the autonomous LLM-and-tool loop inside
`LlmAgentSubnet`. It is "not a generic loop bound", because a budget that
bounds the wrong thing is worse than none: it hides the design question of
*why* a loop runs.

ADK 2's `Workflow` allows cycles through routed edges: a node may route back
to an earlier node. ADK bounds nothing; a cycle runs as long as its routes
say. Compiling such a workflow into a net keeps the cycle, and Z3 can still
prove the safety properties (one turn at a time, serialized nodes, at most one
terminal output, deadlock freedom), but not that the turn terminates.

## Decision

`compile_workflow(..., back_edge_budget={(a, b): K})` routes the edge `a -> b`
through `wf/edge/a->b` and a budget place `wf/budget/a->b` seeded with `K`
permits at `Wf_Start`:

- `Wf_Edge_a_b` (priority 10) takes the edge token and one permit and
  triggers `b`;
- `Wf_Edge_a_b_Exhausted` (priority -10, inhibited by the budget) fails the
  turn with a typed `LoopBudgetExhausted` error event.

The turn's end resets the budget, so no allowance outlives its turn. Budgets
are opt-in per edge. An unbudgeted cycle compiles and is reported as
"unbudgeted cycle: termination is not provable".

Commitment 6 is amended to read: budget places bound autonomous loops
structurally: the LLM-and-tool loop in `LlmAgentSubnet`, and, in compiled
workflows, a back edge the caller explicitly budgets. Each budget names the
loop it bounds.

## Consequences

- The budget is the caller's statement about their workflow (a counter that
  should reach 3 gets a budget of, say, 5), not a default the library picks.
- Proofs state budgets in seeds, as elsewhere: libpetri models a K-permit
  seed as one token, so `place_bound(budget, 1)` is the claim that matters.
- A bookkeeping transition that consumes one place and produces another holds
  its token in no place while it fires. The verifier found that this let a
  turn end mid-retry. Every such transition (`Backoff`, budgeted edges, the
  resume match) therefore takes and returns a seeded `wf/quiet` token, which
  every turn end reads.
