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
