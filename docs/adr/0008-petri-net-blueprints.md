# ADR 0008: Petri-net blueprints in ADK's YAML

- **Status:** Accepted
- **Date:** 2026-10-06
- **Scope:** Python `0.1.0`, `adk_libpetri.net` (`@experimental`).
  google-adk `~=2.11.0`, libpetri-py `>=7.2,<8`.

## Context

`from_workflow` compiles an ADK 2 graph `Workflow` into a net, but a
`Workflow` cannot say what a net says: a race with one winner, a quorum, a
permit, an inhibitor fallback, a timed transition. Until now the only way to
write such a net was Python (`NetSpec`, `PetriAgent`).

People and agents both write ADK's YAML agent config today; ADK ships an
Agent Builder Assistant that writes `root_agent.yaml`. If a net can be
written in that format, an agent can propose a net, have Z3 check it, and
read a counterexample or a key-path error to fix it, before anything runs.

Two facts about ADK 2.11's loader (`google.adk.agents.config_agent_utils`)
shape the design:

- `from_config` accepts any fully qualified `BaseNode` subclass as
  `agent_class` and maps the YAML keys onto its pydantic fields. `adk web`
  and `adk run` load `root_agent.yaml` through it. (The web UI's *upload*
  builder rejects an `agent_class` outside the app package; loading from disk
  works.)
- Only a field annotated `list[EdgeItem]` gets path-aware node resolution:
  `.agent.fn` (leading dot = the YAML file's package), `other.yaml` relative
  to the file, and inline `{agent_class: ...}` nodes. A plain `BaseNode`
  field gets no YAML path. `PetriWorkflow.edges` already relies on this.

## Decision

### The format

`agent_class: adk_libpetri.net.PetriNet` is a `BaseNode` whose YAML holds a
net:

```yaml
agent_class: adk_libpetri.net.PetriNet
name: speculative_race
nodes:                                # ADK node refs, resolved by ADK's loader
  - [.agent.fast]
  - [.agent.slow]
places:
  triggerA: {}                        # no type: a unit (Void) place
  triggerB: {}
  branchADone: {type: str}
  branchBDone: {type: str}
  racePermit: {}
  raceWon: {}
  raceDiscarded: {type: str}
transitions:
  Race_Start:      {in: [userIn], out: {and: [triggerA, triggerB, racePermit]}, reset: [racePermit, raceWon]}
  Race_RunBranchA: {in: [triggerA], out: branchADone, inhibit: [raceWon], node: fast}
  Race_CommitA:    {in: [branchADone, racePermit], out: {and: [eventOut, raceWon]}, priority: 10, action: emit}
  Race_DiscardA:   {in: [branchADone], out: raceDiscarded, read: [raceWon], priority: -10}
  # ... the B branch is the same
prove:
  options:
    initial_marking: {userIn: 1}
    sinks: [eventOut, raceWon, raceDiscarded]
    sinks_when: {raceWon: [triggerA, triggerB]}   # the loser's trigger stays
  claims:
    - deadlock_free
    - place_bound: {place: raceWon, bound: 1}
```

The top-level keys are `nodes`, `places`, `transitions`, `env`, `ports`,
`subnets` and `prove`, plus any `BaseNode` field (`name`, `description`,
`input_schema`, ...). Any other key is a load error with a did-you-mean
hint. No key is named `args`, at any depth (ADK's web UI blocks the
file): a place, transition, port, subnet or route label named `args` is a
load error.

- **Places.** `name: {type: T, seed: S}`. `T` is an alias (`str`, `int`,
  `Content`, `Event`, `NodeError`, ...), a dotted name resolved by ADK's
  `resolve_fully_qualified_name`, or `.module.Name` relative to the YAML's
  package. `S` is a count for a unit place or a list of tokens. The catalog
  places (`userIn`, `eventOut`, `turnPermit`, `turnAbort`, `llmRequest`, ...)
  may be used undeclared with their catalog types; any other undeclared place
  is an error.
- **Arcs.** `in` items are `p`, `{place, count}`, `{place, at_least}` or
  `{place, all: true}`. `out` is `p`, `{and: [...]}`, `{xor: [...]}`,
  `{xor: {label: out, ...}}` (route-labelled, `default` and `error` reserved)
  or `{timeout: ms, child: out}`. Also `read`, `inhibit`, `reset`, `priority`,
  and `timing` (`{delayed|deadline|exact: ms}` or `{window: [a, b]}`).
- **Actions**, at most one per transition:
  - none, or `action: move`: consume the inputs, put the one coloured value on
    every coloured output, signal the unit outputs (with no coloured output,
    the value is discarded);
  - `action: emit`: as move, the value turned into an `Event` first;
  - `node: <name>`: run that ADK node through ADK's dynamic node runner, inside
    the turn's invocation. Its input is the consumed and read values (one as
    itself, several as `{place: value}`); its output goes on the chosen
    branch, checked against each place's type (on an `Event` place it becomes
    an `Event`, as `emit` makes it); its route picks a labelled xor branch,
    else `default`; a failure takes the `error` branch with a `NodeError`
    token, or, with no `error` branch, fails the turn as a failing node fails
    a `Workflow`.
  - An xor with more than one choosable branch and no `node` is rejected:
    nothing would choose its branch.

The YAML normalises to one flat `NetSpec`, the same IR as the cross-language
fixtures (`spec/fixtures/nets`), so a YAML net's `spec.fingerprint()` can be
compared with a hand-written one; the Pattern A twin is.

### The turn protocol

A `PetriNet` turn follows `PetriAgent`'s protocol, so the pattern nets carry
over unchanged:

- the invocation's input (the node input, else the user's message) is
  injected on `userIn`, a `str` made a user `Content` when `userIn` takes
  one, any other value checked against `userIn`'s type;
- the first non-partial token on `eventOut` ends the turn: an `Event` is
  yielded as this node's event, any other value becomes `ctx.output`;
- `env: [...]` declares further environment places, filled with
  `PetriNet.inject(session, place, value)`; a `turnPermit` place is seeded
  with one token; `turnAbort` is signalled on any transition failure, and a
  mounted child's `turnAbort` fuses with the parent's;
- a session's net serves one turn at a time (a second invocation waits), and
  no transition may consume, read or inhibit `eventOut`, which keeps every
  turn's answer.

The per-session runner, the registry and the mapping of a turn's result onto
ADK's context are shared with `PetriWorkflow` (`adk_libpetri/_net_node.py`).
Runners are keyed by the node's name and a digest of its net, so two nodes
of one name in one registry keep their own. A node with its own registry
(the default, and every node ADK's loader builds) closes its runners when it
is collected; until then each session it served keeps its runner, so under
`adk web` a session's net lives until its agent is reloaded or the process
ends. A caller that passes a registry closes sessions itself.

### Why `nodes` is a `list[EdgeItem]` field

The ADK nodes a net runs are declared in `nodes:`, typed `list[EdgeItem]`, and
transitions name them. That type is the only one ADK's loader resolves code
and file references for, relative to the YAML file, so `- [.agent.fast]`,
`- [child.yaml]` and inline nodes all work, as they do in a `Workflow`'s
`edges`. Each entry is written as a list (`- [x.yaml]`); a bare string is not
resolved. A field typed `BaseNode` or `dict` would need a loader of our own,
which would drift from ADK's.

### Composition by YAML ref

A child blueprint is another node ref. ADK resolves `- [race.yaml]` relative
to the parent file into a `PetriNet`, whose own `nodes:` resolve relative to
its own file. ADK refuses a ref that leaves the parent's directory
(`../x.yaml`), and a leading dot means the YAML file's directory name as a
package, so a file below the agent's own folder needs fully qualified refs
and types (`app.sub.agent.fn`) under `adk web`. The parent mounts it:

```yaml
subnets:
  first:  {net: two_way_race, bind: {question: q1, answer: r1}}
  second: {net: two_way_race, bind: {question: q2, answer: r2}}
```

- `ports: {name: {place, direction: in|out|inout}}` is a blueprint's
  interface; with none, it is `userIn` (in) and `eventOut` (out).
- A bound port's place fuses with the parent place, whose type must equal the
  port's; an undeclared parent place takes the port's type.
- Every other child place and transition is renamed `inst/<name>`, so one
  blueprint mounts twice; nesting gives `outer/inner/...`. Seeds, unbound env
  places and actions carry over under the prefix.
- An unknown port, an unbound in-port, a type conflict and a ref cycle
  (`a.yaml -> b.yaml -> a.yaml`) are load errors.
- The result is one flat `NetSpec`, so the parent's `prove:` checks the
  composed net.

### Stock subnets

`stock: llm_agent | llm_step | tool_dispatch | router` mounts a stock subnet
under its existing port names, configured from an ADK `LlmAgent` node named
in `from:` (its model, through `LLMRegistry` for a string; its instruction;
its tools). The mounted `llm_agent` is fingerprint-equal to `llm_agent.DEF`
under its prefix. Its `turnAbort` in-port, like any mounted child's, fuses with
the parent's `turnAbort` unless bound elsewhere: only the top-level place is
signalled by the runner. Toolsets and instruction providers are rejected.

### `prove:`

`prove: {options, claims, on_load}`. Claims are `deadlock_free`,
`place_bound`, `unreachable` and `mutual_exclusion`, each one libpetri
`verify()` call, each with an optional `label` and `options` laid over the
shared ones key by key. Options are `initial_marking`, `environment`
(`always`, `arrivals`, `bounded`; one mode for all places), `sinks`,
`sinks_when`, `assume_atomic_firing` and `assume_atomic_nodes`. With no
`environment`, the inputs come as a session's turns do: `userIn` holds one
input, and the next arrives only after an answer is on `eventOut` and no
node run is in flight (a verification-only `turn:next`; each node transition
is split into its start and a `T:deposit`). A safety claim covers one turn,
`deadlock_free` two, and `k` sets the number for both. The `env:` places get
at most `k` arrivals (exactly `k` for `deadlock_free`), and a safety claim
also lets `turnAbort` arrive. An `initial_marking` keeps this, except that an
environment place it seeds is closed. An explicit `environment` drops the
turn order and must list every `env:` place. `assume_atomic_firing` on a net
with nodes or stock subnets needs `assume_atomic_nodes: true` as well. The
default `deadlock_free` sinks are `eventOut`, `turnPermit` and each mounted
subnet's unbound `eventOut`, `turnPermit` and `transfer`. Each verdict says
what it assumed (`NetProof.scope`, `notes`; the CLI's `under:` line). The proofs run from `PetriNet.verify(k=...)`, from the CLI
(`adk-libpetri verify FILE [--k N] [--recursive]`; `adk-libpetri check FILE`
validates without Z3), or at load with `on_load: true`, where a claim not
proven fails the load.

**Soundness.** The proofs are about the net's structure; no action runs.
Colours are abstracted to counts, and every xor is a free choice, so a
`node:` transition's route is covered whichever branch the node picks. A
claim proven on the blueprint therefore holds for every value and route the
nodes produce. `move` and `emit` are synchronous, so `assume_atomic_firing`
is exact for a claim that only needs them to be atomic (the race permit); a
`node:` action is asynchronous, so the loader refuses the option on a net
with nodes unless `assume_atomic_nodes: true` records that judgement.

**Limits.**

- The verifier is untimed: `timing:` and timeout outputs are proof structure,
  not proved latencies. A `delayed` transition is verified as immediate (the
  same untimed claim, which state-space enumeration can then decide).
- A safety claim covers one turn by default, `deadlock_free` two; `--k N`
  covers N. `place_bound: {place: eventOut, bound: 1}` is a per-turn
  statement: `turn:next` takes each answer when the next turn starts, while
  at run time `eventOut` keeps every turn's answer (hence no arc may test
  it). The turn model lets the next input arrive once the node runs are
  done, even if other transitions are still enabled, which over-approximates
  the runtime.
- `deadlock_free` covers runs where no action fails: it never lets
  `turnAbort` arrive. Safety claims do, at any time.
- A turn ends on `eventOut` only, so a turn that ends elsewhere (a stock
  `llm_agent`'s `transfer`) hangs; `deadlock_free` over two turns reports it.
- A mounted child's own `prove:` does not run with the parent; `verify
  --recursive` runs it on the child alone.
- `k` rewrites only `arrivals` bounds; `bounded` and `always` ignore it.
- Budgets are stated in seeds, as elsewhere: libpetri models a K-token seed
  as one token.

### Deviations from the first design

- **Leading-dot place types.** `type: .agent.Draft` resolves against the
  package of the YAML file being loaded, with ADK's rule (the file's
  directory name). The node finds that file on ADK's loader frames (the
  `abs_path` of `from_config`, or the mapper's for an inline node), and stops
  at an import boundary. A `PetriNet` built in Python has no file and needs
  fully qualified names. Ref cycles are caught the same way, before ADK would
  recurse.
- **The invocation stays open until in-flight node runs finish.** The answer
  event is yielded as soon as it exists, but the invocation waits for every
  node run the turn started (a race's loser runs inside it). Without that, a
  loser's ADK node run on an ended invocation never returned and hung the
  runner's drain. A node failure after the answer does not fail the turn. A
  node transition that fires with no turn open (a seeded one, a timed one
  after the answer) keeps its tokens in flight and runs in the next turn's
  invocation; if the session's net closes first, it fails. Turns of one
  session are served one at a time.
- **No interrupts.** A node that requests input (`RequestInput`, tool
  confirmation) takes the `error` branch or fails the turn, and
  `ctx.resume_inputs` raise `NetRunError`. `PetriNet` sets
  `rerun_on_resume=True` only because ADK requires it to schedule child
  nodes dynamically.

## Consequences

- An agent or a person can write a net in the same file format as the rest
  of an ADK app, served by `adk web` and `adk run`, and check it before it
  runs. Errors name the YAML key path (`transitions.Race_Commit.out.xor[1]`)
  and a fix.
- Node runs share the invocation's branch, as sequential `Workflow` nodes do,
  so two concurrent `LlmAgent` nodes in one net see each other's events. A
  per-transition sub-branch option may be needed.
- **Open issue: timeout outputs do not run.** When a timeout output fires,
  libpetri-py writes a unit `()` token, and `MarkingView` fails to decode it
  ("Expected python.object token, found ()"). Parsing, fingerprints and
  proofs handle timeout outputs; running them waits on a libpetri fix.

## Next steps

- A Java loader for the same format: the YAML normalises to the fixture IR
  that both ports already golden-check, so it can follow.
- The remaining stock subnets as `stock:` blocks (`persist_state`,
  `transfer_router`, the streaming pair).
- Exporting a compiled workflow (`from_workflow`) to blueprint YAML, so a
  compiled `Workflow` can be read and edited as a net.
