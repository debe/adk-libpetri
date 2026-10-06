# Authoring net blueprints

A guide for an agent (or a person) writing a Petri net as an ADK YAML file,
`agent_class: adk_libpetri.net.PetriNet`. `adk web` and `adk run` load it like
any `root_agent.yaml`. The JSON Schema next to this file
(`adk-libpetri schema`) gives the exact shape.

## The loop

1. Write the YAML, starting from a motif below.
2. `adk-libpetri check FILE`. It loads the file through ADK's loader, builds
   the net and prints `OK ...`, or one `ERROR <file>: <key path>: <message>.
   Fix: <hint>` line. Fix the key the path names, then run it again.
3. `adk-libpetri verify FILE`. It proves each `prove:` claim and prints one
   `PROVEN`, `VIOLATED` or `UNKNOWN` line per claim, then an `under:` line
   naming the run the claim covers (`1 turn`, `2 turns, ...`). A violated
   claim also prints its counterexample: the transitions that fire, in
   order, and the marking after each step.
4. Fix from the counterexample: find the step where the bad marking appears.
   That transition's arcs are the bug. Re-run `verify`. Exit code 0 means
   every claim is proven.

`verify --k 3` checks every claim over three turns instead of the default
(one turn for a safety claim, two for `deadlock_free`). `verify --recursive`
also proves each mounted child blueprint's own `prove:` on the child alone.
`check` needs no Z3. `verify` needs the `z3` binary for claims that
state-space enumeration cannot close.

## The format

- **`nodes:`** lists the ADK nodes the transitions run. Write each entry as
  a list: `- [helper.yaml]`, `- [.agent.my_fn]` (a function in
  `agent.py` next to the YAML, named `my_fn`), `- [.agent.a, .agent.b]`,
  or an inline `- [{agent_class: LlmAgent, name: helper, ...}]`. A bare
  entry (`- x.yaml`, `- {agent_class: ...}`) is a load error.
  - A leading dot means the YAML file's own directory name as a package
    (ADK's rule). It works for a file directly inside the agent's folder,
    which `adk web` imports. A YAML one directory deeper needs fully
    qualified refs (`my_app.sub.agent.fn`); so does a place type there.
  - ADK resolves `x.yaml` relative to the file and refuses `../x.yaml`: a
    parent can mount children in its directory or below it, not a sibling
    package's file.
- **`places:`** maps a name to `{type: T, seed: S}`.
  - With no type, the place is a unit place (black tokens), written `{}`.
  - T is an alias (`str`, `int`, `float`, `bool`, `dict`, `list`, `object`,
    `Content`, `Event`, `NodeError`, ...), a dotted name, or `.agent.MyType`.
  - `seed` is a count for a unit place, or a list of values for a coloured
    one.
  - `userIn` (Content) and `eventOut` (Event) need no declaration. Nor do
    the other catalog places (`turnPermit`, `turnAbort`, `llmRequest`, ...).
- **`transitions:`** maps a name (`Net_Verb`, no `/`) to arcs, timing and
  one action.
  - `in`: `p`, `{place: p, count: n}`, `{place: p, at_least: n}` or
    `{place: p, all: true}`.
  - `out`: `p`, `{and: [...]}`, `{xor: {label: out, ...}}` or `{xor: [p, q]}`.
  - `read: [p]` needs a token on p and leaves it there. `inhibit: [p]` is
    enabled only while p is empty. `reset: [p]` empties p when the
    transition fires.
  - `priority: n` decides which fires first among transitions enabled at
    the same time. The motifs use +10 for commits and -10 for discards.
  - `timing`: `{delayed: ms}`, `{deadline: ms}`, `{exact: ms}` or
    `{window: [a, b]}`.
- **`env: [p]`** adds environment places: an approval, a webhook, a
  sensor. Fill one with `PetriNet.inject(session, place, value)` while a
  turn runs. A turn blocks until `eventOut`, so the value comes from another
  task. `adk web` and `adk run` cannot fill an env place. `userIn` is always
  an environment place.
- **`ports:`**, **`subnets:`** and **`prove:`** are covered below.

Unknown keys, unknown places and unknown nodes are load errors with a
did-you-mean hint. No key may be named `args` at any depth (no place,
transition, port, subnet or route label): `adk web` refuses the file.

## The turn

- The invocation's input goes onto `userIn`: the node input, else the
  user's message (a `Content`). A `str` input becomes a user `Content`
  when `userIn` takes one; any other input must be of `userIn`'s type
  (declare `userIn: {type: str}` for a net inside a `Workflow`).
- The first token on `eventOut` ends the turn. An `Event` token is yielded
  as the node's event. Any other value (declare `eventOut: {type: int}`)
  becomes the node's output. A turn ends on `eventOut` only: a net that can
  end a turn elsewhere (a stock `llm_agent`'s `transfer`) must answer it
  too, or that turn and every later one hang.
- After the answer, the invocation stays open until every node run started
  this turn has finished. A race's loser still completes.
- A session's net serves one turn at a time; a second invocation of the
  session waits. A node transition that fires between turns (a seeded
  one, a timed one after the answer) keeps its tokens and runs in the next
  turn's invocation.
- A session's net lives across turns: a token left on a place is still
  there next turn. Reset per-turn places in the transition that starts the
  turn. No transition may consume, read or inhibit `eventOut`: mark a place
  of your own next to it.
- Prove `place_bound: {place: eventOut, bound: 1}`: a turn answers once.
- A session's net lives until its registry closes it. A net built by ADK's
  loader keeps its own registry, closed when the node is collected (an
  agent reloaded by `adk web`); until then every session it served keeps
  its runner. Pass a registry to `PetriNet.from_config` or `serve_on` and
  close sessions from a session-end hook to end them sooner.

## Actions

Each transition has at most one action.

- **No action (move).** Consumes its inputs. If an output is coloured, the
  transition must consume exactly one token of exactly one coloured place,
  of a type the outputs accept. That value goes to every coloured output.
  Unit outputs get a black token. A coloured input with no coloured output
  is dropped, which is how a discard is written.
- **`action: emit`.** Like move, but the value becomes an `Event` first. A
  `str` becomes model text and `Content` becomes content, each also the
  event's `output`; anything else becomes `Event(output=value)`. Write it
  on the transition that puts the answer on `eventOut`.
- **`node: name`.** Runs the ADK node inside the turn's invocation.
  - Its input is the coloured values the transition consumes or reads. One
    value is passed as itself, several as `{place: value}`, and a
    `count`/`at_least`/`all` arc as a list.
  - Its output goes on every coloured place of the chosen branch, and must
    be of the place's type. On an `Event` place (`eventOut`) it becomes an
    `Event` as `emit` makes it, so a node can answer the turn at once. A
    node that returns a `types.Content` gives no output (ADK hands on its
    content only): return `Event(output=content)`.
  - On an xor, the node's route picks the branch: return
    `Event(output=v, actions=EventActions(route="label"))`. `default`
    catches any other route. In a plain list `{xor: [p, q]}` each branch
    that is one place is routed by its place name; prefer labels.
  - If the node fails, the `error` branch is taken with a `NodeError`
    token. Declare its coloured places `type: NodeError`. With no `error`
    branch, the turn fails as a Workflow node fails.
- A transition takes `node:` or `action:`, not both. To emit a node's
  output, put it on a place and add `{in: [p], out: eventOut, action: emit}`.
- An xor with no node is an error, because nothing would choose the branch.

## Motifs

Every block below is a complete file, checked and proven by the test
suite. The `.agent.*` refs are functions in `agent.py` next to the YAML.

### Permit race: the first result wins, once

The start puts one permit down. A commit consumes it, so a second commit
cannot fire. The inhibitor on `won` is not what makes the race safe.
`inhibit: [won]` on the runs cancels the loser if it has not started yet.
The discards drain a late result.

```yaml
# file: race.yaml
agent_class: adk_libpetri.net.PetriNet
name: race
nodes:
  - [.agent.fast, .agent.slow]
places:
  goA: {}
  goB: {}
  doneA: {type: str}
  doneB: {type: str}
  permit: {}
  won: {}
  late: {type: str}
transitions:
  Race_Start:
    in: [userIn]
    out: {and: [goA, goB, permit]}
    reset: [permit, won, goA, goB, doneA, doneB, late]
  Race_RunA: {in: [goA], out: doneA, inhibit: [won], node: fast}
  Race_RunB: {in: [goB], out: doneB, inhibit: [won], node: slow}
  Race_CommitA: {in: [doneA, permit], out: {and: [eventOut, won]}, priority: 10, action: emit}
  Race_CommitB: {in: [doneB, permit], out: {and: [eventOut, won]}, priority: 10, action: emit}
  Race_DiscardA: {in: [doneA], out: late, read: [won], priority: -10}
  Race_DiscardB: {in: [doneB], out: late, read: [won], priority: -10}
prove:
  options:
    sinks: [eventOut, won, late]
    sinks_when: {won: [goA, goB]}
  claims:
    - deadlock_free
    - place_bound: {place: won, bound: 1}
      label: one winner
    - place_bound: {place: eventOut, bound: 1}
```

Writing the commits as `inhibit: [won]` with no permit looks equivalent,
but it is not. Two results that are ready at the same time both commit,
and `verify` prints that firing sequence.

### Quorum: answer at exactly k of n

`count: 2` puts k in the arc. The tally fires once two votes are there and
gets them as a list. The `met` place stops a second tally, and late votes
drain to `late`.

```yaml
# file: quorum.yaml
agent_class: adk_libpetri.net.PetriNet
name: quorum
nodes:
  - [.agent.voter_a, .agent.voter_b, .agent.voter_c]
  - [.agent.tally]
places:
  question: {type: Content}
  go1: {}
  go2: {}
  go3: {}
  vote: {type: str}
  verdict: {type: str}
  met: {}
  late: {type: str}
transitions:
  Quorum_Start:
    in: [userIn]
    out: {and: [question, go1, go2, go3]}
    reset: [question, vote, met, late]
  Quorum_Vote1: {in: [go1], read: [question], out: vote, node: voter_a}
  Quorum_Vote2: {in: [go2], read: [question], out: vote, node: voter_b}
  Quorum_Vote3: {in: [go3], read: [question], out: vote, node: voter_c}
  Quorum_Tally:
    in: [{place: vote, count: 2}]
    out: {and: [verdict, met]}
    inhibit: [met]
    priority: 10
    node: tally
  Quorum_Answer: {in: [verdict], out: eventOut, action: emit}
  Quorum_Late: {in: [vote], out: late, read: [met], priority: -10}
prove:
  options:
    sinks: [eventOut, met, late, vote, question]
  claims:
    - deadlock_free
    - place_bound: {place: met, bound: 1}
    - place_bound: {place: eventOut, bound: 1}
```

### Budget: retry, then an inhibitor fallback

`budget` holds the retries left. A retry consumes one. Once it is empty,
the inhibitor enables the fallback. The question sits on a read arc, so
each attempt sees it again. Here the start refills one retry per turn. For
a budget per session, give the place `seed: 3` and leave it out of the
start.

```yaml
# file: retry.yaml
agent_class: adk_libpetri.net.PetriNet
name: retry
nodes:
  - [.agent.flaky, .agent.apologise]
places:
  question: {type: Content}
  attempt: {}
  budget: {}
  answer: {type: str}
  failed: {type: NodeError}
transitions:
  Retry_Start:
    in: [userIn]
    out: {and: [question, attempt, budget]}
    reset: [question, budget, failed]
  Retry_Try:
    in: [attempt]
    read: [question]
    out: {xor: {ok: answer, error: failed}}
    node: flaky
  Retry_Again: {in: [failed, budget], out: attempt}
  Retry_GiveUp:
    in: [failed]
    inhibit: [budget]
    out: answer
    priority: -10
    node: apologise
  Retry_Answer: {in: [answer], out: eventOut, action: emit}
prove:
  options:
    sinks: [eventOut, question, budget]
  claims:
    - deadlock_free
    - place_bound: {place: eventOut, bound: 1}
    - place_bound: {place: budget, bound: 1}
```

### Optimistic commit: a cheap answer, validated, else the slow one

The validator node routes `passed` or `failed`. It returns
`Event(output=draft, route="passed")`. The xor makes the two verdicts
exclusive. The slow path commits only on `failed`, and is discarded once
something committed.

```yaml
# file: optimistic.yaml
agent_class: adk_libpetri.net.PetriNet
name: optimistic
nodes:
  - [.agent.cheap, .agent.slow, .agent.validate]
places:
  question: {type: Content}
  goCheap: {}
  goSlow: {}
  cheapDone: {type: str}
  slowDone: {type: str}
  draft: {type: str}
  passed: {}
  failed: {}
  committed: {}
  late: {type: str}
transitions:
  Opt_Start:
    in: [userIn]
    out: {and: [question, goCheap, goSlow]}
    reset: [question, goCheap, goSlow, cheapDone, slowDone, draft, passed, failed, committed, late]
  Opt_RunCheap: {in: [goCheap], read: [question], out: cheapDone, node: cheap}
  Opt_RunSlow: {in: [goSlow], read: [question], out: slowDone, inhibit: [committed], node: slow}
  Opt_Validate:
    in: [cheapDone]
    out: {xor: {passed: {and: [passed, draft]}, failed: failed}}
    node: validate
  Opt_CommitCheap:
    in: [passed, draft]
    out: {and: [eventOut, committed]}
    inhibit: [committed]
    priority: 10
    action: emit
  Opt_CommitSlow:
    in: [slowDone]
    read: [failed]
    out: {and: [eventOut, committed]}
    inhibit: [committed]
    priority: 10
    action: emit
  Opt_DiscardSlow: {in: [slowDone], out: late, read: [committed], priority: -10}
prove:
  options:
    sinks: [eventOut, committed, late, failed, question]
    sinks_when: {committed: [goSlow]}
  claims:
    - deadlock_free
    - place_bound: {place: committed, bound: 1}
    - mutual_exclusion: [passed, failed]
```

### Timed escalation: no answer in time, then a fallback

`waiting` is the clock. The reply consumes it, and so does the delayed
escalation, which can fire only once `waiting` has been marked for 5000 ms.
Exactly one of them wins. A reply that comes after the escalation drains
to `late`. The proofs are untimed: `inhibit: [asked]` says what the clock
already ensures, that the specialist was asked before the escalation, so a
proof over two turns does not see an ask from the first turn answer the
second.

```yaml
# file: escalate.yaml
agent_class: adk_libpetri.net.PetriNet
name: escalate
nodes:
  - [.agent.specialist, .agent.canned_reply]
places:
  asked: {type: Content}
  waiting: {}
  reply: {type: str}
  due: {}
  fallback: {type: str}
  escalated: {}
  late: {type: str}
transitions:
  Esc_Start:
    in: [userIn]
    out: {and: [asked, waiting]}
    reset: [asked, waiting, reply, due, fallback, escalated, late]
  Esc_Ask: {in: [asked], out: reply, node: specialist}
  Esc_Reply: {in: [reply, waiting], out: eventOut, priority: 10, action: emit}
  Esc_Escalate:
    in: [waiting]
    inhibit: [asked]
    out: {and: [due, escalated]}
    timing: {delayed: 5000}
  Esc_Fallback: {in: [due], out: fallback, node: canned_reply}
  Esc_FallbackEmit: {in: [fallback], out: eventOut, action: emit}
  Esc_Late: {in: [reply], out: late, read: [escalated], priority: -10}
prove:
  options:
    sinks: [eventOut, escalated, late]
  claims:
    - deadlock_free
    - place_bound: {place: eventOut, bound: 1}
```

Prefer a timed transition to a `{timeout: ms, child: out}` output for now.
Timeout outputs parse and prove, but libpetri-py cannot yet run them.

## Ports and subnets

A blueprint can be mounted inside another. The child declares its
interface under `ports:`. With no `ports:`, its ports are `userIn` (in) and
`eventOut` (out). The parent lists the child YAML under `nodes:` and mounts
it under `subnets:`, binding each port to a parent place.

- The child's other places and transitions are renamed `<inst>/<name>`.
  The same file can be mounted twice, and claims can name `inst/won`.
- Every `in` port must be bound.
- A bound place takes the port's type. An undeclared parent place is
  created with it.

```yaml
# file: answer_race.yaml
agent_class: adk_libpetri.net.PetriNet
name: answer_race
nodes:
  - [.agent.fast, .agent.slow]
places:
  question: {type: str}
  answer: {type: str}
  goA: {type: str}
  goB: {type: str}
  doneA: {type: str}
  doneB: {type: str}
  permit: {}
  won: {}
ports:
  question: {direction: in}
  answer: {direction: out}
transitions:
  Race_Start:
    in: [question]
    out: {and: [goA, goB, permit]}
    reset: [permit, won]
  Race_RunA: {in: [goA], out: doneA, inhibit: [won], node: fast}
  Race_RunB: {in: [goB], out: doneB, inhibit: [won], node: slow}
  Race_CommitA: {in: [doneA, permit], out: {and: [answer, won]}, priority: 10}
  Race_CommitB: {in: [doneB, permit], out: {and: [answer, won]}, priority: 10}
  Race_DiscardA: {in: [doneA], read: [won], priority: -10}
  Race_DiscardB: {in: [doneB], read: [won], priority: -10}
prove:
  options:
    environment: {question: {arrivals: 1}}
  claims:
    - place_bound: {place: won, bound: 1}
```

The parent below mounts it twice. The `split` and `join` nodes convert
the user's message to `str` and merge the two answers. A transition with
several coloured inputs hands its node `{place: value}`, here
`{a1: ..., a2: ...}`.

```yaml
# file: two_races.yaml
agent_class: adk_libpetri.net.PetriNet
name: two_races
nodes:
  - [answer_race.yaml]
  - [.agent.split, .agent.join]
places:
  q1: {type: str}
  q2: {type: str}
  joined: {type: str}
subnets:
  first: {net: answer_race, bind: {question: q1, answer: a1}}
  second: {net: answer_race, bind: {question: q2, answer: a2}}
transitions:
  Two_Split: {in: [userIn], out: {and: [q1, q2]}, node: split}
  Two_Join: {in: [a1, a2], out: joined, node: join}
  Two_Answer: {in: [joined], out: eventOut, action: emit}
prove:
  claims:
    - place_bound: {place: first/won, bound: 1}
    - place_bound: {place: second/won, bound: 1}
    - place_bound: {place: eventOut, bound: 1}
```

`adk-libpetri verify two_races.yaml --recursive` proves the parent's
claims, then `answer_race`'s own claims on the child alone.

### Stock subnets

`stock:` mounts a built-in subnet configured from an ADK node named by
`from:`. The kinds and their ports:

| stock | ports (direction) | `from:` |
|---|---|---|
| `llm_agent` | `userIn` (in), `turnAbort` (in), `eventOut` (out), `transfer` (out) | an `LlmAgent`, required |
| `llm_step` | `llmRequest` (in), `llmResponse` (out) | an `LlmAgent`, required |
| `tool_dispatch` | `toolCalls` (in), `toolResults` (out) | an `LlmAgent` whose tools run (optional) |
| `router` | `llmResponse` (in), `toolCalls`, `transfer`, `eventOut` (out) | optional; names the author |

`llm_agent` takes the agent's `model`, its `instruction` (a string) and
its `tools`. Toolsets and instruction providers are rejected.
Its `turnAbort` port fuses with the net's own `turnAbort` unless you bind
it elsewhere, as does any mounted child's: the runner signals that place
when a transition fails, so the subnet ends the turn instead of hanging.
A turn the model ends with a transfer puts nothing on `eventOut`: bind
`transfer` and answer it, as below.

```yaml
# file: helper.yaml
agent_class: LlmAgent
name: helper
model: gemini-2.5-flash
instruction: You answer weather questions briefly.
```

```yaml
# file: assistant.yaml
agent_class: adk_libpetri.net.PetriNet
name: assistant
nodes:
  - [helper.yaml]
subnets:
  llm:
    stock: llm_agent
    from: helper
    bind: {userIn: userIn, eventOut: eventOut, transfer: transfer}
transitions:
  Assistant_Handoff: {in: [transfer], out: eventOut, action: emit}
prove:
  claims:
    - place_bound: {place: llm/LlmAgent_turnActive, bound: 1}
```

## `prove:`

```text
prove:
  options:            # shared by every claim
    initial_marking:  {place: n}            # laid over the seeds
    environment:      {place: always | {arrivals: k} | {arrivals: [lo, hi]} | {bounded: k}}
    sinks:            [place]               # deadlock_free: where tokens may rest
    sinks_when:       {marker: [place]}     # ...and these, while marker is marked
    assume_atomic_firing: true
    assume_atomic_nodes: true               # needed with the line above on a net with nodes
  claims:
    - deadlock_free
    - place_bound: {place: p, bound: n}
    - unreachable: [p, q]                   # never all marked at once
    - mutual_exclusion: [p, q]              # never two marked at once
    - place_bound: {place: p, bound: 1}
      label: my name for the verdict line
      options: {...}                        # laid over the shared options
  on_load: true                             # prove when the file loads; fail if not proven
```

- **Turns.** With no `environment`, the user's inputs come as a session's
  turns do: the next one on `userIn` only once an answer is on `eventOut`
  and the turn's node runs are done. A safety claim covers one turn,
  `deadlock_free` two (a first turn that leaves the net unable to answer
  the next is a deadlock); `--k N` sets N turns for every claim.
  `place_bound: {place: eventOut, bound: 1}` then means one answer a turn.
- **Environment places.** Every `env:` place gets at most k arrivals
  (exactly k for `deadlock_free`). A safety claim also lets `turnAbort`
  arrive, since the runner signals it on any failure or ADK abort;
  `deadlock_free` covers runs where nothing fails.
- `initial_marking` keeps all of that. An environment place it seeds is
  closed: `initial_marking: {userIn: 1}` covers that one input and no
  further turn.
- `environment` replaces the turn order: the places it lists arrive in any
  order, `userIn` among them. It must list every `env:` place the
  `initial_marking` does not seed. Every place takes the same mode
  (libpetri's rule). `{arrivals: k}` means at most k, so possibly none;
  `{arrivals: [k, k]}` means exactly k. An optional input such as an
  approval or a webhook then arrives under the same mode as `userIn`.
- `sinks` defaults to `eventOut`, `turnPermit`, and each mounted subnet's
  unbound `eventOut`, `turnPermit` and `transfer` (`assistant/turnPermit`).
  Any other place a turn leaves a token on must be a sink, or
  `deadlock_free` reports it.
- `sinks_when` excuses a place only while a marker is marked: for example
  a loser's trigger that the winner's `won` inhibits.
- Proofs check the structure only and never run an action. A node with an
  xor may take any branch. They are untimed: a `delayed` transition may
  fire as soon as it is enabled, so state what a delay ensures with an arc
  (the escalation motif's `inhibit: [asked]`).
- `assume_atomic_firing` reads every firing as one step. A node runs while
  other transitions fire, so on a net with nodes the loader refuses it
  unless `assume_atomic_nodes: true` says that the claim's counterexample
  needs only move and emit transitions to be atomic. `verify` prints the
  assumption under the verdict.
- `unknown` is not a proof. The usual cause is a state space too large to
  close. Bound the environment, or check a smaller net.

## Reading a counterexample

```text
VIOLATED place_bound(raceWon, 1)  [place_bound]
  fires: Race_CommitA -> Race_CommitB -> complete:Race_CommitA -> complete:Race_CommitB
  markings:
    0: {branchADone: 1, branchBDone: 1}
    ...
    4: {eventOut: 2, raceWon: 2}
```

- `complete:T` is the completion step of transition T. The verifier
  splits a firing into a start, which consumes, and a completion, which
  deposits. Other transitions fire in between, as they do at run time.
- `inflight:T` in a marking is T's start without its completion yet.
- `complete:T_b<i>` is T completing into xor branch i (counted from 0).
- `env:arrive[i]:P` and `env:arrive?[i]:P` are a mandatory and an optional
  arrival on environment place P; `env:decline[i]` is an optional arrival
  that never comes. `env:optional[i]` and `env:arrivals[i]` in a marking
  hold the arrivals still to come.
- `turn:next` starts the next turn: it moves the answer to `turn:answered`
  and puts the next input on `userIn`. `turn:remaining` holds the turns to
  come. In a multi-turn proof a node transition T shows as T (it starts the
  run, marking `T:running`) and `T:deposit` (the output lands); `turn:quiet`
  keeps `turn:next` out of either step.
- Here both commits started before either deposited `raceWon`, so the
  inhibitor never saw it. The fix is a consumed permit: see the permit
  race.
