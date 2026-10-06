# Runner (RUN)

- **RUN-001** Ingress is env-place injection only; injecting onto an
  undeclared place is an error; a drained runner refuses injects.
- **RUN-002** Egress is the hot `eventOut` stream; late subscribers miss
  earlier events.
- **RUN-003** A transition failure does not end the egress stream; it is
  published on a separate, non-terminating failure signal.
- **RUN-004** A net with `turnPermit` is seeded with one permit on a fresh
  start unless the initial marking names the place; a restore carries its own.
  A net with `turnAbort` has it declared as an env place.
- **RUN-005** An instantiated agent's prefixed permit must be seeded
  explicitly, or the start fails.
- **RUN-006** A checkpoint is the marking a drained run came to rest in,
  minus `eventOut` and the declared exclusions; any other ending has none.
- **RUN-007** Restore and initial marking are exclusive, except that a
  checkpoint found by `resume_from` supersedes the initial marking; a resumed
  run uses a fresh execution scope.
- **RUN-008** Action failures are logged, never silent.
