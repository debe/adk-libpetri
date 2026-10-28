"""A timeout output's token in the marking (ADR 0008's open issue).

libpetri-py writes a timeout branch's token as a unit ``()``, and a snapshot of
the marking fails to decode it. Strict xfail: it passes once libpetri fixes it,
and then the ADR's open issue and AUTHORING.md's advice can go.
"""

from __future__ import annotations

import pytest
from google.adk.agents.config_agent_utils import from_config

from ._harness import runner_of, session, settled
from .conftest import BLUEPRINTS


@pytest.mark.xfail(
    strict=True,
    reason="libpetri-py <=7.3.0: 'Expected python.object token, found ()' on snapshot",
)
async def test_a_timeout_branchs_token_reads_back_from_the_marking() -> None:
    node = from_config(str(BLUEPRINTS / "bp_timeout_mark" / "root.yaml"))
    s = await session(node)
    turn = await s.say("go")
    assert turn.error is None
    snap = await settled(runner_of(node, s), lambda m: m.marking.count("timed_out") == 1)
    assert snap.marking.count("timed_out") == 1
