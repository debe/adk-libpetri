---
name: adk-libpetri-blueprint
description: "Write, fix and prove a Petri-net blueprint for adk-libpetri, an ADK YAML agent config with `agent_class: adk_libpetri.net.PetriNet` (or a compiled workflow, `adk_libpetri.workflow.PetriWorkflow`). Use when asked to model an ADK agent flow as a Petri net, to write or edit a `root_agent.yaml` that is a PetriNet, or to fix one that fails `adk-libpetri check` or `adk-libpetri verify`."
compatibility: "Needs the adk-libpetri Python package (its `adk-libpetri` command) installed with google-adk. `adk-libpetri verify` needs the `z3` binary (on PATH or in LIBPETRI_Z3) for claims that state-space enumeration cannot close."
---

# Petri-net blueprints for adk-libpetri

A blueprint is an ADK agent config whose `agent_class` is
`adk_libpetri.net.PetriNet`. `adk web` and `adk run` load it like any
`root_agent.yaml`. Its `prove:` section states properties that Z3 proves
before the net runs.

## When a net is the right tool

Write a net when the flow needs what ADK's workflow agents cannot state: a
race where only the first result counts, a quorum (answer at k of n), a
permit or a budget, an inhibitor fallback, a timeout, or work joined back by
name so that a late reply is dropped, never mistaken for another turn's. For
a plain sequence, loop or fan-out, keep ADK's own agents.

## Read first

- `adk-libpetri guide "the loop"`, then `adk-libpetri guide "the format"`
  and `adk-libpetri guide "the turn"`.
- Start from the closest motif: `adk-libpetri guide "permit race"`,
  `"quorum"`, `"budget"`, `"optimistic commit"`, `"timed escalation"`,
  `"late work"`, `"ports and subnets"`, `"stock subnets"`. Each is a
  complete, proven file.
- `adk-libpetri guide "prove:"` for the claims, and
  `adk-libpetri guide "reading a counterexample"` before fixing one.
- `adk-libpetri schema` prints the exact YAML shape (JSON Schema).
- `adk-libpetri guide` with no section prints the whole guide.

Do not guess the format from memory: read the section, then write.

## The loop

Writing a net is a loop. A net is not done while any claim is violated or
unknown.

1. Write the YAML (and the `agent.py` functions its `.agent.*` refs name).
2. Run `adk-libpetri check FILE`. It prints `OK ...` or one
   `ERROR <file>: <key path>: <message>. Fix: <hint>` line. Fix the key the
   path names and run it again until it prints `OK`.
3. Put the properties the net must have under `prove:`: at least
   `deadlock_free`, and `place_bound: {place: eventOut, bound: 1}` for one
   answer per turn. Run `adk-libpetri verify FILE`. It prints one `PROVEN`,
   `VIOLATED` or `UNKNOWN` line per claim. Exit code 0 means every claim is
   proven. Add `--recursive` for a net with mounted child blueprints.
4. A violated claim prints its counterexample: the transitions that fire
   (`fires:`) and the marking after each step (`markings:`). Find the step
   where the bad marking first appears; that transition's arcs are the bug.
   Fix the net, or ask the user when the claim itself may be wrong, then run
   `verify` again.
5. An unknown claim is not a proof. Read the `note:` lines under its verdict
   first: on a net with `match:` they say where a name is lost (a transition,
   or an `env:` place), and for a missing `mint:` or `relay:` they suggest a
   line to add; run `check` after adding it. Otherwise the usual cause is a
   state space too large to close: bound the environment (`prove: options:
   environment:`) or prove a smaller net. Check that `z3` is installed.

## Report

End every reply in which you wrote or verified a net with its state, taken
from the command output, not from memory:

- the net: its name and size (the `check` line);
- each claim and its verdict exactly as `verify` printed it. Say proven only
  for a claim `verify` proved. For a violated one, quote the two or three
  steps that matter and say which transition you will change.

## Conventions

- Transition names are `<Name>_<Verb>` (`Race_Commit`).
- Never name a key `args`, at any depth: `adk web` refuses the file.
- A budget or permit is a place with seed tokens, not a counter in a node.
- The turn's input is on `userIn`; the first token on `eventOut` is the
  answer.

To see the net drawn, run `adk-libpetri web <agents dir>` and open the app
in ADK's dev UI: its graph panel draws the net and lights what ran.
