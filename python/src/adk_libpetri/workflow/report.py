"""What a ``from_workflow`` compilation kept, changed, or could not translate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["exact", "approximated", "opaque", "rejected"]


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    subject: str
    message: str


@dataclass
class TranslationReport:
    """Every deviation from ADK's own scheduling is listed here, never silent."""

    workflow: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: Severity, subject: str, message: str) -> None:
        self.findings.append(Finding(severity, subject, message))

    def of(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    @property
    def rejected(self) -> list[Finding]:
        return self.of("rejected")

    def __str__(self) -> str:
        lines = [f"TranslationReport({self.workflow})"]
        lines += [f"  [{f.severity}] {f.subject}: {f.message}" for f in self.findings]
        return "\n".join(lines)


class WorkflowTranslationError(ValueError):
    def __init__(self, report: TranslationReport) -> None:
        super().__init__(
            "workflow cannot be compiled faithfully:\n"
            + "\n".join(f"  {f.subject}: {f.message}" for f in report.rejected)
        )
        self.report = report


class WorkflowRunError(RuntimeError):
    """A turn the net itself ended in failure (not a failing ADK node).

    Raised from the compiled workflow node, so ADK's node runner records it
    as an error event and the failure reaches ``Runner.run_async`` as a
    native ``Workflow`` failure would.
    """


class LoopBudgetExhausted(WorkflowRunError):
    """A budgeted back edge (``back_edge_budget``) was taken more often than allowed."""


class NotInterruptibleError(WorkflowRunError):
    """A node requested input, but it was not compiled interruptible."""


class AmbiguousRouteError(WorkflowRunError):
    """A node emitted several routes matching different branches (``multi_route='reject'``)."""
