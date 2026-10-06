# Verification (VER)

Proofs use one `verify()` per property and accept only `proven`.

- **VER-001** Each of LlmStep, Router, ToolDispatch, TransferRouter turns
  exactly K inputs into exactly K outcomes, deadlock-free with the outputs as
  sinks (`arrivals(K, K)`); PersistState takes every write.
- **VER-002** LlmAgent: one turn in flight, one conversation, a reask budget
  that never stacks (in seeds), without assuming atomic firing.
- **VER-003** LlmAgent: every input becomes exactly one outcome and the agent
  rests holding only its permit; deadlock-free.
- **VER-004** LlmAgent recovers from a failure at any step of a turn.
- **VER-005** LlmAgent never mints a second permit, however aborts arrive.
- **VER-006** StreamingLlmAgent: one turn in flight, one permit, one
  conversation.
- **VER-007** Structural invariants: a single legacy session writer, an
  unknown-target consumer for every transfer demux, `endInvocation`
  inhibitors on declared advancing transitions.
