"""A timeout output's token in the marking (ADR 0008's former open issue).

libpetri-py <= 7.3.0 wrote a timeout branch's token as a unit ``()`` that a
snapshot of the marking failed to decode. Fixed upstream; this pins it.
"""

from __future__ import annotations

from google.adk.agents.config_agent_utils import from_config

from adk_libpetri.net import PetriNet

from ._harness import runner_of, session, settled
from .conftest import BLUEPRINTS, Serve


async def test_a_timeout_branchs_token_reads_back_from_the_marking(serve: Serve) -> None:
    node = from_config(str(BLUEPRINTS / "bp_timeout_mark" / "root.yaml"))
    assert isinstance(node, PetriNet)
    s = await session(serve(node))
    turn = await s.say("go")
    assert turn.error is None
    snap = await settled(runner_of(node, s), lambda m: m.marking.count("timed_out") == 1)
    assert snap.marking.count("timed_out") == 1
