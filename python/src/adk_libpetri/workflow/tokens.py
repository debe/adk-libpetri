"""Colours of a compiled ADK workflow net."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class WfToken:
    """A node trigger: the node's input plus the ADK branch it runs on."""

    input: Any
    branch: str | None = None
    use_sub_branch: bool = False


@dataclass(frozen=True, slots=True)
class Retry:
    """A failed attempt waiting for its backoff: the same run, attempt ``attempt + 1`` next."""

    trigger: WfToken
    run_id: str
    attempt: int


@dataclass(frozen=True, slots=True)
class NodeOutput:
    """A terminal node's output (ADK: at most one per workflow run)."""

    node: str
    output: Any


@dataclass(frozen=True, slots=True)
class Parked:
    """A node waiting on a ``RequestInput`` interrupt."""

    node: str
    interrupt_id: str
    trigger: WfToken
    run_id: str = "1"
    """The interrupted run's id: ADK resumes the same run (same node path)."""


@dataclass(frozen=True, slots=True)
class Resumed:
    """A human's answer to interrupt ``interrupt_id``, from the next turn."""

    interrupt_id: str
    response: Any


@dataclass(frozen=True, slots=True)
class ResumeTrigger:
    parked: Parked
    response: Any


@dataclass(frozen=True, slots=True)
class WorkflowFailure:
    """Why a turn failed: a node's own error, or the net's (``error`` is then a
    :class:`~adk_libpetri.workflow.report.WorkflowRunError`)."""

    node: str
    error_code: str
    message: str
    error: BaseException | None = field(default=None, compare=False)
    node_path: str = ""
    from_node: bool = False
    """True when an ADK node failed: its runner already reported the error."""
