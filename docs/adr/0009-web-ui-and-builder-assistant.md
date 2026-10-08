# ADR 0009: Nets in ADK's web UI, and a Petri builder assistant

- **Status:** Accepted
- **Date:** 2026-10-07
- **Scope:** Python `0.1.0`, `adk_libpetri.web`, `net.graph`, `net.report`,
  `net.counterexample`, `bridge.marking_trace` (all `@experimental`).
  google-adk `~=2.11.0`.

## Context

ADR 0008 lets a person or an agent write a net in ADK's YAML and prove it
before it runs. Two gaps remained:

- `adk web` served a blueprint but showed nothing of it. Its graph view drew
  a `PetriNet` as one box, and it could not check, verify or replay a net.
- ADK's dev UI has an LLM Agent Builder Assistant that writes
  `root_agent.yaml`. It knows only AgentConfig, and its `write_config_files`
  check accepts a blueprint with any content, a broken one included:
  `PetriNet`'s keys are extra keys to that schema.

The goal: everything stays the same for an ADK dev-UI user, and nets just
work in it. They open `/dev-ui/`, chat with the builder assistant (the
pencil button), see the net drawn in ADK's own graph panel, and chat with
the net. A page of our own is not the product.

ADK's frontend is a minified Angular bundle with no extension points, and we
fork nothing (design commitment 4): no patch to ADK's Python files, no
rewrite of its compiled JavaScript. Five seams in ADK 2.11 are enough:

1. `get_fast_api_app(agent_loader=...)` takes a custom loader and returns
   the FastAPI app.
2. Starlette matches routes in order, so a route inserted at
   `app.router.routes[0]` answers before ADK's route with the same path.
   We change what the UI shows by answering the UI's own requests.
3. The dev UI's graph view draws any node's pydantic field named `graph`
   whose value has `.nodes[].name` and `.edges[].from_node/.to_node/.route`
   (`cli/utils/graph_serialization.py`). It has no class check, and
   `runners.py` (`_collect_toolset`) and `cli/utils/state.py` only iterate
   `graph.nodes`.
4. The UI lights up what ran on the client. A drawn node lights up when its
   `<title>` (the DOT node id) equals an event's author or the last segment
   of its `nodeInfo.path`, or when its label text contains that name
   (`applyV1Highlighting`, `highlightExecutionPathInSvg`). A net's `node:`
   runs have paths like `race@1/fast@1`.
5. The builder assistant is `AgentBuilderAssistant.create_agent()`, loaded
   under the reserved name `__adk_agent_builder_assistant` by
   `AgentLoader._perform_load`. Its file tools resolve paths against the
   session's `root_directory` (the UI sets `<app>/tmp/<app>`), relative to
   the server's working directory.

Spikes in a browser found what the UI cannot do:

- `EventActions.agent_state` does **not** colour the graph for a
  non-`Workflow` node; the UI uses it only for its "Agent State" button.
  We emit no extra events, and a turn's event stream is unchanged.
- The UI fetches `build_graph_image` once per app and never calls the
  per-event graph route (`.../events/{e}/graph`). The graph panel therefore
  cannot show a marking; highlighting what ran is what it shows.
- The builder canvas understands only ADK's agent kinds, and it writes its
  own YAML for the root before every message to the assistant (below).

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
loader, plus three routes inserted ahead of ADK's (seam 2) and one new route
for counterexample pictures. Each of the three answers only for a net and
calls ADK's own handler directly for everything else. `PetriAgentLoader` is
ADK's `NestedAgentLoader`, the one stock `adk web` builds, so apps in
sub-folders (`group/app`, listed as `group.app`) are found as there. `serve()`
prints the dev UI's URL and nothing else.

| What the user does in ADK's UI | Request | Our answer |
| --- | --- | --- |
| Opens a net app (Info panel, "Agent Structure") | `GET /dev/apps/{app}/build_graph_image?dark_mode=` | the net as Petri DOT, and each subnet's |
| Opens a subnet in "Agent Structure" | `GET /dev/apps/{app}/build_graph_image?node=<path>` | that subnet's DOT |
| Opens the builder (pencil) | `GET /dev/apps/{app}/builder[?tmp=true]` | a card: what the net runs |
| Sends a message to the assistant, or clicks Save | `POST /dev/apps/{app}/builder/save[?tmp=true]` | keeps the net |
| Reads a violated claim in the assistant's reply | `GET /dev/petri/counterexamples/{id}.svg` | the counterexample as a picture |

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

**The builder canvas cannot write over a net**
(`builder_guard.install_builder_guard`). ADK's canvas turns its own model of
the root back into YAML (`generateYamlFile`: `name`, `model`,
`agent_class`, `description`, `instruction`, `sub_agents`, `tools`,
callbacks) and POSTs it to `builder/save?tmp=true` before every message to
the assistant, and again (tmp, then real) on Save. For a net root that YAML
has no net in it. ADK either writes it over the net or, when it kept
`agent_class: adk_libpetri.net.PetriNet`, rejects the save with 400: its
upload check (`_check_uploaded_yaml`) allows only code references inside the
app. In ADK alone, Save failed for every net.

The guard takes an uploaded file whose current version is a net (the
draft's when one exists, else the app's) and whose upload carries no net of
its own (no `places`, `transitions`, `nodes`, `env`, `ports`, `subnets`,
`prove`, nor a `PetriWorkflow`'s `edges`, `back_edge_budget`, `state`,
`multi_route`, `max_concurrency` or `interruptible`). A net is a root whose
`agent_class` is an `adk_libpetri.` class, `PetriNet` or `PetriWorkflow`.
A file that does not parse but whose `agent_class` line names such a class
(a half-finished edit) is a net too: the guard fails closed, where it once
let the canvas YAML replace it. It does not write the upload and takes
nothing from it. For a net root the canvas offers no field to edit (name and type are
disabled, no description), so its YAML only echoes what it last loaded, and
in a browser that echo was stale: Save sent a description read before the
assistant changed the draft. Sub-agents, tools or callbacks added to a net
root are dropped, with a warning in the log, and so are the agent files the
canvas writes for them (the files the dropped root names by `config_path`,
recursively). What was dropped is kept as a note per app (`take_note`): the
UI shows only "Something went wrong" when a save fails and nothing at all
when one drops an edit, so the builder assistant says it in its next reply.
A real save (the canvas's Save) that would drop such an addition writes
nothing and answers false: the builder stays open with ADK's generic error
instead of closing as if the sub-agent were saved, and the note says to
delete it on the canvas (any message also reloads the canvas from the
draft). Echoes of fields the canvas writes for any `LlmAgent` (`model`,
`instruction`) do not stop a save. A tool renamed on the canvas cannot be
seen (the canvas sends no `tools` for a net root), so every note says
renames are not saved. ADK's handler does everything else:

- files that are not nets, and apps without one, pass through unchanged;
- an upload that carries a net still meets ADK's upload checks and their
  400, so the guard does not widen what an upload may reference;
- the draft (`<app>/tmp/<app>`), where `write_petri_blueprints` writes,
  survives the save before each message. Save runs ADK's copy of the draft
  into the app, so it ships the assistant's latest net. After every real
  save that succeeded, a net or not, the guard drops the app from the
  loader's cache and from ADK's runner cache: a net saved as an `LlmAgent`
  left the old net in the graph panel and in chat until a restart.

**The draft is kept in step with the app** (`web/drafts`). ADK makes the
draft once, when the builder first opens on an app, and never refreshes or
drops it: a draft left by a closed tab or an earlier server run is reused,
and on Save ADK copies every draft file over the app. Before the guard, Save
on a net always failed (the 400 above), before anything was copied; the
guard is what made the copy run for nets, and with it a file edited in an
editor while the builder was open went back to the
draft's older copy (YAML and `agent.py` alike). So:

- we make every draft ourselves (`make_draft`, from our `GET builder?tmp`
  route and the guarded tmp save, as ADK makes it) with a baseline: each
  copied file's digest, in `.adk_libpetri_draft.json` inside the draft. It
  goes when ADK deletes the draft after Save, and the guard takes it out
  before ADK copies the draft into the app;
- `reconcile` compares each draft file with the app's and the baseline. A
  file only the app changed is refreshed in the draft; one only the draft
  changed (the assistant's work) ships; one both changed stops a real save
  (the canvas says "Something went wrong", the assistant names the files
  and how to resolve it). A draft with no baseline falls back to file times:
  an app file newer than its draft copy refreshes it, unless the draft copy
  was written in the draft (ADK's copy keeps the app's older modification
  time, so a copied file's status changed after its content, a written one
  both at once). Then neither side's work can be told newer, and it is a
  conflict;
- it runs when the canvas loads the draft, on each tmp and real save of an
  app whose root (or its draft's) is a net.
  Apps without a net keep ADK's behaviour.

**"Create new app" cannot write into a helper package.** The loader hides a
package folder that defines no agent (see Traces), so the UI's new-app
dialog lets its name through. The canvas route answers such a name as for
an app that does not exist (no draft is made inside it), and the guard saves
nothing into it (the canvas's Save then says "Something went wrong"; the
assistant says why).

ADK keeps one `Runner` per app (`runner_dict`) and evicts it only for an app
in `runners_to_clean`, which its file watcher fills under `reload_agents`
(off by default). Without the eviction, chat ran the old net after Save
while the graph panel drew the new one. `get_fast_api_app` does not return
the `AdkWebServer`; `server.adk_web_server` finds it in the closures of its
route handlers.

Before a tmp save the guard creates the draft as a copy of the app, as ADK's
`GET builder?tmp=true` does. Otherwise ADK would create a draft holding only
the uploaded YAML, without the net's `agent.py`.

The guard and the canvas route resolve app names against the folder ADK's
builder routes use (`builder_guard.agents_base`): in single-agent mode
(`adk-libpetri web <one app's folder>`) that is the folder's parent. Against
the argument itself, every app root missed, the guard passed every save to
ADK and the canvas showed the raw YAML: Save wrote the canvas's `LlmAgent`
over the net.

**The builder canvas shows what a net runs** (`canvas_view.install_canvas_view`).
The canvas loads its root from `GET builder` (with `?tmp=true` for the
draft, again after every assistant reply: `reloadCanvasFromYaml`) and reads
only the fields `generateYamlFile` writes. A net root showed as one empty
box. Our route answers for a `root_agent.yaml` that is a net (as the guard
defines it; a `PetriWorkflow` is described as compiled from its edges) with
a card: its `name` and `agent_class`, a `description` with the net's size
and last verdicts, and as `tools` the functions and agents the net runs,
each once: a function by its ref (`race.agent.fast`), an agent by the YAML
file the net loads it from (`yaml_composed/helper.yaml`), else
`<Class>.<name>`. The route takes `file_path` as the root file only when it
normalizes to `root_agent.yaml` (`../root_agent.yaml` is ADK's). The canvas draws them inside the root
box, so after each reply the box lists what the latest draft runs. Other
roots and files go to ADK, still as plain text.

We tried more in a browser and chose against it:

- The canvas shows no description for a root that is not an `LlmAgent`, so
  the size and verdicts are not visible there. The assistant states them in
  its replies, and the graph panel draws the net after Save.
- As `LlmAgent`, the root would offer a model and an instruction to edit,
  and nothing of them would reach the net.
- Sub-agents draw as separate boxes, each an editable `LlmAgent` form, and
  the canvas writes a `<name>.yaml` for each on every save.
- A tool name without a dot opens ADK's built-in tool picker.

So every tool name has a dot: the canvas treats it as a function tool, whose
dialog shows only the name. Edits there are never saved: the canvas sends no
`tools` for a root that is not an `LlmAgent`, and the guard keeps the net in
any case. The next reload shows the card again.

The draft has its app's package name. Importing it under that name raced
ADK's loader for `sys.modules`: in a browser test, Save left a half-made
`race.agent` behind and the app stopped loading. Loaded in place (with
`<app>/tmp` on `sys.path`), it went wrong both ways: checked first, the
draft's `agent.py` became `<app>.agent`, which ADK's loader then served as
the app; checked after the app had loaded, the draft's refs resolved
against the app's stale code, so a function only the draft had did not
load. Every load of a draft (the card, and the assistant's write, check and
verify) therefore goes through `web/staging.staged`: a copy of the folder
under a package name nothing else imports (`_petri_stage_<hex>`), whose
absolute refs to the app's own package are rewritten to the copy's, whose
modules leave `sys.modules` afterwards, and whose paths and package name are
mapped back in anything shown to the user. The card loads the app's own
file directly.

**A counterexample is a picture in the assistant's reply**
(`proof_view.install_proof_view`, `net.counterexample`). ADK's chat panels,
the builder's included, render a reply through the shared `app-markdown`
component (ngx-markdown and Angular's HTML sanitizer). An `<img>` from the
same server shows inline, and `width` and an `<a target>` around it survive
the sanitizer; a raw `<svg>` or `<iframe>` does not. ADK's CSS puts no width
limit on such an image. The Artifacts tab shows the main app's session, not
the builder's, so it cannot carry the picture.

`verify_petri_blueprint` gives each violated claim:

- `steps`: one line per firing with what it changed (`3. Race_Commit
  starts: -done, +Race_Commit in flight`), the last with the whole marking
  and what breaks. They replace the raw `fires` and `markings` in the tool
  result: on the 41-place showcase net, 13 lines of about 1.4 kB;
- `picture`: `<a href=... target="_blank"><img src=... width="100%"></a>`,
  which the instruction tells the assistant to copy as given, outside a
  code block, and never to write an image of its own. In the panel (at
  most 480 px) it shows the claim and its steps; a whole net shrank there
  to about 3 px text, so the net is drawn only in the full-size view
  (`?full=1`) a click opens in a new tab, which a line in the picture
  says.

The route serves `GET /dev/petri/counterexamples/{id}.svg` (`?step=`,
`?full=1`) under the app's `root_path`, so ADK's `url_prefix` holds. The
server keeps the last 64 violated claims with their nets' graphs, under an id
derived from the counterexample and the graph, served `no-cache`; an id it no
longer holds answers 404 with a picture saying to verify again. The
picture links carry the `root_path` of the last app `install_proof_view`
ran for: one process serving two apps under different `root_path`s names
the first one's pictures wrongly. Under stock
`adk web` nothing serves the route, and the claim carries `steps` only.

`counterexample_svg` draws one self-contained SVG: a header, the net at the
bad step (`to_dot` with the marking, the transition that fired and the
offending places in red), then a filmstrip of what each step changed. It
carries both of ADK's palettes and picks one with `prefers-color-scheme`;
inside an `<img>` that follows the page's `color-scheme`, which ADK's theme
toggle sets. The net drawing runs a Graphviz `dot` binary
(`ADK_LIBPETRI_DOT`, else `PATH`) with a timeout. Without one, the picture
is the header and the filmstrip, with a line saying the net is not drawn.

### The builder assistant

`create_petri_builder_assistant()` keeps ADK's assistant: its model, tools
and instruction. It adds an instruction addendum and five tools:

- `petri_authoring_guide(section)` and `petri_schema()`;
- `write_petri_blueprints`, which writes nothing unless every blueprint
  among the files loads, checked in a staged copy (above);
- `check_petri_blueprint`, on a staged copy;
- `verify_petri_blueprint`, on a staged copy, which returns `steps` and
  `picture` as above.

The three last tools return a `summary` line: the net's size, or each
claim's verdict ("1 of 2 claims proven; violated: ..."). The addendum says
when a net beats ADK's workflow agents and states the loop: write, check,
verify, fix from the counterexample; a net is not done while a claim is
violated or unknown. Every reply that wrote or verified a net ends with the
net's size and each claim's verdict as the tool gave it ("proven" only when
the tool proved it) and, once all are proven, "Click Save to apply it; the
net then shows in the graph panel." The server keeps the last verdicts per
net (`remember_verdicts`, at most 64), keyed by `net_digest`: the root
file's name and every `.yaml`, `.yml` and `.py` file under its folder
(mounted blueprints, helper YAML, `agent.py`), taken before the verify ran.
Keyed by the root's text alone, the greeting said "4 of 4 claims proven"
and "Click Save" for a draft whose mounted race the assistant had just
broken. The canvas card and the greeting show them while those files are
unchanged; one verify runs per digest at a time. Refs into other packages
of the agents folder are not followed.

Some replies need no model, and get none (a `before_model_callback`):

- When the builder opens, the panel sends `hello` by itself. On a net app
  the first such message is answered from the draft (else the app's file),
  after proving its claims, which needs z3 and no model: "This is
  `race_agent` (app `yaml_race`), a Petri net: 11 places, 10 transitions;
  4 of 4 claims proven.", each claim's verdict, each violated claim's last
  steps and picture, that `verify` and `check` work any time, and that the
  canvas is read-only for a net. "Click Save to apply the draft" only when
  the draft differs from the app and every claim is proven; else it says to
  prove them (or to fix a draft that does not load) first. Replies name the
  net and its app once (`race_agent` (app `yaml_race`)), or one name when
  they are the same. The greeting waits
  at most 8 s for the proof (the 41-place showcase takes about 11 s); a
  longer one goes on in the background, its verdicts are remembered, and
  the greeting says to send `verify`.
- A bare `verify` or `check` (any case, with or without `/` or a full stop)
  on a net app is answered without the model: how many claims are proven,
  each claim as a list item (a violated one with the step where it breaks
  and why), each violated claim's last three steps and its picture, and a
  line saying a click opens the picture full size; or whether the net
  loads. Without a key this is how a user gets
  a verdict at all; before, "4 claims, not verified yet" was a dead end.
- With no Gemini key (no `GOOGLE_API_KEY` or `GEMINI_API_KEY`, not Vertex)
  for a Gemini model, every reply says where to put one, and on a net app
  that `check` and `verify` still work. ADK's panel showed only "Error
  Code: ValueError" (it prints the error code, never the message), and that
  on opening, before the user typed anything. On an app that is not a net
  it says chat with a Gemini agent needs the key too. In the main chat the
  stock `llm_agent`'s model error leads with the same fix
  (`subnet.llm_step.ModelKeyMissing`; the failure's message keeps the fix
  first and names the transition last, as ADK's snackbar cuts a long
  message): "No Gemini API key: put GOOGLE_API_KEY=... in agents/.env ...
  (The model said: No API key was provided.)
  (assistant/LlmStep_OnModelError)". `ModelKeyMissing` is Python only: the
  Java `LlmStep` has no counterpart (an action, not structure, so the spec
  fixtures are unaffected).
- What the canvas's last save dropped from a net leads the next reply (with
  the model, an `after_model_callback` puts it in front of the first text).

`PetriAgentLoader` serves this assistant under
`__adk_agent_builder_assistant`, so the UI's own pencil button uses it.
Under stock `adk web` it runs as an ordinary app (`root_agent =
create_petri_builder_assistant()`). `serve()` works from the agents folder,
because the assistant's tools resolve `root_directory` against the working
directory.

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
sets out not to change; the builder assistant covers checking, verifying
and showing counterexamples; a net's YAML is edited in an editor, as any
ADK YAML is; and its file writes were the one route that needed ADK's
upload check re-implemented. What it alone showed, a run's markings, is
left to `MarkingTraces` as a library store.

## Consequences

- No ADK code is patched. These ADK internals are load-bearing: the
  duck-typed `graph` field; `AgentLoader._perform_load` and
  `_validate_agent_name`; `google.adk.cli.built_in_agents`
  (`AgentBuilderAssistant`, `resolve_file_path`); the dev UI's
  `root_directory` convention; and the request shapes and client-side
  behaviours of three dev-UI routes (re-check list below). If the assistant
  module moves, `create_petri_builder_assistant` falls back to a plain
  `LlmAgent` with the Petri tools.
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
  are in the counterexample picture, and in a `MarkingTraces` given to the
  loader.
- The canvas card lists what a net runs, not its places, transitions or
  claims, and its tool dialogs look editable although nothing in them is
  saved. Verdicts follow the files under the net's folder; a change in
  another package it refers to (`yaml_composed.agent.brief`) is not seen
  until the next verify.
- A full blueprint cannot be uploaded through `builder/save` (ADK's 400
  stands). Nets are edited by the assistant or in an editor.
- A staged copy rewrites the draft's absolute refs to its own package in
  YAML files only; a draft `agent.py` that imports its app's package by
  name (`from race import x`) still reaches the app's code. Relative
  imports are fine.
- A counterexample shows only when the model copies the `picture` line as
  given. A model that rewrites it, or puts it in a code block, shows text;
  the `steps` lines are there either way. No live model was available to
  test the wording end to end; a scripted stand-in for the assistant,
  calling the real tool through ADK's `run_sse`, showed the picture in the
  builder panel.
- The inline picture has no net drawing: the new tab has it, unscaled.
  Pictures, verdicts and traces live in the server's
  memory and end with it. Traces are observability; the net never reads
  them.
- In a headless browser the builder panel can look stuck on "..." after a
  reply; any mouse event renders it (Angular change detection). Real use
  does not see it.
- A file edited in both the app and the builder's draft stops Save until
  one side is dropped; the user resolves it with the assistant or by
  deleting the draft. A draft made before this baseline existed resolves by
  file times, and a file written in it that the app also changed is a
  conflict.
- Save on a net root with a sub-agent added on the canvas answers false
  ("Something went wrong") until the sub-agent is deleted there or the
  canvas reloads; the reason comes in the assistant's next reply.
- Loser branches of a race still post their outputs in the main chat after
  the net's answer: they are node events ADK records for any workflow. The
  canvas card's left panel stays ADK's (name and type, read-only), and its
  tool dialogs still accept edits that are not saved; the assistant's
  greeting says so. Both are frontend-only. A node that returns a `Content`
  output still shows as ADK's JSON tree: it is that node's own event.

## Re-check on an ADK bump

Add to ADR 0006's list:

- these private ADK helpers are re-implemented, not called, and must still
  match: `_get_app_root` and `_parse_upload_filename` (app and upload-name
  checks, `builder_guard`), the draft copy `GET builder?tmp=true` makes
  (`drafts.make_draft`: `copy_dir_contents`, `copy2`, `tmp` left out),
  `NestedAgentLoader._is_valid_agent_dir` and `list_agents`, and
  `AdkWebServer.runner_dict`/`runners_to_clean` with `get_runner_async`'s
  eviction (`server._forgetter`, found through the route handlers'
  closures; `build_app` logs a warning when it is not found);
- `get_fast_api_app(web=True)` still builds a `NestedAgentLoader` when given
  no loader (`PetriAgentLoader` subclasses it to list the same apps);
- `graph_serialization.serialize_agent` still draws a `graph` field by duck
  type, and `_collect_toolset` and `create_empty_state` still only iterate
  `graph.nodes`;
- `get_fast_api_app` still takes `agent_loader` and still registers its
  routes on `app.router.routes` as `APIRoute`s whose endpoints take the
  arguments we pass; the builder's app name is still
  `__adk_agent_builder_assistant` and its `root_directory` still
  `<app>/tmp/<app>`; `AgentBuilderAssistant.create_agent` and
  `built_in_agents.utils.resolve_root_directory.resolve_file_path` still
  exist;
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
- `POST /dev/apps/{app_name}/builder/save` still takes `files` and `tmp`,
  validates uploads the same way (`_check_uploaded_yaml`), copies every file
  of `<app>/tmp/<app>` into the app on a real save and then deletes the
  draft, and the UI still never cancels or refreshes a draft when it enters
  builder mode (`loadExistingAgentConfiguration`); the canvas
  (`generateYamlFile`) still sends only the fields listed above, still
  saves the draft before each assistant message, and still drops `tools`
  for a root that is not an `LlmAgent`;
- `GET /dev/apps/{app_name}/builder` still takes `file_path` and `tmp` and
  answers plain text; the canvas (`loadFromYaml`, `reloadCanvasFromYaml`
  after each reply) still reads the root's `tools` and draws them in the
  root box, treats a dotted name without `args` as a function tool, and
  shows no description for a root that is not an `LlmAgent`;
- the builder panel still sends `hello` with `root_directory` in its state
  delta when it opens, and still shows only an error's code;
- `Context` still copies `event_author` from its parent, `NodeRunner`
  still stamps `author` from `ctx.event_author` and the path from
  `ctx.node_path` (`_enrich_event`), and node names still need only be
  identifiers (`·` included); `Context._run_node_internal` still takes
  `use_as_output`, `return_ctx`, `run_id` and `skip_run_id_validation`, and
  a child run with `use_as_output` still stamps `output_for` with its
  parent; the UI's name match (`highlightExecutionPathInSvg`'s `p`) still
  tries an exact title or text before a substring, and still shows an event
  with both content and output as a message above its JSON;
- `get_fast_api_app` still serves a single-agent folder's builder routes
  from its parent (`is_single_agent_directory`);
- the new-app dialog still checks the name against `list-apps` only;
- the builder panel (`app-builder-assistant`) still renders replies with
  the shared `app-markdown` component (ngx-markdown, Angular's sanitizer,
  not `disableSanitizer`), an `<img>` with `width` and an `<a target>`
  still survive it, and the theme toggle still sets `color-scheme` on
  `<html>`.

`tests/web` pins each claim it can without a browser. The rest needs the
browser check: open a net app's graph panel in both themes, run a turn and
watch the highlighting (the path back to `userIn`; on the composed showcase
each mount lights for its own runs, and the error lights `assistant`, not
`eventOut`), open a collapsed mount and the stock subnet in "Agent
Structure", open the builder and read its greeting (verdicts, no "Click
Save" on an unchanged draft), send `verify`, edit the app's `agent.py` in
an editor, then Save, and see both the net and the edit kept. On the race,
select the answer and see `Race_CommitA` and its branch lit; add a sub-agent
on the canvas, Save, and see the builder stay open.
