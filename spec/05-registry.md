# Session registry (REG)

- **REG-001** Strong-owned (default): a runner lives until `close(key)` or
  `close_all()`.
- **REG-002** Owner-bound (Java `cleanerOwned`, Python `finalizer_owned`): the
  runner is torn down when its owner is collected; the registry never pins the
  owner.
- **REG-003** One owner per key, by identity; a different owner, or mixing
  owned and ownerless calls for one key, is an error.
- **REG-004** Concurrent first calls for a key converge on one runner; losers
  are drained.
- **REG-005** With a checkpoint store, teardown drains, then saves the final
  marking or removes a stale checkpoint; the key stays taken until then, and
  a replacement for it waits.
- **REG-006** `discard(key)` ends a session without saving and removes its
  checkpoint.
