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
