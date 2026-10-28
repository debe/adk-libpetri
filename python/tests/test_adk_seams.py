"""Every private google-adk symbol the port relies on, in one place.

ADK may rename or drop a private name in any release. The port reads these
names directly, never through ``getattr(..., default)``, so a rename fails
loudly; this test fails first, on the bump, naming the seam. It is the
authoritative list behind ADR 0006's "private, pinned by the range".
"""

from __future__ import annotations

import importlib
import inspect

import pytest
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.cli.utils._nested_agent_loader import NestedAgentLoader
from google.adk.tools.function_tool import FunctionTool
from google.adk.workflow import BaseNode, FunctionNode

# (module, name): private modules or names imported by src/adk_libpetri.
IMPORTED = [
    ("google.adk.workflow._graph", "EdgeItem"),  # net/node.py, workflow/agent.py
    ("google.adk.workflow._base_node", "START"),  # compiler.py, net/node.py
    ("google.adk.workflow._base_node", "BaseNode"),
    ("google.adk.workflow._errors", "WorkflowConfigurationError"),  # compiler.py
    ("google.adk.workflow._node_state", "NodeState"),  # compiler.py, retries
    ("google.adk.workflow.utils._retry_utils", "_should_retry_node"),
    ("google.adk.workflow._tool_node", "_ToolNode"),  # compiler.py, interrupts
    ("google.adk.workflow._tool_node", "_get_tool_auth_config"),
    ("google.adk.workflow._parallel_worker", "_ParallelWorker"),  # compiler.py
    ("google.adk.flows.llm_flows.prompt._instructions_utils", "_TEMPLATE_VAR_PATTERN"),
    ("google.adk.flows.llm_flows.prompt._instructions_utils", "_is_valid_state_name"),
    ("google.adk.cli.utils._nested_agent_loader", "NestedAgentLoader"),  # web/loader.py
]


@pytest.mark.parametrize(("module", "name"), IMPORTED, ids=lambda v: str(v))
def test_imported_private_symbol_exists(module: str, name: str) -> None:
    assert hasattr(importlib.import_module(module), name), f"{module}.{name} is gone"


# (class, attribute): private members the port reads or overrides.
MEMBERS = [
    (InvocationContext, "_abort_signal"),  # run_turn's abort (_net_node.py, petri_agent.py)
    (Context, "_run_node_internal"),  # compiled node runs, PetriNet node: transitions
    (BaseNode, "_requires_all_predecessors"),  # compiler.py: JoinNode
    (BaseNode, "_iter_static_children"),  # NetNodeBase override: Runner.close() toolsets
    (BaseNode, "_run_impl"),  # compiler.py: retry attempts
    (NestedAgentLoader, "_perform_load"),  # web/loader.py override: assistant, traces
    (NestedAgentLoader, "_is_valid_agent_dir"),
    (NestedAgentLoader, "_validate_agent_name"),
]


@pytest.mark.parametrize(("cls", "name"), MEMBERS, ids=lambda v: getattr(v, "__name__", v))
def test_private_member_exists(cls: type, name: str) -> None:
    assert hasattr(cls, name), f"{cls.__name__}.{name} is gone"


def test_run_node_internal_takes_the_arguments_the_port_passes() -> None:
    params = inspect.signature(Context._run_node_internal).parameters
    for p in (
        "node_input",
        "use_as_output",
        "return_ctx",
        "run_id",
        "use_sub_branch",
        "override_branch",
        "resume_inputs",
        "skip_run_id_validation",
    ):
        assert p in params, f"Context._run_node_internal lost {p}"


def test_should_retry_node_takes_error_config_and_state() -> None:
    from google.adk.workflow.utils._retry_utils import _should_retry_node

    assert len(inspect.signature(_should_retry_node).parameters) == 3


# Private attributes set per instance (in __init__ or as PrivateAttr), which
# the port writes on a node Context or reads on a FunctionNode / FunctionTool.
INSTANCE_ATTRIBUTES = [
    (Context, "_error"),  # _net_node.py: a failed turn
    (Context, "_error_node_path"),
    (Context, "_output_delegated"),  # _net_node.py, compiler.py, net/node.py
    (Context, "_interrupt_ids"),  # _net_node.py: a parked turn
    (Context, "_attempt_count"),  # compiler.py: retry attempts
    (FunctionNode, "_sig"),  # compiler.py: state reads
    (FunctionNode, "_context_param_name"),
    (FunctionNode, "_func"),
    (FunctionNode, "_unwrapped_func"),
    (FunctionTool, "_require_confirmation"),  # compiler.py: interrupts
    (NestedAgentLoader, "_is_single_agent"),  # web/loader.py: list_agents
]


@pytest.mark.parametrize(
    ("cls", "name"), INSTANCE_ATTRIBUTES, ids=lambda v: getattr(v, "__name__", v)
)
def test_private_instance_attribute_is_still_set(cls: type, name: str) -> None:
    private = getattr(cls, "__private_attributes__", {})
    assert name in private or f"self.{name}" in inspect.getsource(cls), (
        f"{cls.__name__} no longer sets {name}"
    )
