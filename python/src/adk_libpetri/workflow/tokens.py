"""Colours of a compiled ADK workflow net."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WfToken:
    """A node trigger: the node's input plus the ADK branch it runs on."""

    input: Any
    branch: str | None = None
    use_sub_branch: bool = False


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
    """A node that failed for good (retries spent or not retryable)."""

    node: str
    error_code: str
    message: str
