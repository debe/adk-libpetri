# ADR 0009: Nets in ADK's web UI, and a Petri builder assistant (removed)

- **Status:** Accepted; amended 2026-11-27 (google-adk 2.15 removed ADK's
  Visual Agent Builder: the Petri builder assistant is gone, see below)
- **Date:** 2026-10-07
- **Scope:** Python `0.1.0`, `adk_libpetri.web`, `net.graph`, `net.report`,
  `net.counterexample`, `bridge.marking_trace` (all `@experimental`).
  google-adk `~=2.15.1` (first written against `2.11.0`; the builder parts,
  now removed, against `2.13.0`).

## Amendment: google-adk 2.15 removed the builder

google-adk 2.15.0 removed ADK's Visual Agent Builder (breaking, in its
release notes): the `/dev/apps/{app_name}/builder*` routes,
`google.adk.cli.built_in_agents` (`AgentBuilderAssistant`,
`resolve_file_path`), every `__`-prefixed agent
(`NestedAgentLoader._validate_agent_name` now rejects any name starting with
`__`), and the builder panel and canvas in the UI bundle. Its release notes
point builders to a coding agent with skills instead. On 2.15 every seam
the builder half of this ADR used is gone: there is no route to guard, no
canvas to answer, no assistant to extend, and no name to serve one under.

So this package drops everything that existed only for the builder:

- `web/builder.py` (`create_petri_builder_assistant`, its tools, greeting,
  verdict cache and canned replies), `web/builder_guard.py`,
  `web/canvas_view.py`, `web/drafts.py` and `web/staging.py`, and the
  loader's branch that served the assistant under
  `__adk_agent_builder_assistant`;
- `web/proof_view.py`, the counterexample-picture route. Its pictures were
  linked only from the assistant's replies: the graph panel never shows a
  marking (below), and the CLI prints text. With no page left to link them,
  the route and its in-memory store served nothing. `net.counterexample`
  stays as a library (`steps`, `step_lines`, `counterexample_svg` on a
  `verify_file` claim and its `graphs[claim.net]`); only `missing_svg`, the
  route's 404 picture, went with it;
- in `server.py`, the runner-cache eviction after Save (`_forgetter`,
  `adk_web_server`) and the `chdir` into the agents folder, which only the
  guard and the assistant's file tools needed. `build_app` still passes
  `host` as ADK's `bind_host`, as `adk web` does: since 2.15 that turns on
  ADK's DNS-rebinding guard for a loopback bind and warns on any other.

What the builder assistant taught is now a coding-agent skill in the Agent
Skills format (`net/SKILL.md`, a `name`/`description` frontmatter, printed by
`adk-libpetri skill`), on the CLI the assistant's tools wrapped: when a net
beats ADK's workflow agents; the loop (write the YAML, `adk-libpetri check`,
`adk-libpetri verify`, fix from the counterexample's steps; a net is not
done while a claim is violated or unknown); the report at the end of each
reply (size, each verdict as `verify` printed it); and the conventions. It
points at `adk-libpetri guide [section]` (the assistant's
`petri_authoring_guide(section)`, now a CLI argument) and
`adk-libpetri schema` instead of copying `AUTHORING.md`.

The graph panel, the loader's helper-package filter and traces, and the
answer's node path (Layer 1, Layer 2's graph view, Traces) are unchanged and
work on 2.15. The sections below on the guard, the drafts, the canvas, the
staged copies, the pictures and the assistant are removed from this ADR;
git history has them.

## Context

ADR 0008 lets a person or an agent write a net in ADK's YAML and prove it
before it runs. Two gaps remained:

- `adk web` served a blueprint but showed nothing of it. Its graph view drew
  a `PetriNet` as one box, and it could not check, verify or replay a net.
- ADK's dev UI had (until 2.15) an LLM Agent Builder Assistant that writes
  `root_agent.yaml`. It knows only AgentConfig, and its `write_config_files`
  check accepts a blueprint with any content, a broken one included:
  `PetriNet`'s keys are extra keys to that schema.

The goal: everything stays the same for an ADK dev-UI user, and nets just
work in it. They open `/dev-ui/`, see the net drawn in ADK's own graph
panel, and chat with the net (until 2.15 they also chatted with the builder
assistant, the pencil button). A page of our own is not the product.

ADK's frontend is a minified Angular bundle with no extension points, and we
fork nothing (design commitment 4): no patch to ADK's Python files, no
rewrite of its compiled JavaScript. Four seams in ADK 2.11 are enough (a
fifth, the builder assistant's name and file tools, went with the builder in
2.15):

1. `get_fast_api_app(agent_loader=...)` takes a custom loader and returns
   the FastAPI app.
2. Starlette matches routes in order, so a route inserted at
   `app.router.routes[0]` answers before ADK's route with the same path.
   We change what the UI shows by answering the UI's own requests.
3. The dev UI's graph view draws any node's pydantic field named `graph`
   whose value has `.nodes[].name` and `.edges[].from_node/.to_node/.route`
   (`cli/utils/graph_serialization.py`). It has no class check, and
   `cli/utils/state.py` only iterates `graph.nodes`. `runners.py`
   (`_collect_toolset`, which closes toolsets on `Runner.close()`) walks
   `_iter_static_children` since 2.13; `NetNodeBase` overrides it to name
   the ADK nodes a net runs, so their toolsets are closed.
4. The UI lights up what ran on the client. A drawn node lights up when its
   `<title>` (the DOT node id) equals an event's author or the last segment
   of its `nodeInfo.path`, or when its label text contains that name
   (`applyV1Highlighting`, `highlightExecutionPathInSvg`). A net's `node:`
   runs have paths like `race@1/fast@1`.
Spikes in a browser found what the UI cannot do:

- `EventActions.agent_state` does **not** colour the graph for a
  non-`Workflow` node; the UI uses it only for its "Agent State" button.
  We emit no extra events, and a turn's event stream is unchanged.
- The UI fetches `build_graph_image` once per app and never calls the
  per-event graph route (`.../events/{e}/graph`). The graph panel therefore
  cannot show a marking; highlighting what ran is what it shows.

## Decision

### Shared core

- `net/report.py` returns the CLI's results as data:
  - `check_file` gives `CheckReport` (the error's YAML key path and hint);
  - `verify_file` gives `VerifyReport`, with a `ClaimResult` per claim
    (`places`, `bound`) and, for a violated claim, `fires` and `markings`.
- `adk-libpetri check|verify --json` prints those results. The text output
  is unchanged.
- `net/graph.py` builds a `NetGraph` from a `NetSpec`: places (type, seeds,
  env, port), transitions (action, priority, timing, subnet), arcs
  (`in`/`out`/`read`/`inhibit`/`reset`, counts, xor route labels) and the
  mounted `subnets`. `to_dot(marking, fired, theme=, collapse_stock=,
  collapse_mounts=, alarm=, compact=, agent=, key=, author=)` draws it:
  - in the dev UI's light or dark palette; ellipse places with token dots,
    env places dashed, ports double-bordered; box transitions with a
    second line (`node:fast`, `emit`, `prio 10`, timing), blue-bordered
    when they run an ADK node; `odot`-headed red inhibitors, dashed read
    arcs; out-arcs keep their order (`ordering=out`), so `triggerA`,
    `triggerB`, `triggerC` stay in that order;
  - read, inhibitor and reset arcs take no part in ranking
    (`constraint=false`). A transition with more than two reset arcs lists
    the places, shortened (`resets trigger{A,B,C}, ...`), and a place that inhibits or is read by three or more
    transitions is named on each of them (`unless raceWon` in red, `reads
    raceWon`), their tooltips listing them, instead of drawing the arcs: the
    race's nine resets and three inhibitors from `raceWon` had tangled the
    drawing;
  - mounted blueprints are labelled clusters (`first · race_agent`) whose
    members carry their short names. A mounted transition keeps its own
    action, so `Mount` records the child blueprint and a stock subnet's
    `from:` agent;
  - a stock subnet, and in a net of more than 15 places every mounted
    blueprint, is one rounded node (`assistant` over `llm_agent ·
    summarizer`; `first` over `race_agent · 9 places` and `runs first·fast,
    first·medium, first·slow`) wired to the parent places it binds. `NetGraph.sub(prefix)` is a
    subnet as a net of its own, the parent places it binds as its ports;
  - node ids are the exact place and transition names (a transition that
    shares a place's name gets `t:`), so ADK's highlighting matches them.
    Side arcs attach through a port (`"raceWon" -> "Race_RunA":_`), so their
    SVG titles name no node and ADK's path walk (below) skips them. A
    word joiner keeps the net's name, and each run's name outside its own
    transition, from matching ADK's text search (below);
  - a proof splits a timed action into a start, `complete:T` and an
    `inflight:T` place, and the DOT maps all three back onto `T`.

### Layer 1: stock `adk web`

`PetriNet` and `PetriWorkflow` gain a derived `graph` field holding the
`NetGraph`. A YAML file cannot set it (a key-path error). For ADK's view it
exposes:

- its places and transitions as stub nodes;
- the token-flow arcs (`in`, `out`, `read`) as plain edges. Routed edges
  would make ADK draw a router diamond with a "NO DEFAULT" warning;
- each `node:` transition's ADK node beside it, so the dev UI lights the
  node up from the event path.

It has one shape for every node and no arc kinds: the flow at a glance.
Each top-level subnet is also a node, named by its prefix, that carries a
`graph` of its own (`NetGraph.sub`). ADK serializes a node with
`model_fields` field by field, `graph` included, and the "Agent Structure"
view can open such a node (below).

### Layer 2: `adk-libpetri web` is ADK's dev UI, Petri-aware

`build_app` is ADK's `get_fast_api_app` with `PetriAgentLoader` as its
loader, plus one route inserted ahead of ADK's (seam 2). It answers only for
a net and calls ADK's own handler directly for everything else. `PetriAgentLoader` is
ADK's `NestedAgentLoader`, the one stock `adk web` builds, so apps in
sub-folders (`group/app`, listed as `group.app`) are found as there. `serve()`
prints the dev UI's URL and nothing else.

| What the user does in ADK's UI | Request | Our answer |
| --- | --- | --- |
| Opens a net app (Info panel, "Agent Structure") | `GET /dev/apps/{app}/build_graph_image?dark_mode=` | the net as Petri DOT, and each subnet's |
| Opens a subnet in "Agent Structure" | `GET /dev/apps/{app}/build_graph_image?node=<path>` | that subnet's DOT |

**ADK's graph panel draws the Petri net** (`graph_view.install_graph_view`).
The UI asks `build_graph_image` once per app for `{"<path>": {"dotSrc":
...}}` (`""` is the root) and renders the DOT itself, in the Info panel and
the fullscreen "Agent Structure" view. Fed the duck-typed `graph`, ADK's
answer is one generic box per place and transition. Our route answers for a
`PetriNet` or `PetriWorkflow` root with `to_dot` in the requested theme, and
with each subnet's drawing under its prefix (`first`, `first/inner`). Other
apps, apps that fail to load, and a `node=` that is not a subnet go to ADK.

Highlighting (seam 4) works as follows. For every event of the invocation
up to the selected one, the UI takes the last `nodeInfo.path` segment, and
for the selected event its author too. It maps each name to one drawn node:
the first whose label text or title equals it, else the first whose label
text or title contains it (text run together, lower case, spaces as `_`). It
then walks back from each such node through every predecessor that is its
node's only one (`calculateVisitedPath`), reading predecessors from the SVG
edge titles, and greys every node it did not reach, titling it "Not run in
this invocation". Separately, it lights every node whose title is the
selected event's author or whose label contains it. Every event under a net
carries the net's name as its author (child contexts inherit
`event_author`, as under ADK's `Workflow`), and the net's own events (an
error) also end their path with it. So:

- no label holds the net's name, and nothing is titled with it but a decoy.
  A round of this ADR titled the out port with it so the answer lit
  `eventOut`; it then lit for every event, a failed turn's error included,
  and read as an answer. In a label that would hold the name (the hero net
  is `race`, its transitions `Race_*`), a word joiner (U+2060) breaks it:
  drawn the same, matched by nothing. Titles stay exact (the answer is
  emitted under `Race_Commit`, below), and when one holds the name, an
  invisible node titled exactly with it takes the UI's exact match, which
  it tries before any substring;
- a `node:` transition's label names its run (`node:fast`), and no other
  label or title holds that name: `fast` lights `Race_RunBranchA`, not a
  place called `fastDone` (the optimistic net's `slowTrigger` took `slow`
  until the joiner);
- a function node mounted in a subnet runs as `<mount>·<node>`
  (`blueprint.run_name`: `second·fast`, `first·inner·leaf`), so its path
  says which mount ran it, and the collapsed mount lists its runs (`runs
  second·fast, second·medium, second·slow`). Two mounts of one blueprint
  each light up for their own runs; before, the UI's first match always
  took the first mount. Other mounted nodes (agents, nets, workflows) keep
  their own names: renaming an agent changes how its history reads;
- a net-level failure (a transition of the net raised, not a node) is
  authored by where it broke: the failing transition, or its top-level
  subnet (`assistant` for `assistant/LlmStep_OnModelError`), which is the
  collapsed node's title. A node's own failure is already on that node's
  path;
- side arcs attach through a port, so a transition's only predecessors are
  the places it consumes, and the walk from `Race_RunBranchA` reaches
  `triggerA`, `Race_Start` and `userIn`. Drawn as plain `raceWon ->
  Race_RunBranchA`, the inhibitor gave it two predecessors and the walk
  stopped there.

**The answer is emitted under the transition that answered.** A review in a
browser found the step that decided a race (the commit, its input, the arc
to `eventOut`) the one part never lit, and every join stopping the walk. A
`PetriNet` now emits its answer as an ADK `Workflow` emits its terminal
node's: as the output of a child (`use_as_output`), here a child named after
the transition that put the token on `eventOut` (`race_agent@1/
Race_CommitA@1`; inside a subnet, the subnet's top-level mount, as drawn).
The egress tap learns that transition from the `TransitionCompleted` that
follows the token, or, for a `timeout` output branch (which has no
`TransitionCompleted`), from the `ActionTimedOut` before it, telling the
branch's tokens from the next firing's by the places the branch puts to. It
names each token and the turn looks up the one it took (by identity, or an
`Event`'s id), so a partial or a token from before the turn never names the
answer. This is an event store shaping output, which design commitment 5
reserves for observability: the store decides only the answer's node path
(where the dev UI lights it), never what the answer is or when the turn
ends. The UI lights `Race_CommitA` and walks back through
`branchADone` to `userIn`. The event keeps the net's author, and its
`output_for` names the net, so a parent `Workflow` and ADK's "hide
intermediate events" treat it as the net's output. A value that is not an
`Event` (a `BranchResult`, a count) used to become the node's output at the
end of the invocation, after every loser; it is now emitted at once like an
`Event`. An answer with no content gets a text part (the value's `text`,
else JSON): the chat shows it as a message, not as one more JSON bubble
among the node outputs. A race's losers still post their outputs after it:
they are node events ADK records for any workflow, and holding the answer
until they finish would cost a race its point (the first answer). The
message bubble, and ADK's "hide intermediate events", tell them apart.

A join stopped the walk: `Race_CommitA` consumes `branchADone` and the Void
`racePermit`, the composed net's `assistant` its `question` and a
`turnAbort` nothing produces. A join's control inputs (a `Void` place, or
one nothing in the net produces) attach through a port when it also has a
data input, so the data input is its one predecessor. A data join
(`Composed_Brief` over `a1` and `a2`) still stops the walk: neither input
is the one that ran. A place nothing produces (not an environment input,
not seeded) is drawn dotted, its tooltip saying so.

The drawing itself: a xor's branches that all put a token on one place are
one unlabelled arc (`Opt_Validate`'s `pass` and `fail` both keep
`cheapPending`); top-level transitions that compete for an input with
different priorities are lined up by priority (`Race_CommitA..C` before
`Race_DiscardA..C`) by an invisible `rank=same` chain; a transition with more
than two reset arcs says how many (`resets 9 places`) and lists them,
shortened, in the label's own colour (`trigger{A,B,C}, branch{A,B,C}Done,
race{Permit,Won,Discarded}`); an arc that gives a seeded place its token
back (a permit) does not shape the layout, so the permit sits by its
consumer, not at the bottom with long back arcs; and a key under the drawing
says what its shapes mean (a token, a dotted place included), three entries
per line: a one-line key was wider than the composed net and shrank it.
Collapsed views are drawn tighter (`ranksep` 0.3). ADK's own legend (Agent,
Workflow, Function, Join, Tool) is static in the "Agent Structure" overlay
and stays: it is ADK's frontend.

**The "Agent Structure" view opens a subnet.** In a net of more than 15
places (the 41-place composed showcase drew its labels at about 6 px) a
mounted blueprint is one node. The view's own drill-down opens it: a node is
clickable when its entry in `build_graph` has a `graph`, a click pushes its
name onto the breadcrumbs, and the view renders the preloaded drawing for
that path or asks `build_graph_image?node=<path>` for `{"dotSrc": ...}`. A
path may start with the root's name (`composed_agent/first`); the route
drops it. The breadcrumbs read `composed > composed_agent > first`.

A stock subnet's drawing is compact (`to_dot(compact=True, agent=...)`):
the overlay fits a drawing to its height, and the `llm_agent` subnet's 20
places drew their labels at about 5 px. Labels drop the `LlmStep_`-style
prefixes (ids keep them), each part with two or more members is boxed
(`LlmStep`), every inhibitor, read and reset is named on its transition
instead of drawn, rows are tighter, and the model call is marked with the
agent it is configured from (`LlmCall`, `✦ summarizer`). At 974 pt tall it
renders at about 9.5 px in a 950 px window.

In the main chat, the stock `llm_agent`'s model error leads with the fix
when no Gemini key is set (`subnet.llm_step.ModelKeyMissing`; the failure's
message keeps the fix first and names the transition last, as ADK's snackbar
cuts a long message): "No Gemini API key: put GOOGLE_API_KEY=... in
agents/.env ... (The model said: No API key was provided.)
(assistant/LlmStep_OnModelError)". `ModelKeyMissing` is Python only: the
Java `LlmStep` has no counterpart (an action, not structure, so the spec
fixtures are unaffected).

### Traces

`bridge/marking_trace.py`'s `MarkingTraces` is an `EventStore` decorator
(design commitment 5). It keeps per-session token counts and the marking
after each firing, bounded by steps and by sessions, and never records token
values.

- `NetNodeBase._start_runner` asks a store for its session's link through
  `for_session(key, initial_counts)` when the store has that method. The
  executor reports no `TokenAdded` for a seed, so the link takes the seeds
  (a compiled workflow's include the `turnPermit` the runner adds).
- A `timeout` output branch fires with no `TransitionCompleted`: its tokens
  follow the transition's `ActionTimedOut`, and the trace records a
  `timed_out` step for them. A removal from an empty place (wrong seeds) is
  logged once per trace. The session recorded longest ago is evicted first.
- `PetriAgentLoader(traces=...)` puts those traces in front of every net
  node it loads, whatever store the node already had. It traces nothing by
  default: nothing in the dev UI reads a trace.

`PetriAgentLoader` also leaves out of the app list a package folder that
defines no agent: no `root_agent.yaml`, and an `__init__.py`, `agent.py`
and, for an `agent` package, `agent/*.py` that never name `root_agent` or
`app` (a helper package of `.agent.fn` refs, such as the showcase's
`yaml_composed`). A file it cannot read counts as an app: a helper listed by
mistake fails to load, an app hidden by mistake is lost. ADK lists every folder with
an `agent.py`, and picking such a helper in the UI failed with a 404. At the
top level it also lists, as ADK's flat loader does, a package whose `agent`
is itself a package (`agent/__init__.py`), which the nested listing misses.

### No page of our own

An earlier version also served `/petri`, an unlisted page with a YAML
editor beside the drawn net, steppable counterexamples and a replay of each
session's markings. It is removed. It looked unlike ADK's UI, which this ADR
sets out not to change; the CLI (`check`, `verify`) covers checking,
verifying and counterexamples, driven since 2.15 by a coding agent with the
skill above; a net's YAML is edited in an editor, as any
ADK YAML is; and its file writes were the one route that needed ADK's
upload check re-implemented. What it alone showed, a run's markings, is
left to `MarkingTraces` as a library store.

## Consequences

- No ADK code is patched. These ADK internals are load-bearing: the
  duck-typed `graph` field; `NestedAgentLoader._perform_load`,
  `_is_valid_agent_dir` and `list_agents`; and the request shape and
  client-side behaviour of the graph route (re-check list below).
- Since 2.15 the dev UI has no builder, so nets are written in an editor or
  by a coding agent with `adk-libpetri skill`, and checked with the CLI. A
  counterexample is text (`verify`'s `fires:` and `markings:`), not a
  picture in a chat reply.
- `PetriNet.nodes` is left out of serialization (`Field(exclude=True)`):
  ADK's app-info serializer read its edge items as nodes and logged `Error
  serializing nodes field` each time a net app was selected. The `graph`
  field lists what the net runs.
- A function node mounted in a subnet has its own path segment
  (`composed_agent@1/second·fast@1`, was `fast@2`), in the Events tab and
  the stored session. A mounted agent, net or workflow keeps its name, so
  two mounts running one agent still light one mount only.
- A `PetriNet`'s answer event has a child path (`race@1/Race_Commit@1`),
  `output_for` naming the net, and, for a value, a text part; code that
  found it by the path `race@1` must look at `output_for`. A value answer
  comes at once, not after the losers. A data join still stops the UI's
  walk. A net-level error event is authored by the failing transition or
  subnet, not the net, in the chat as well.
- A blueprint whose net name is a common word has word joiners in its
  drawn labels, which copy along if a user copies a label, and an invisible
  decoy node titled with it.
- The UI greys every node the walk did not reach with its own colours
  (`#424242`/`#e0e0e0`), not our palette.
- The graph panel shows structure and what ran, never a marking. Markings
  are in `verify`'s counterexample, and in a `MarkingTraces` given to the
  loader. Traces live in the server's memory and end with it; they are
  observability, and the net never reads them.
- Loser branches of a race still post their outputs in the main chat after
  the net's answer: they are node events ADK records for any workflow. A
  node that returns a `Content` output still shows as ADK's JSON tree: it is
  that node's own event.

## Re-check on an ADK bump

Add to ADR 0006's list:

- `get_fast_api_app(web=True)` still builds a `NestedAgentLoader` when given
  no loader (`PetriAgentLoader` subclasses it to list the same apps), and
  `NestedAgentLoader._is_valid_agent_dir`, `list_agents` and `_perform_load`
  still exist with the same roles;
- `graph_serialization.serialize_agent` still draws a `graph` field by duck
  type, `create_empty_state` still only iterates `graph.nodes`, and
  `_collect_toolset` still finds a net's toolsets through
  `_iter_static_children` (`tests/net/test_static_children.py`);
- `get_fast_api_app` still takes `agent_loader` and `bind_host` (a loopback
  one turns on its DNS-rebinding guard, as `adk web` passes it), and still
  registers its routes on `app.router.routes` as `APIRoute`s whose endpoints
  take the arguments we pass;
- `GET /dev/apps/{app_name}/build_graph_image` still takes `dark_mode` and
  `node`, returns `{path: {"dotSrc": ...}}`, and is fetched once per app;
  the bundle's `applyV1Highlighting` and `highlightExecutionPathInSvg` still
  match a node by `<title>` or by label text, by event author and the last
  `nodeInfo.path` segment, still read predecessors from SVG edge titles
  split on `->` (a port suffix such as `:_` stays in the name), and still
  walk only single predecessors (`calculateVisitedPath`); the "Agent
  Structure" view (`getExpandableNodes`, `Pp`, `Hv`, `nq`) still opens a
  node whose `build_graph` entry has a `graph`, renders the preloaded path
  or asks `node=<path>` for `{"dotSrc": ...}`; the UI still never calls
  `.../events/{e}/graph`, and `agent_state` still colours nothing for a
  non-`Workflow` node;
- `Context` still copies `event_author` from its parent, `NodeRunner`
  still stamps `author` from `ctx.event_author` and the path from
  `ctx.node_path` (`_enrich_event`), and node names still need only be
  identifiers (`·` included); `Context._run_node_internal` still takes
  `use_as_output`, `return_ctx`, `run_id` and `skip_run_id_validation`, and
  a child run with `use_as_output` still stamps `output_for` with its
  parent; the UI's name match (`highlightExecutionPathInSvg`'s `p`) still
  tries an exact title or text before a substring, and still shows an event
  with both content and output as a message above its JSON;
- the dev UI still has no builder (2.15 removed it). If a later ADK brings
  one back, re-read this ADR's history (git) before serving nets there: the
  old builder's canvas wrote its own YAML over a net on every save.

`tests/web` pins each claim it can without a browser. The rest needs the
browser check: open a net app's graph panel in both themes, run a turn and
watch the highlighting (the path back to `userIn`; on the composed showcase
each mount lights for its own runs, and the error lights `assistant`, not
`eventOut`), and open a collapsed mount and the stock subnet in "Agent
Structure". On the race, select the answer and see `Race_CommitA` and its
branch lit.
