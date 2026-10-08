"""Replies the Petri builder gives without its model: the panel's own hello, a missing key."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_libpetri.web import builder, builder_guard
from adk_libpetri.web.builder import (
    _after_model,
    _before_model,
    create_petri_builder_assistant,
    gemini_key_missing,
    greeting,
)

from .conftest import add_workflow_app


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    with builder._VERDICTS_LOCK:
        builder._VERDICTS.clear()
    with builder_guard._NOTES_LOCK:
        builder_guard._NOTES.clear()
    yield
    with builder_guard._NOTES_LOCK:
        builder_guard._NOTES.clear()


def _state(agents: Path, app: str) -> dict[str, Any]:
    return {"root_directory": str(agents / app / "tmp" / app)}


def _ctx(state: dict[str, Any], text: str, *, replied: bool = False) -> Any:
    events = [SimpleNamespace(author="user")]
    if replied:
        events.append(SimpleNamespace(author="agent_builder_assistant"))
    return SimpleNamespace(
        state=state,
        session=SimpleNamespace(events=events),
        user_content=types.Content(role="user", parts=[types.Part(text=text)]),
    )


def _text(response: Any) -> str:
    assert isinstance(response, LlmResponse)
    assert response.content is not None
    return "".join(p.text or "" for p in response.content.parts or ())


def test_the_greeting_tells_what_the_net_is(agents: Path) -> None:
    text = greeting(_state(agents, "race"))  # no draft yet: the app's own file
    assert text is not None
    assert text.startswith(
        "This is `race`, a Petri net: 8 places, 5 transitions; 2 claims, not verified yet."
    )
    assert "read-only for a net" in text and "Click Save" not in text  # nothing to save
    draft = agents / "race" / "tmp" / "race"
    shutil.copytree(agents / "race", draft, ignore=shutil.ignore_patterns("tmp"))
    assert "Click Save" not in str(greeting(_state(agents, "race")))  # the draft is the app
    (draft / "root_agent.yaml").write_text(
        (draft / "root_agent.yaml").read_text().replace("inhibit: [won], node: fast", "inhibt: x")
    )
    changed = str(greeting(_state(agents, "race")))
    assert "does not load yet" in changed and "fix it before you Save" in changed
    assert "Click Save" not in changed


def test_no_greeting_for_an_app_without_a_net(agents: Path) -> None:
    plain = agents / "plain"
    plain.mkdir()
    (plain / "root_agent.yaml").write_text("name: plain\nagent_class: LlmAgent\n")
    assert greeting(_state(agents, "plain")) is None
    assert greeting({}) is None
    workflow = greeting(_state(agents, add_workflow_app(agents).name))
    assert workflow is not None and "a Petri workflow: " in workflow


async def test_the_panels_hello_is_answered_without_the_model(agents: Path) -> None:
    request = SimpleNamespace(model="gemini-2.5-pro")
    reply = await _before_model(_ctx(_state(agents, "race"), "hello"), request)
    # It proves the claims first: the verdicts, not "not verified yet".
    assert _text(reply).startswith(
        "This is `race`, a Petri net: 8 places, 5 transitions; 2 of 2 claims proven."
    )
    # Anything else, or a hello later on, goes to the model.
    assert await _before_model(_ctx(_state(agents, "race"), "add a timeout"), request) is None
    later = _ctx(_state(agents, "race"), "hello", replied=True)
    assert await _before_model(later, request) is None


async def test_without_a_key_every_reply_says_how_to_set_one(
    agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    request = SimpleNamespace(model="gemini-2.5-pro")
    reply = _text(await _before_model(_ctx(_state(agents, "race"), "add a timeout"), request))
    assert reply.startswith("No Gemini API key is set") and "GOOGLE_API_KEY=" in reply
    assert "graph panel works without it" in reply
    assert "send `check` or `verify`" in reply  # a net app: what still works
    hello = _text(await _before_model(_ctx(_state(agents, "race"), "hello"), request))
    assert hello.startswith("This is `race`") and "No Gemini API key is set" in hello
    later = _ctx(_state(agents, "race"), "verify", replied=True)
    verify = _text(await _before_model(later, request))
    # No key needed, nor any key notice.
    assert verify == (
        "`race`: 2 of 2 claims proven.\n\n- one answer per turn: proven\n- deadlock_free: proven"
    )
    assert not gemini_key_missing("openai/gpt-4o")  # not a Gemini model: not ours to judge
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    assert not gemini_key_missing("gemini-2.5-pro")


async def test_what_the_canvas_dropped_leads_the_next_reply(agents: Path) -> None:
    builder_guard._note("race", ["root_agent.yaml"], ["sub_agents (sub_agent_1)"])
    response = LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text="Done: added a timeout.")])
    )
    call = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name="petri_schema", args={}))],
        )
    )
    ctx = _ctx(_state(agents, "race"), "add a timeout")
    assert await _after_model(ctx, call) is None  # a tool call: kept for the text
    out = await _after_model(ctx, response)
    text = _text(out)
    assert text.startswith("The builder canvas cannot change a Petri net")
    assert "sub_agents (sub_agent_1)" in text and text.endswith("Done: added a timeout.")
    assert await _after_model(ctx, response) is None  # once


def test_the_assistant_has_the_callbacks() -> None:
    assistant = create_petri_builder_assistant()
    assert _before_model in assistant.before_model_callback
    assert _after_model in assistant.after_model_callback


async def test_verify_and_check_are_answered_without_the_model(agents: Path) -> None:
    request = SimpleNamespace(model="gemini-2.5-pro")
    draft = agents / "race_naive" / "tmp" / "race_naive"
    shutil.copytree(agents / "race_naive", draft, ignore=shutil.ignore_patterns("tmp"))
    state = _state(agents, "race_naive")
    check = _text(await _before_model(_ctx(state, "Check.", replied=True), request))
    assert check.startswith("`race_naive`: net 'race_naive' loads (")
    verify = _text(await _before_model(_ctx(state, "/verify", replied=True), request))
    # Each claim, the step where the violated one breaks, its last steps quoted.
    assert verify.startswith("`race_naive`: 1 of 2 claims proven.\n\n- ")
    assert (
        "- one answer per turn: violated at step 7 (Race_Commit completes: eventOut=2 "
        "exceeds the bound 1)"
    ) in verify
    assert "- deadlock_free: proven" in verify
    assert "Steps that break *one answer per turn*:\n\n5. " in verify
    assert verify.count("\n7. Race_Commit completes:") == 1
    assert "Click Save" not in verify  # not every claim proven
    # Not a net: the model answers.
    plain = agents / "plain"
    plain.mkdir()
    (plain / "root_agent.yaml").write_text("name: plain\nagent_class: LlmAgent\n")
    assert await _before_model(_ctx(_state(agents, "plain"), "verify"), request) is None


async def test_a_slow_proof_does_not_hold_the_greeting(
    agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    def slow(*_: object, **__: object) -> None:
        time.sleep(0.5)
        return None

    monkeypatch.setattr(builder, "_verify_now", slow)
    monkeypatch.setattr(builder, "GREETING_VERIFY_S", 0.05)
    request = SimpleNamespace(model="gemini-2.5-pro")
    reply = _text(await _before_model(_ctx(_state(agents, "race"), "hello"), request))
    assert "2 claims, not verified yet" in reply
    assert "send `verify` for the verdicts" in reply


def test_replies_name_the_net_and_its_app(agents: Path) -> None:
    app = agents / "my_app"
    shutil.copytree(agents / "race", app)
    text = greeting(_state(agents, "my_app"))
    assert text is not None and text.startswith("This is `race` (app `my_app`), a Petri net")


def test_without_a_key_a_plain_app_is_not_told_about_nets() -> None:
    text = builder.no_key_text(net=False)
    assert "net" not in text.replace("GOOGLE_API_KEY", "")
    assert "chat with a Gemini agent needs it too" in text
