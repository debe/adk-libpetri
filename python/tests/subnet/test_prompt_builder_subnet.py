"""Port of ``PromptBuilderSubnetTest.java``."""

from __future__ import annotations

import libpetri as lp
from google.adk.models.llm_request import LlmRequest
from google.adk.tools import FunctionTool
from google.genai import types

from adk_libpetri import colours as C
from adk_libpetri._spec import NetSpec
from adk_libpetri.subnet import prompt_builder


def user(t: str) -> types.Content:
    return types.Content(parts=[types.Part(text=t)])


async def run(config: prompt_builder.Config, *messages: types.Content) -> list[LlmRequest]:
    net = NetSpec.compose("test", prompt_builder.DEF).build(prompt_builder.action_bindings(config))
    marking = await lp.run_async(
        net, initial={C.USER_IN.name: list(messages)}, event_store=lp.InMemoryEventStore()
    )
    return list(marking.tokens(C.LLM_REQUEST.name))


async def test_minimal_config_produces_request_with_model_and_user_content() -> None:
    fixture = await run(prompt_builder.Config("fake-model"), user("hello"))

    assert len(fixture) == 1
    req = fixture[0]
    assert req.model == "fake-model"
    assert len(req.contents) == 1
    assert req.contents[0].parts[0].text == "hello"  # type: ignore[index]
    # Python LlmRequest always carries a config object; "no config" means nothing set on it.
    assert req.config.system_instruction is None


async def test_system_instruction_propagates_to_config() -> None:
    config = prompt_builder.Config("fake-model", system_instruction="You are a helpful assistant.")

    req = (await run(config, user("hi")))[0]

    sys_inst = req.config.system_instruction
    assert isinstance(sys_inst, types.Content)
    assert sys_inst.parts[0].text == "You are a helpful assistant."  # type: ignore[index]


async def test_tools_map_propagates_to_request() -> None:
    def noop() -> dict[str, object]:
        """No-op."""
        return {}

    # A FunctionTool, not a bare BaseTool: append_tools skips tools without a declaration.
    config = prompt_builder.Config("fake-model", tools={"noop": FunctionTool(noop)})

    req = (await run(config, user("hi")))[0]

    assert "noop" in req.tools_dict


async def test_empty_tools_map_does_not_appear_in_request() -> None:
    req = (await run(prompt_builder.Config("fake-model"), user("hi")))[0]

    assert req.tools_dict == {}
    assert not req.config.tools


def test_subnet_def_declares_exactly_one_transition_and_two_ports() -> None:
    assert list(prompt_builder.DEF.transition_names) == [prompt_builder.Transitions.BUILD_PROMPT]
    assert sorted(p.name for p in prompt_builder.DEF.ports) == ["llmRequest", "userIn"]
