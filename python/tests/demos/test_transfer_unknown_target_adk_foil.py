"""ADK-only foil for multi-agent transfer, the sibling of
``test_multi_agent_demo.test_hallucinated_agent_name_surfaces_as_typed_error_event_not_npe``.

Re-derived from google-adk 2.11, where the Java foil holds only in part.

What carries over from Java:

* the tree lookup ``BaseAgent.find_agent`` / ``find_sub_agent`` returns
  ``None`` for an unknown name, with no error routing;
* the ``transfer_to_agent`` tool function records *any* string into
  ``EventActions.transfer_to_agent``, unvalidated. ``TransferToAgentTool``
  only adds a JSON-schema ``enum`` to the *declaration*, a hint to the model,
  not a check on what it calls with.

What differs: Java ADK lets the empty ``Optional`` travel until a site
forgets to unwrap it (a failure or a silent no-op). Python ADK resolves the
name at one site, ``workflow/utils/_transfer_utils.resolve_and_derive_transfer_context``
(it returns ``(None, None)``), and the dynamic node scheduler then raises a
bare ``ValueError("Transfer target agent '<name>' not found.")``. So the
failure is loud, but still untyped and fatal: it escapes ``Runner.run_async``
and ends the whole invocation, no error ``Event`` reaches the caller, and the
session keeps the unvalidated transfer the model asked for.

The net's ``TransferRouter`` instead turns an unknown target into a typed
error ``Event`` in the topology, and the turn ends normally. This foil
green-locks the ADK behaviour above: if a release adds typed unknown-target
handling, an assertion here goes red and the catalog gets updated.
"""

from __future__ import annotations

import pytest
from google.adk.agents import LlmAgent
from google.adk.events.event_actions import EventActions
from google.adk.runners import InMemoryRunner
from google.adk.tools.transfer_to_agent_tool import TransferToAgentTool, transfer_to_agent
from google.genai import types

from support.fake_llm import ScriptedLlm, text, transfer


def specialists_under_router(router_llm: ScriptedLlm) -> LlmAgent:
    billing = LlmAgent(
        name="billing", description="billing specialist", model=ScriptedLlm.of(text("billing"))
    )
    tech_support = LlmAgent(
        name="tech_support", description="tech specialist", model=ScriptedLlm.of(text("tech"))
    )
    return LlmAgent(
        name="router",
        description="routes to a specialist",
        model=router_llm,
        sub_agents=[billing, tech_support],
    )


def test_adk_agent_lookup_returns_none_for_a_hallucinated_target_with_no_typed_error_foil() -> None:
    router = specialists_under_router(ScriptedLlm.of())

    # Known targets resolve.
    assert router.find_agent("billing") is not None
    assert router.find_agent("tech_support") is not None

    # A hallucinated target resolves to None: no typed error, no unknown-target
    # routing.
    assert router.find_agent("hallucinated_typo") is None
    assert router.find_sub_agent("hallucinated_typo") is None


@pytest.mark.filterwarnings("ignore::UserWarning")  # JSON_SCHEMA_FOR_FUNC_DECL is experimental
def test_adk_transfer_tool_records_any_name_unvalidated_foil() -> None:
    class _ToolContext:
        def __init__(self) -> None:
            self.actions = EventActions()

    ctx = _ToolContext()
    transfer_to_agent("hallucinated_typo", ctx)  # type: ignore[arg-type]
    assert ctx.actions.transfer_to_agent == "hallucinated_typo"

    # The enum is on the declaration only: advice to the model.
    tool = TransferToAgentTool(["billing", "tech_support"])
    decl = tool._get_declaration()
    assert decl is not None
    schema = decl.parameters_json_schema or {}
    enum = schema.get("properties", {}).get("agent_name", {}).get("enum")
    if enum is None and decl.parameters is not None and decl.parameters.properties:
        enum = decl.parameters.properties["agent_name"].enum
    assert enum == ["billing", "tech_support"]


@pytest.mark.filterwarnings("ignore::UserWarning")  # JSON_SCHEMA_FOR_FUNC_DECL is experimental
async def test_adk_run_fails_the_invocation_with_an_untyped_value_error_foil() -> None:
    router_llm = ScriptedLlm.of(transfer("hallucinated_typo"))
    runner = InMemoryRunner(agent=specialists_under_router(router_llm), app_name="foil")
    session = await runner.session_service.create_session(app_name="foil", user_id="u")

    seen = []
    with pytest.raises(ValueError, match="Transfer target agent 'hallucinated_typo' not found"):
        async for e in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="help")]),
        ):
            seen.append(e)

    # The bare ValueError is the only signal: no event names the bad target in
    # its content, so nothing typed reaches the caller.
    def text_of(e: object) -> str:
        content = getattr(e, "content", None)
        return "".join(p.text or "" for p in (content.parts or [])) if content else ""

    assert not any("hallucinated_typo" in text_of(e) for e in seen)
    # The unvalidated transfer was recorded and handed out as an ordinary event.
    assert [e.actions.transfer_to_agent for e in seen if e.actions.transfer_to_agent] == [
        "hallucinated_typo"
    ]
    # ...and the session keeps it.
    stored = await runner.session_service.get_session(
        app_name="foil", user_id="u", session_id=session.id
    )
    assert stored is not None
    assert "hallucinated_typo" in [e.actions.transfer_to_agent for e in stored.events]
