# Net blueprints (NET) (Python)

`agent_class: adk_libpetri.net.PetriNet` writes a net in ADK's YAML agent
config; ADK's loader builds it and the node serves it under the stock
`Runner` ([ADR 0008](../docs/adr/0008-petri-net-blueprints.md)).

- **NET-001** A blueprint loads through ADK's own loader (`from_config`, so
  `adk web` and `adk run`) and through `PetriNet.from_config`. `nodes:` is a
  `list[EdgeItem]` field, so ADK resolves its refs (`.agent.fn`, `x.yaml`,
  inline nodes) relative to the YAML file; transitions name nodes by node
  name.
- **NET-002** Every arc, output and timing form parses to the matching
  `NetSpec` element: `in` (`one`, `exactly`, `at_least`, `all`), `read`,
  `inhibit`, `reset`, `priority`; `out` as a place, `and`, `xor`,
  route-labelled `xor` and `timeout`; `timing` as `delayed`, `deadline`,
  `exact` and `window`.
- **NET-003** A place's type is an alias, a dotted name, or a leading-dot name
  relative to the YAML file's package; a place with no type is a unit place.
  Catalog places may be used undeclared with their catalog types; any other
  undeclared place is an error. `seed:` seeds the session's net, and a
  `turnPermit` place is seeded with one token.
- **NET-004** A blueprint normalises to one flat `NetSpec`: a YAML twin of a
  hand-written net has the same `spec.fingerprint()`.
- **NET-005** Actions: `move` (the default) forwards the one coloured value to
  the coloured outputs and signals the unit ones; `emit` turns it into an
  `Event`; `node:` runs the ADK node inside the turn's invocation, its route
  picking a labelled xor branch (`default` the fallback). An xor with more
  than one choosable branch and no node is rejected.
- **NET-006** A failing node takes its transition's `error` branch with a
  `NodeError` token; with no `error` branch the turn fails as a failing node
  fails a `Workflow`: ADK's error event is recorded and `run_async` raises the
  node's exception.
- **NET-007** The turn: the input is injected on `userIn`; the first
  non-partial `eventOut` token ends the turn, an `Event` yielded as the node's
  event and any other value becoming its output. The invocation stays open
  until the turn's in-flight node runs finish. A session's net serves one turn
  at a time; a node transition that fires with no turn open runs in the next
  turn's invocation. A `str` input becomes a user `Content` on a
  `Content`-typed `userIn`; any other mismatch with `userIn`'s type is a
  `NetRunError`. A node's output must be of each coloured place's type; on an
  `Event` place it becomes an `Event`. Interrupts are not supported.
- **NET-008** Every load error is a `BlueprintError` naming the YAML key path,
  the file and a fix; an unknown top-level key gets a did-you-mean hint. A
  bare `nodes:` entry, a key named `args` at any depth and an arc that tests
  `eventOut` are load errors; `adk-libpetri check` prints a pydantic error as
  one line per field.
- **NET-009** `subnets:` mounts a child blueprint (`net:`) or a stock subnet
  (`stock:` `llm_agent`, `llm_step`, `tool_dispatch`, `router`, configured
  from an ADK `LlmAgent` named in `from:`). Bound ports fuse with parent
  places of the same type; every other child place and transition is
  prefixed `inst/`, so a blueprint mounts twice; seeds, env places and
  actions carry over under the prefix. A child's `turnAbort` fuses with the
  parent's unless bound elsewhere. A mounted `llm_agent` is
  fingerprint-equal to `llm_agent.DEF` under its prefix. Session runners are
  keyed by node name and net digest; a node's own registry closes its
  runners when the node is collected.
- **NET-010** An unknown port, an unbound in-port, a type conflict across a
  binding and a ref cycle are load errors; a cycle error names its chain.
- **NET-011** `prove:` runs one `verify()` per claim (`deadlock_free`,
  `place_bound`, `unreachable`, `mutual_exclusion`) on the composed net, with
  shared options and per-claim options laid over them. `verify(k=N)` replaces
  the `arrivals` bounds and the number of turns. With no `environment` the
  inputs come turn by turn (the next after an answer and the turn's node
  runs): one turn for a safety claim, two for `deadlock_free`. Every `env:`
  place, and for a safety claim `turnAbort`, arrives; an explicit
  `environment` that leaves out an `env:` place is a load error, as is
  `assume_atomic_firing` on a net with nodes without `assume_atomic_nodes`.
  `on_load: true` fails the load on a claim not proven. A violated claim is
  reported as violated, not as an error.
- **NET-012** The CLI: `adk-libpetri check FILE` validates and builds a
  blueprint without Z3; `adk-libpetri verify FILE [--k N] [--recursive]`
  prints each claim's verdict, exits nonzero on a violated claim, and with
  `--recursive` also checks each mounted child's own `prove:`.
- **NET-013** `adk_libpetri/net/schema.json` is a JSON Schema for the format,
  and every sample blueprint validates against it. `AUTHORING.md` is
  packaged with the library.
- **NET-014** The YAML twins of Patterns A, B and C have their Python nets'
  fingerprints, the same runtime behaviour (winner, latency bound, discards)
  and the same proofs, stated in `prove:`.
