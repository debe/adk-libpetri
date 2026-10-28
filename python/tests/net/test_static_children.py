"""A net names the ADK nodes it runs as its static children.

ADK 2.13's ``Runner.close()`` closes the toolsets of every static node under
the root (``Runner._collect_toolset`` walks ``_iter_static_children``), so a
toolset on an agent inside a net or a compiled workflow is closed with the
runner, as it is inside a ``Workflow``.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.config_agent_utils import from_config
from google.adk.runners import InMemoryRunner
from google.adk.tools.base_toolset import BaseToolset
from google.adk.workflow import Workflow

from adk_libpetri._aio import OrchestratorLoop
from adk_libpetri.net import PetriNet
from adk_libpetri.workflow import PetriWorkflow

from .conftest import BLUEPRINTS


class _Toolset(BaseToolset):
    closed: bool = False

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        return []

    async def close(self) -> None:
        self.closed = True


async def test_closing_the_runner_closes_a_toolset_inside_a_net() -> None:
    node = from_config(str(BLUEPRINTS / "bp_llm" / "root.yaml"))
    assert isinstance(node, PetriNet)
    [helper] = node.nodes[0]
    assert list(node._iter_static_children()) == [helper]
    toolset = _Toolset()
    helper.tools.append(toolset)

    await InMemoryRunner(node=node, app_name="app").close()
    assert toolset.closed


async def test_closing_the_runner_closes_a_toolset_inside_a_compiled_workflow(
    orchestrator: OrchestratorLoop,
) -> None:
    toolset = _Toolset()
    agent = LlmAgent(name="helper", model="gemini-2.5-flash", tools=[toolset])
    wf = Workflow(name="root", edges=[("START", agent)])
    node = PetriWorkflow.from_workflow(wf, orchestrator=orchestrator)
    assert [n.name for n in node._iter_static_children()] == ["helper"]

    await InMemoryRunner(node=node, app_name="app").close()
    assert toolset.closed
