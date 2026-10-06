"""Standard typed boundary places (colours) for adk-libpetri.

Every stock subnet declares its interface ports with these places, and
:meth:`~adk_libpetri._spec.NetSpec.compose` fuses them by ``(name, type)``.
Users compose by reusing the same constants.

The wrapper records (:class:`ToolCalls`, :class:`ToolResults`,
:class:`LegacySessionWrite`, :class:`TransferTarget`) give a generic-typed
colour such as ``list[FunctionCall]`` an unambiguous identity.

What this catalog deliberately does NOT contain: a general-purpose "state"
colour. The marking IS the state. In-net shared state belongs on
user-declared typed places read through read arcs. The only bag-shaped colour
here is :class:`LegacySessionWrite`, named loudly because it exists for one
purpose: write-only export to ADK's ``Session.state``. There is no
general-purpose raw-payload colour either; a feature this catalog does not
model belongs on a place *you* declare, typed to that feature (see the raw
provider passthrough demo).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from ._spec import VOID, Place


@dataclass(frozen=True, slots=True)
class ToolCalls:
    """The function calls of one model turn.

    ``model_turn`` is the model ``Content`` the calls came from, verbatim. A
    re-ask sends it back ahead of the function responses: Gemini pairs each
    response with the preceding call turn, and Gemini 3 also needs the turn's
    ``thought_signature`` parts, which a turn rebuilt from the calls would
    lose. ``None`` when the producer did not have it; dispatch then rebuilds a
    model turn from ``calls``.
    """

    calls: tuple[types.FunctionCall, ...]
    model_turn: types.Content | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "calls", tuple(self.calls))


@dataclass(frozen=True, slots=True)
class ToolResults:
    """Function responses plus the model turn whose calls they answer (required)."""

    results: tuple[types.FunctionResponse, ...]
    model_turn: types.Content

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        if self.model_turn is None:
            raise TypeError("ToolResults.model_turn is required")


@dataclass(frozen=True, slots=True)
class LegacySessionWrite:
    """Write-only envelope for an export to ADK's legacy ``Session.state``.

    **Not the in-net state primitive.** The mapping shape exists at this
    boundary because that is the shape of the external system. Tokens on
    :data:`LEGACY_SESSION_WRITE` are consumed only by ``PersistStateSubnet``.
    """

    delta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "delta", MappingProxyType(dict(self.delta)))


@dataclass(frozen=True, slots=True)
class TransferTarget:
    """An agent-transfer target name."""

    agent_name: str


USER_IN: Place[types.Content] = Place("userIn", types.Content)
"""User message inbound. Inject with ``runner.inject(USER_IN, content)``."""

EVENT_OUT: Place[Event] = Place("eventOut", Event)
"""Agent event outbound: the net's one way out to ADK."""

LLM_REQUEST: Place[LlmRequest] = Place("llmRequest", LlmRequest)
LLM_RESPONSE: Place[LlmResponse] = Place("llmResponse", LlmResponse)
TOOL_CALLS: Place[ToolCalls] = Place("toolCalls", ToolCalls)
TOOL_RESULTS: Place[ToolResults] = Place("toolResults", ToolResults)

LEGACY_SESSION_WRITE: Place[LegacySessionWrite] = Place("legacySessionWrite", LegacySessionWrite)
"""Write-only envelope for ADK ``Session.state`` mutations; drained by ``PersistState_Persist``."""

TRANSFER: Place[TransferTarget] = Place("transfer", TransferTarget)
"""Agent-transfer target, demuxed by ``TransferRouterSubnet`` over per-agent places."""

END_INVOCATION: Place[None] = Place("endInvocation", VOID)
"""Termination signal; inhibitor source for every advancing transition."""

TURN_PERMIT: Place[None] = Place("turnPermit", VOID)
"""The permit a one-turn-at-a-time net holds while idle (ADR 0005).

A turn takes it to start and every way the turn can end gives it back.
``PetriRunner`` seeds one token when it starts a net that has this place,
unless the run is a restore or the initial marking already names it.
"""

TURN_ABORT: Place[None] = Place("turnAbort", VOID)
"""Abandon the turn in flight.

A failed transition consumes its inputs and produces nothing, so a failure
mid-turn would keep the permit forever. ``PetriAgent`` signals this place on
every transition failure of a runner it created.
"""

__all__ = [
    "END_INVOCATION",
    "EVENT_OUT",
    "LEGACY_SESSION_WRITE",
    "LLM_REQUEST",
    "LLM_RESPONSE",
    "TOOL_CALLS",
    "TOOL_RESULTS",
    "TRANSFER",
    "TURN_ABORT",
    "TURN_PERMIT",
    "USER_IN",
    "LegacySessionWrite",
    "ToolCalls",
    "ToolResults",
    "TransferTarget",
]
