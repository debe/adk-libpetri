# ν-joins in YAML blueprints

Plan: ~/.claude/plans/serialized-sniffing-key-of-501cc5b7-904e-4d93-9dc7-225288be1e43.md

## Plan (tracer bullet first)

### 1. Tracer: parse + run one join (check in after this)
- [x] `blueprint.py`: add `match`, `relay`, `mint` to `_TRANSITION_KEYS`.
  - `_match(path, d, spec, places)` builds `Match`.
  - `_projection(path, place, field)`:
    - `self` needs a `str` place.
    - Otherwise a dotted field path, checked against dataclass `fields()` or
      pydantic `model_fields` with `_suggest` did-you-mean, and the field must be `str`.
    - The projection is `attrgetter`.
  - Errors, each a `BlueprintError` at `transitions.T.match.<place>`:
    - a key place that is not an `in` arc (a read gets the NU-020 hint);
    - a unit place;
    - fewer than 2 keys;
    - `relay` without `match`;
    - a relay target that is not an output of the transition.
  - Attach `match=` in `TransitionSpec(...)` at L1319.
- [x] `mint:` is parsed into `ActionPlan.mints` as `(place, field | None)`. Each target
  must be an output place, and a unit place is an error. `_plan_action`/`_node_action`
  call `ctx.fresh_name()` once per firing and write the name into each target value
  before `ctx.output`:
  - a str place: the token is the name;
  - a dataclass: `dataclasses.replace`;
  - pydantic: `model_copy(update=)`.
  Check that mounted subnets rename the mint places (`_renamed`, L2072) the way
  `rename_spec` renames match places.
- [x] Fixture `tests/net/blueprints/bp_nu_join/`, the peer's motif in small:
  - `T_Start` mints `workItem.turn` and `pending.turn`.
  - A node turns workItem into `result` (copying `turn`).
  - A deadline transition retires the pending token as a `retired` name.
  - `T_DropLate` (priority 10) joins `result` with `retired` by name and discards it.
  - The on-time path joins `result` with `pending` and emits.
- [x] Tests:
  - `ERRORS` entries in `test_blueprint_parse.py` (L251) for each refusal above;
  - a run test like `test_yaml_nets.py:57`: on-time answers, late result discarded;
  - a test that two firings mint two different names.

- [x] Review/fix rounds 1-3 (Workflow): 21, 8, 6 confirmed findings, all fixed but one
  (a test for a runtime error YAML cannot reach). Round 3 was the cap, so the rounds
  stopped without a dry one.
- [x] Mounted subnet: `mount.yaml` mounts `pair.yaml` twice; the keys are renamed and each
  instance joins its own name.
- [x] The late-reply test releases the slow answer itself after the escalation's answer
  (a `threading.Event`), instead of racing a sleep against the timer.

### 2. Proofs
- [ ] `net/proofs.py` `_run`/`verify_blueprint`: when any transition has a match or a mint,
  pass `mint_transitions`. With relays or carriers, also pass `fragment_mode="extended"`
  and `carrier_places`, and add a `NetProof.notes` line saying so.
  - Under `turn_spec`, a node transition's deposit moves to `complete:T:run`, so the
    mint name must follow it. Settle that, and whether the verifier's in-flight split
    needs `assume_atomic_firing`, against libpetri's own tests
    (`libpetri/python/tests/test_nu_verification.py:431-464`, `test_nu_mint_contract.py`).
  - Derive the carriers (mint targets, relay targets, the places names flow through by a
    one-in drain) and check the derivation against those tests. If it can't be derived
    soundly, add a `prove.options.carriers` override.
- [ ] A new claim kind `joined_or_dead_lettered: place` (`ClaimKind`, `_claim` L1923,
  `_property`). Check `report.py`/`counterexample.py` for exhaustive kind matches.
- [ ] Tests in the style of `test_prove.py:27` (`@requires_z3`):
  - the fixture proves deadlock_free and joined_or_dead_lettered(result);
  - a variant without the drop transition is `Violated`, so the proof is not vacuous.

### 3. Schema, docs, guide
- [ ] `schema.json` `$defs.transition` (L443-514): add `match`, `relay` and `mint`, plus
  `BROKEN` entries in `test_schema.py` (L80-128). The keys-parity test at L153 must stay green.
- [ ] `AUTHORING.md`:
  - `## The format`: match/relay/mint.
  - `## Actions`: what mint writes.
  - A new motif `### Late work: discard it by name` as a complete `# file:` block,
    checked and proven by `test_authoring.py` (motif list L70).
  - `## prove:`: the new claim, plus why env-borne names give `Unknown`.
- [ ] `SKILL.md:25-27`: list the motif (`test_cli.py:246` runs it through `adk-libpetri guide`).
- [ ] Regenerate `spec/api/python.txt` if `ClaimKind`/`ActionPlan` show (`PUBLICAPI_WRITE=1`) and
  review the diff.
- [ ] `CHANGELOG.md` Unreleased: add `### Python` (there is none yet; L15).

### 4. Release and reply (only on the engineer's word)
- [ ] Release 0.2.1 by the Python ritual in CLAUDE.md (dating commit, `--dry-run`, then the real run).
- [ ] `borg tell 2a436aac-2edb-45fb-a0e5-f34cf837f8cb --re mbf67cb61acfb "..."` with the
  release, plus how the design differs from the request: a mapping not pairs, inputs not
  reads, and `mint:` instead of node-side `fresh_name()`.

Out of scope: drawing match arcs in `graph.py` (a UI change needs ADR 0009's
real-browser check), and the workflow compiler's match-free over-approximation
(its names come from the environment).

## Risks
- The carrier derivation is the part most likely to be unsound. It gets a `Violated`
  control test, and an explicit override if derivation falls short.
- A node that fails to copy the name breaks the verifier's drain assumption (EXTENDED
  reads a one-in transition as name-preserving), and libpetri checks only relays. Document it.
  A runtime check is a possible follow-up.

## Verification
```bash
cd python && . .venv/bin/activate
pytest tests/net -k "nu or match or mint" && REQUIRE_Z3=1 pytest
ruff check . && ruff format --check . && pyright
adk-libpetri check tests/net/blueprints/bp_nu_join/root.yaml
adk-libpetri verify tests/net/blueprints/bp_nu_join/root.yaml
adk-libpetri guide "late work"
```
The CI `python-wheel` job (`scripts/test-python-wheel.sh`) runs again before the release.

# Dependency bump, 2026-11-27 (branch `deps/bump-2026-11`)

## Java: done
- [x] libpetri 10.0.0 -> 11.1.0, google-adk 1.13.0 -> 1.15.0, mockito 5.25.0,
      otel-sdk-testing 1.68.0, compiler 3.16.0, surefire 3.6.1,
      central-publishing 0.12.0, Maven wrapper 3.10.2
- [x] `VoiceVadEdgeAdkFoilTest`: ADK 1.15's `convertToServerResponse(msg, PendingText)`
- [x] `LlmStepSubnet`: defer `generateContent` so a synchronous throw reaches `OnModelError` (test first)
- [x] ADR 0012, CHANGELOG, README / java/README / CLAUDE.md version mentions and anchor
- [x] `./mvnw verify` (312 tests, z3, 0 skipped) + javadoc

## Tooling: done
- [x] CI `setup-java@v6`, `setup-python@v7`
- [x] docs/diagrams: libpetri 10.1, tsx 4.23, TypeScript 7 (DOT output unchanged)

## Python: google-adk ~=2.15.1, libpetri >=8.1 (decision: drop ADK's removed builder)
- [x] pyproject pins, rebuild `.venv`
- [x] compiler parity, ADK 2.14 join-in-loop: a join on a cycle keeps arrivals from
      predecessors it cannot reach again (read, not consume); test from probe
- [x] compiler parity, ADK 2.14 parallel-worker retry: `_with_attempt` must not clear the
      wrapped node's `retry_config`; test from probe
- [x] web/: remove builder_guard, canvas_view, drafts, staging, builder assistant,
      loader's `__` assistant branch, server's builder wiring, and proof_view (only the
      builder linked its pictures); keep graph_view, loader
- [x] builder assistant -> packaged Agent Skill (`SKILL.md`) over `adk-libpetri check/verify/guide/schema`
- [x] re-vendor ADK workflow samples at v2.15.1 if they changed
- [x] ADR 0006 / 0009, READMEs, CLAUDE.md, CHANGELOG; regenerate spec/api/python.txt
- [x] `REQUIRE_Z3=1 pytest`, ruff, ruff format, pyright; real-browser check of `adk-libpetri web`

## Review
- Java: `./mvnw verify` 312 tests (z3, 0 skipped), javadoc clean. No libpetri 11.x
  workaround to remove; one real defect fixed (sync-throwing `BaseLlm`).
- Python: `REQUIRE_Z3=1 pytest` 1104 passed, 1 xfailed; ruff, format, pyright clean;
  readme_diagrams, conformance and public-API goldens green. Browser check of
  `adk-libpetri web` on 2.15.1 passed (graph panel, drill-down, turn highlighting).
- New parity tests fail on the old compiler (9 of 13 in test_adk_2_14_parity.py);
  test_adk_2_15_parity.py's native side was probed: 2.13 runs the target twice, 2.15.1 once.
- Seam pins: added `_delegates_item_retry`, `Node._inner_node`; dropped the orphaned
  `NestedAgentLoader._validate_agent_name`.
- Open: `net/counterexample.py` has no caller in src since the builder went (delete,
  or add `verify --svg DIR`). ADK warns `LlmAgentConfig` deprecated from inside its own
  loader, and Starlette 1.10 suggests `httpx2` for TestClient: neither is ours to migrate.
- Not done: Java `from_workflow` / blueprints (separate project, needs its own ADR).
