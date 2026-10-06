# docs/diagrams

Diagram generation for the net topologies embedded in the root README.

## Provenance rule

A diagram of a real net is exported from a net the tests build, never
drawn by hand. There are three sources, and each writes its own files
in `dot/`:

1. **Java nets**: `ReadmeDiagramsTest`
   (`java/src/test/java/org/libpetri/adk/docs/`) builds each net (the
   whole net, or a view of named transitions from it), exports it with
   libpetri's Java `DotExporter`, post-processes it and compares it with
   the committed file. A drift fails `mvn verify`.
2. **Python nets**: `python/tests/readme_diagrams/` compiles a sample
   workflow from `python/tests/workflow/samples.py` with
   `compile_workflow`, exports a view of it with libpetri-py's
   `dot_export`, post-processes it to the same conventions and compares
   it with the committed `workflow-*.dot`. A drift fails `pytest`.
3. **Sketches**: `src/index.ts` builds a small net with the `libpetri`
   npm package, applies the same conventions and writes
   `dot/sketch-*.dot`. Sketches are not nets this project runs and are
   not proved; the root README captions them *Illustrative*, and their
   place names stay in UPPER_SNAKE prose form.

All three sources post-process to the same conventions: each place's
name sits inside the place (an ellipse, not a circle with an `xlabel`,
which graphviz may park beside another node), mid-edge labels that
repeat what the style or the target already says are dropped (`read`,
XOR branch names), and seed markers (`●`, `●×K`), 12 pt edge labels, a
white background and `pad` are added. In a view, a place that a
transition outside the view also uses is drawn as a dotted grey cut
place. The Java test and the sketches also drop the mid-edge `reset`
label (`reset+out` stays), order each junction's branches
(`ordering="out"`) and collapse a transition's reset arcs into one
bundle note. The Java test can group a view into subnet clusters; inside
a cluster the `<Subnet>_` prefix is dropped from labels. The Python
helper (`_dot.py`) draws flat views without clusters and drops a reset
arc to a place no transition produces. Each Java and Python export
starts with a `// GENERATED` header naming its source; do not edit it,
or a sketch DOT, by hand.

Hand-drawn SVGs (the legend, the runner seam, ingress and
egress, the BIDI halves, the repository layout) live in `docs/assets/`,
not here.

## Regenerating

Three steps, from the repository root:

```bash
cd java && ./mvnw test -Dtest=ReadmeDiagramsTest -Dreadme.diagrams.write=true
cd ../python && READMEDIAGRAMS_WRITE=1 pytest tests/readme_diagrams
cd ../docs/diagrams && npm install && npm run build
```

The first two steps rewrite the Java and Python exports in `dot/` (the
Python step runs in the `python/` venv); skip the one whose port you
did not touch. `npm run build` runs `sketches`
(writes `dot/sketch-*.dot`), then `render`, which runs graphviz
`dot -Tsvg` over every `dot/*.dot` into `svg/`, then `hero`, which
composes `svg/hero.svg` from the two hero blueprints, their rendered nets and
the `adk-libpetri verify` output the Python step wrote to `hero/`. Both directories are
checked in, so a reader on GitHub sees the diagrams without running
anything. When a diagram is removed, delete its `.dot` and its `.svg`.

## Required tools

- The golden checks need only Java and Maven, or Python with the
  `python/` dev install; CI runs both without graphviz.
- Node.js 20 or later for the `tsx` runner, and graphviz `dot` on the
  path, are needed only to regenerate the SVGs locally. On Debian or
  Ubuntu install graphviz with `apt install graphviz`.

## What gets rendered

Section names are the root README's headings.

| File | Source | Embedded in (root README) |
|---|---|---|
| `hero` | `src/hero.ts`, composed from `hero/race*.yaml`, `svg/hero-race*.svg` and `hero/verify-*.txt` | the opening figure |
| `hero-race`, `hero-race-naive` | Python, `python/tests/readme_diagrams/hero/race.yaml` and `race_naive.yaml`, whole nets, inhibitor and read arcs left out of the ranking | inside `hero` (the fix, and the obvious guard) |
| `workflow-router` | Python, `compile_workflow(samples.router())`, view: `Wf_Start`, `Wf_classify_Run`, `Wf_handle_bug_Run`, `Wf_handle_other_Run`, `Wf_EndTurnOutput` | Quick start (Python) › Compile a Workflow (collapsed) |
| `workflow-back-edge-budget` | Python, `compile_workflow(samples.looping(), back_edge_budget={('counter', 'counter'): 3})`, view: `Wf_Start`, `Wf_counter_Run`, `Wf_Edge_counter_counter`, `Wf_Edge_counter_counter_Exhausted`, `Wf_finish_Run` | Quick start (Python) › Compile a Workflow |
| `llm-agent-turn-shell` | Java view of `LlmAgentSubnet.DEF`: StartTurn, BuildPrompt, EmitAnswer, EmitTransfer, AbortTurn, DropAbort | G1 One turn at a time, and no stranded turn |
| `reask-budget` | Java view of `LlmAgentSubnet.DEF`: BuildPrompt, ReAsk, ReAskExhaustedFallback, EmitAnswer | G2 Bounded autonomous loops (reask budget) |
| `transfer-router` | Java, `TransferRouterSubnet.def` with `billing` and `tech_support` | G3 Typed fallbacks: no dead letters |
| `speculative-race` | Java, `PatternA_SpeculativeRaceDemoTest.buildNet()` | G4 At most one commit per turn: race, optimistic commit, quorum |
| `quorum` | Java, `PatternB_QuorumDemoTest.buildNet()` | G4 At most one commit per turn: race, optimistic commit, quorum |
| `optimistic-commit` | Java, `PatternC_OptimisticCommitDemoTest.buildNet()` | G4 At most one commit per turn: race, optimistic commit, quorum (collapsed) |
| `escalation-ladder` | Java, `LiveApiRecoverySubnet.def(Config.defaults())` composed into a net, timing shown, one cluster | G5 Escalation ladders: timed recovery as places |
| `sketch-tiered-sla-ladder` | Sketch, `src/index.ts`, timing shown | G5 Escalation ladders: timed recovery as places (Illustrative) |
| `vad-bargein` | Java, `VadSubnet.DEF` composed with `BargeInSubnet.DEF`, one cluster per subnet | G6 Full duplex: VAD, barge-in, chunk drop, ordering (experimental) |
| `barge-in-chunk-drop` | Java, `VoiceSessionDemoTest.bargeInDropNet()` | G6 Full duplex: VAD, barge-in, chunk drop, ordering (experimental) |
| `sketch-stale-result` | Sketch, `src/index.ts` | N1 Not yet guaranteed: staleness across turns (Illustrative) |
| `sketch-fanout-monitor` | Sketch, `src/index.ts` | N2 Not yet guaranteed: variable-N fan-out (Illustrative) |
| `llm-agent-inner-loop` | Java view of `LlmAgentSubnet.DEF`: BuildPrompt, the four `LlmStep` transitions, `Router_Route`, `ToolDispatch_Dispatch`, ReAsk, ReAskExhaustedFallback; `LlmStep` and `ToolDispatch` clusters | How it works › The canonical composition: `LlmAgent` |

The legend at `docs/assets/diagram-legend.svg` documents the notation.
If the post-processing conventions change (in `ReadmeDiagramsTest`,
`python/tests/readme_diagrams/_dot.py` or `src/index.ts`), change all
three and the legend with them.
