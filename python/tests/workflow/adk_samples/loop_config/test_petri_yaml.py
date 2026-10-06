"""``agent_class: adk_libpetri.workflow.PetriWorkflow`` in ADK's own YAML.

``petri_root_agent.yaml`` is ``root_agent.yaml`` with the PetriWorkflow agent
class and compile options. ADK's loader (what ``adk web`` and ``adk run`` call)
builds it; it must serve the same run as the native YAML workflow.
"""

from __future__ import annotations

import json

import pytest
from google.adk.agents.config_agent_utils import from_config
from google.adk.workflow import Workflow

from adk_libpetri import OrchestratorLoop
from adk_libpetri.workflow import PetriWorkflow, WorkflowTranslationError
from support.fake_llm import ScriptedLlm, text

from .._harness import llm_agents, run
from .test_loop_config import HERE, ROOT_YAML, TRACES, _samples_on_sys_path, paths

PETRI_YAML = HERE / "petri_root_agent.yaml"

__all__ = ["_samples_on_sys_path"]  # the sys.path fixture, used by every test here


def _fake(node: Workflow, trace: str) -> None:
    _, headlines, feedbacks = TRACES[trace]
    fakes = {
        "generate_headline": ScriptedLlm.of(*(text(h) for h in headlines)),
        "evaluate_headline": ScriptedLlm.of(*(text(json.dumps(f)) for f in feedbacks)),
    }
    for a in llm_agents(node):
        a.model = fakes[a.name]


def test_adks_loader_builds_a_compiled_workflow() -> None:
    node = from_config(str(PETRI_YAML))
    assert isinstance(node, PetriWorkflow)
    cw = node.compiled
    assert cw.budgets == {("route_headline", "generate_headline"): 3}
    assert set(cw.node_names) == {
        "process_input",
        "generate_headline",
        "evaluate_headline",
        "route_headline",
    }
    assert not cw.report.rejected


@pytest.mark.parametrize("trace", sorted(TRACES))
async def test_yaml_petri_workflow_matches_the_native_yaml_workflow(
    trace: str, orchestrator: OrchestratorLoop
) -> None:
    user = TRACES[trace][0]
    native_wf = from_config(str(ROOT_YAML))
    assert isinstance(native_wf, Workflow)
    _fake(native_wf, trace)
    native = await run(native_wf, [user], "native")

    petri = PetriWorkflow.from_config(str(PETRI_YAML), orchestrator=orchestrator)
    _fake(petri.compiled.workflow, trace)
    compiled = await run(petri, [user], "compiled")

    assert compiled.texts == native.texts
    assert compiled.authors == native.authors
    assert paths(compiled) == paths(native)
    assert compiled.state == native.state


async def test_a_workflow_yaml_compiles_with_options(orchestrator: OrchestratorLoop) -> None:
    with pytest.raises(WorkflowTranslationError, match="instruction"):
        PetriWorkflow.from_config(str(ROOT_YAML), orchestrator=orchestrator)
    node = PetriWorkflow.from_config(str(ROOT_YAML), orchestrator=orchestrator, state="legacy_read")
    assert "Wf_route_headline_Run" in node.compiled.spec.transition_names
