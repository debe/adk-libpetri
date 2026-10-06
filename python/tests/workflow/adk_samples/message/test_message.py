"""ADK sample ``workflows/message``: string, multimodal, multiple and streamed messages.

Recorded trace ``tests/go.json`` (user says ``go``): eight non-partial
messages from four chained nodes, the image as inline PNG data, and the
streamed sentence once assembled. The streamed chunks are ``partial`` and not
stored in the session. No model; under pytest the sample skips its sleeps.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from google.adk.workflow import Workflow

from adk_libpetri.workflow import compile_workflow, verify_workflow
from support.smt_proofs import requires_z3

from .._harness import Run, run, run_both
from . import agent

SENTENCE = (
    "This is a streaming message sent in chunks.\n\n"
    "You can stream in markdown as well. For example, the table below:\n\n"
    "| Header 1 | Header 2 |\n"
    "|----------|----------|\n"
    "| Cell 1   | Cell 2   |\n"
    "| Cell 3   | Cell 4   |\n"
)
TEXTS = [
    "#1 This is a simple string message.",
    "#2 Here is a multi-modal message with an inline image (red circle):",
    "#3 Multiple messages",
    "Processing step 1...",
    "Processing step 2...",
    "Done processing.",
    "#4 Starting to stream...",
    SENTENCE,
]
NODES = ["send_string", "send_multimodal", "multiple_messages", "stream_sentence"]


def make() -> Workflow:
    return importlib.reload(agent).root_agent


def shape(r: Run) -> list[tuple[Any, ...]]:
    """Every streamed event: author, path, partial, and each part's text or mime type."""
    out = []
    for e in r.events:
        parts = e.content.parts if e.content and e.content.parts else []
        out.append(
            (
                e.author,
                e.node_info.path if e.node_info else None,
                bool(e.partial),
                tuple(p.text if p.text is not None else p.inline_data.mime_type for p in parts),
                e.output,
            )
        )
    return out


def image(r: Run) -> bytes | None:
    for e in r.events:
        for p in e.content.parts if e.content and e.content.parts else []:
            if p.inline_data is not None:
                return p.inline_data.data
    return None


async def test_native_run_reproduces_the_recorded_trace() -> None:
    r = await run(make(), ["go"])
    assert r.texts == TEXTS
    assert r.authors == ["message"]
    assert r.final_output is None
    paths = [e.node_info.path for e in r.events if not e.partial]
    assert (
        paths
        == [f"message@1/{n}@1" for n in [NODES[0], NODES[1], *[NODES[2]] * 4]]
        + ["message@1/stream_sentence@1"] * 2
    )
    chunks = "".join(e.content.parts[0].text for e in r.events if e.partial)
    assert chunks == SENTENCE
    data = image(r)
    assert data is not None and data.startswith(b"\x89PNG")


async def test_compiled_run_matches_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    native, petri, _ = await run_both(make, ["go"], orchestrator)
    assert petri.final_output is native.final_output is None
    assert petri.authors == native.authors
    assert petri.texts == native.texts == TEXTS
    assert image(petri) == image(native)
    # Same events in the same order, partial chunks included.
    assert shape(petri) == shape(native)
    assert petri.state == native.state


async def test_second_turn_restarts_node_run_ids_like_native(orchestrator) -> None:  # type: ignore[no-untyped-def]
    """Node run ids are per workflow run: a second turn's nodes are ``@1``
    again under the new workflow run, natively and compiled."""
    native, petri, _ = await run_both(make, ["go", "again"], orchestrator)
    assert shape(petri) == shape(native)
    second = [e.node_info.path for e in petri.turns[1] if e.node_info and not e.partial]
    assert second and all(p.endswith("@1") for p in second)


def test_report() -> None:
    cw = compile_workflow(make())
    assert not cw.report.rejected
    exact = {f.subject for f in cw.report.of("exact")}
    assert set(NODES) <= exact
    assert cw.node_names == NODES
    names = set(cw.spec.transition_names)
    assert {f"Wf_{n}_Run" for n in NODES} | {"Wf_EndTurnOutput_stream_sentence"} <= names


@requires_z3
@pytest.mark.timeout(300)
def test_every_safety_claim_and_deadlock_freedom_is_proven() -> None:
    proofs = verify_workflow(compile_workflow(make()), k=1)
    verdicts = {p.label: p.result.verdict for p in proofs}
    assert {p.kind for p in proofs} == {"safety", "deadlock"}
    assert "deadlock_free" in verdicts
    assert (
        "stream_sentence keeps one output: place_bound(stream_sentence/terminalOutput, 1)"
        in verdicts
    )
    assert len(verdicts) == 3 + len(NODES) + 1
    assert all(v == "proven" for v in verdicts.values()), verdicts
