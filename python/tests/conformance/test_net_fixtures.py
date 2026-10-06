"""SUB-*: every stock subnet's structure equals the Java net's (spec/fixtures/nets).

Java's ``SpecFixturesTest`` writes the fixtures; this suite and that one both
golden-check them, so a topology drift in either language fails its build.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from adk_libpetri._spec import NetSpec
from adk_libpetri.subnet import (
    llm_agent,
    llm_step,
    llm_streaming_step,
    persist_state,
    prompt_builder,
    router,
    streaming_llm_agent,
    tool_dispatch,
    transfer_router,
)

FIXTURES = Path(__file__).resolve().parents[3] / "spec" / "fixtures" / "nets"

NETS: dict[str, NetSpec] = {
    "llm-step": llm_step.DEF,
    "prompt-builder": prompt_builder.DEF,
    "router": router.DEF,
    "tool-dispatch": tool_dispatch.DEF,
    "transfer-router": transfer_router.def_(["Sales", "Support"]),
    "persist-state": persist_state.DEF,
    "llm-agent": llm_agent.DEF,
    "llm-streaming-step": llm_streaming_step.DEF,
    "streaming-llm-agent": streaming_llm_agent.DEF,
}


@pytest.mark.parametrize("name", sorted(NETS))
def test_net_matches_java_fixture(name: str) -> None:
    golden = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    actual = NETS[name].fingerprint_json()
    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                f"java/{name}.json",
                f"python/{name}.json",
            )
        )
        pytest.fail(f"{name} differs from the Java net:\n{diff}")


def test_every_fixture_has_a_python_net() -> None:
    pending: set[str] = set()
    files = {p.stem for p in FIXTURES.glob("*.json")}
    assert files - pending == set(NETS)
