"""ADK sample ``workflows/auth_oauth``: a FunctionNode gated by GitHub OAuth2.

``list_github_repos`` carries ``auth_config`` (and ``rerun_on_resume=True``).
Its first run finds no credential in session state and yields an
``adk_request_credential`` call whose id is ``wf_auth:<node_path>``; the run
ends interrupted. The next turn answers with the auth config, the access token
filled in; ADK hands it to the node as ``ctx.resume_inputs[wf_auth:<node_path>]``,
the node stores the credential, calls the GitHub API and returns the repo
names, which ``display_result`` turns into a message.

The sample has no recorded trace. No network: the user's answer carries a ready
access token (so ADK does no token exchange) and ``requests.get`` is
monkeypatched to return two repos.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from google.adk.workflow import Workflow

from adk_libpetri.workflow import PetriWorkflow, compile_workflow, verify_workflow
from support.smt_proofs import requires_z3

from .._harness import Run, function_response, run, run_both
from . import agent

NODE = "list_github_repos"
INTERRUPT = "wf_auth:auth_oauth@1/list_github_repos@1"
REPOS = {"status": "Success", "repos": ["alpha", "beta"]}
MESSAGE = "Successfully fetched repositories: alpha, beta"
TOKEN = "tok-123"


class _Response:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> list[dict[str, str]]:
        return [{"name": "alpha"}, {"name": "beta"}]


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_get(url: str, headers: dict[str, str] | None = None, **_: Any) -> _Response:
        calls.append((url, dict(headers or {})))
        return _Response()

    monkeypatch.setattr(agent.requests, "get", fake_get)
    return calls


def make() -> Workflow:
    return importlib.reload(agent).root_agent


def credential_requests(events: list[Any]) -> list[Any]:
    return [
        p.function_call
        for e in events
        for p in (e.content.parts if e.content and e.content.parts else [])
        if p.function_call and p.function_call.name == "adk_request_credential"
    ]


def answer(events: list[Any]) -> Any:
    """The user completes the OAuth flow: return the requested auth config
    (with its OAuth ``state``) and an access token."""
    call = credential_requests(events)[-1]
    cfg = json.loads(json.dumps(call.args["authConfig"]))
    cfg.setdefault("exchangedAuthCredential", {}).setdefault("oauth2", {})["accessToken"] = TOKEN
    return function_response(call.id, call.name, cfg)


TURNS = ["start", answer]


def interrupt_ids(events: list[Any]) -> set[str]:
    return {i for e in events for i in (e.long_running_tool_ids or ())}


def outputs(r: Run) -> list[tuple[str, Any]]:
    return [(e.node_info.path, e.output) for e in r.events if e.output is not None]


async def test_native_run_completes_the_oauth_round_trip(github) -> None:  # type: ignore[no-untyped-def]
    r = await run(make(), TURNS)
    first, second = r.turns
    assert [c.id for c in credential_requests(first)] == [INTERRUPT]
    assert interrupt_ids(first) == {INTERRUPT}
    assert r.final_output == REPOS
    assert r.texts == [MESSAGE]
    assert r.authors == ["auth_oauth"]
    assert outputs(r) == [("auth_oauth@1/list_github_repos@1", REPOS)]
    assert [e.node_info.path for e in second] == [
        "auth_oauth@1/list_github_repos@1",
        "auth_oauth@1/display_result@1",
    ]
    assert github == [
        (
            "https://api.github.com/user/repos",
            {
                "Authorization": f"Bearer {TOKEN}",
                "User-Agent": "ADK-Sample-Agent",
                "Accept": "application/json",
            },
        )
    ]
    assert list(r.state) == [f"adk_oauth_state:{INTERRUPT}"]


def shape(r: Run) -> list[list[tuple[Any, ...]]]:
    """Per turn, each event's author, node path, output, interrupt ids, the
    names and ids of its function calls, its text and its error code (the
    OAuth ``state`` inside the credential request is random per run)."""
    return [
        [
            (
                e.author,
                e.node_info.path,
                e.output,
                sorted(e.long_running_tool_ids or ()),
                [
                    (p.function_call.name, p.function_call.id)
                    for p in (e.content.parts if e.content and e.content.parts else [])
                    if p.function_call
                ],
                "".join(
                    p.text or "" for p in (e.content.parts if e.content and e.content.parts else [])
                ),
                e.error_code,
            )
            for e in turn
        ]
        for turn in r.turns
    ]


def test_default_compile_makes_the_auth_gated_node_interruptible() -> None:
    """A node with ``auth_config`` always interrupts on its first run, which is
    statically visible: the compiler makes it interruptible without being told."""
    cw = compile_workflow(make())
    assert not cw.report.rejected
    assert any(
        f.subject == NODE and "auth_config" in f.message and "compiled interruptible" in f.message
        for f in cw.report.of("exact")
    )
    assert [t.name for t in cw.spec.transitions if "Resume" in t.name] == [
        "Wf_Resume",
        "Wf_ResumeMatch",
        "Wf_DropResume",
        f"Wf_{NODE}_ResumeRun",
    ]


@pytest.mark.parametrize("interruptible", [(), (NODE,)], ids=["default", "named"])
async def test_compiled_run_matches_native(github, orchestrator, interruptible) -> None:  # type: ignore[no-untyped-def]
    """The resumed node reuses the interrupted run's id (``list_github_repos@1``),
    so its auth gate finds the answer under ``wf_auth:<node_path>``; the net
    yields no event of its own, so the session sees exactly ADK's events."""
    native, petri, _ = await run_both(make, TURNS, orchestrator, interruptible=list(interruptible))
    assert native.final_output == REPOS
    assert shape(petri) == shape(native)
    assert petri.final_output == native.final_output
    assert petri.texts == native.texts == [MESSAGE]
    assert outputs(petri) == outputs(native)
    assert petri.state.keys() == native.state.keys()
    # One GitHub call per run, with the token from the user's answer.
    assert len(github) == 2
    assert github[0] == github[1]


async def test_compiled_first_turn_requests_the_same_credential(github, orchestrator) -> None:  # type: ignore[no-untyped-def]
    native = await run(make(), ["start"])
    node = PetriWorkflow.from_workflow(make(), orchestrator=orchestrator)
    petri = await run(node, ["start"])
    assert shape(petri) == shape(native)
    assert [c.id for c in credential_requests(petri.events)] == [INTERRUPT]
    assert interrupt_ids(petri.events) == {INTERRUPT}
    assert petri.texts == []
    assert github == []


def test_report_with_interruptible() -> None:
    """Named explicitly, the node is interruptible by request: the report has
    no auth finding, and the net is the same."""
    cw = compile_workflow(make(), interruptible=[NODE])
    assert not cw.report.rejected
    assert {f.subject for f in cw.report.of("exact")} == {NODE, "display_result"}
    assert not any("auth" in f.message for f in cw.report.findings)
    assert [t.name for t in cw.spec.transitions] == [
        t.name for t in compile_workflow(make()).spec.transitions
    ]


SUFFIX = " [on the match-free over-approximation]"


@requires_z3
@pytest.mark.parametrize("interruptible", [(), (NODE,)], ids=["default", "named"])
def test_proofs(interruptible: tuple[str, ...]) -> None:
    """With an interruptible node the safety claims hold on the match-free
    over-approximation; deadlock freedom is not claimed."""
    proofs = verify_workflow(compile_workflow(make(), interruptible=list(interruptible)), k=1)
    assert {p.kind for p in proofs} == {"safety"}
    assert {p.label: p.proven for p in proofs} == {
        "one turn at a time: place_bound(turnActive, 1)" + SUFFIX: True,
        "permit never doubles: place_bound(turnPermit, 1)" + SUFFIX: True,
        "display_result keeps one output: place_bound(display_result/terminalOutput, 1)"
        + SUFFIX: True,
        f"{NODE} runs serially: place_bound({NODE}/idle, 1)" + SUFFIX: True,
        "display_result runs serially: place_bound(display_result/idle, 1)" + SUFFIX: True,
    }
