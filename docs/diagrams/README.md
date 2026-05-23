# docs/diagrams

Diagram generation for the topologies embedded in the root README. The
script builds each net via the `libpetri` npm package, exports it to DOT
via `libpetri/export`, and renders the DOT to SVG via graphviz `dot`.

## Regenerating

```bash
cd docs/diagrams
npm install
npm run build
```

The `build` script writes DOT files to `dot/` and SVGs to `svg/`.
Both directories are checked into the repository so a reader on
GitHub sees the diagrams without running anything.

## Topology source

The TS topologies in `src/index.ts` mirror the Java subnet sources
under `java/src/main/java/org/libpetri/adk/subnet/` and the voice demo
subnets under `java/src/test/java/org/libpetri/adk/demos/voice/`. They
are intended as readable diagrams, not as compile-equivalent ports.
When the Java topology changes in a way that affects the diagram,
edit `src/index.ts` and rerun `npm run build`.

## Required tools

- Node.js 20 or later for the `tsx` runner.
- graphviz `dot` on the path. On Debian or Ubuntu install with
  `apt install graphviz`.

## What gets rendered

| File | Embedded in | What it shows |
|---|---|---|
| `svg/llm-agent-subnet.svg`         | `README.md` (stock subnet catalog) | The canonical agent loop: PromptBuilder, LlmStep, Router with `Out.xor`, ToolDispatch, the reask-budget feedback loop, and the LegacySessionWrite sink. |
| `svg/stateful-monitor.svg`         | `README.md` (stateful-monitor bullet) | Batch-scoped fan-in via a `COLLECTOR` token plus the cross-cutting interactions that make it load-bearing. `SpawnJobs`, three `Worker` transitions, and `CollectResult` form the merge loop. `OrthogonalRead` reads `COLLECTOR` via a read arc so other subnets can snapshot the in-flight batch atomically. `OnNewUserTurn` consumes `USER_NEW_TURN` and resets `COLLECTOR`, `JOB_A/B/C`, and `SEARCH_RESULT` in one transition firing. |
| `svg/stale-result-validation.svg`  | `README.md` (stale-result bullet) | Two divergent commit sites (`ValidateToolResult` and `ValidateStreamChunk`) share the same `read(LATEST_GENERATION)` arc and XOR-route to a committed or discarded leaf. `BumpGeneration` consumes `USER_NEW_TURN` and applies `reset(LATEST_GENERATION)` plus an output, atomically invalidating every in-flight result. |
| `svg/speculative-race.svg`         | `README.md` (speculative-race bullet) | Both `SlowInflight` and `FastInflight` start from t=0. `TimerFires` produces `TIMER_EXPIRED` after 2s. `CommitSlow` and `CommitFastOnTimeout` race against `inhibitor(RESPONSE_SENT)`. `OnBargeInOrNewTurn` consumes `USER_INTERRUPT` and applies `reset` to every race place plus `RESPONSE_SENT`, atomically cancelling the in-flight race and re-arming the lock for the next round. |
| `svg/reask-budget.svg`             | `README.md` (bounding autonomous loops) | The reask-budget pattern isolated: priority plus inhibitor on a `Place<Void>`. |
| `svg/bidi-composition.svg`         | `README.md` (voice and full-duplex failure modes) | The composed BIDI net: streaming chunk emission with `CHUNK_BUDGET`, barge-in inhibitor and read pair, two-stage silence recovery. |
