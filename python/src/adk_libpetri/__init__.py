"""adk-libpetri: a Coloured Time Petri Net orchestration core for Google ADK.

Replaces ADK's graph ``Workflow`` (and the deprecated Sequential/Parallel/Loop
agents) with a libpetri net that Z3 can prove properties of. Stock ADK
``Runner`` drives it through :class:`~adk_libpetri.runner.PetriAgent`.
"""

from __future__ import annotations

from ._aio import HotStream, OrchestratorLoop, on_loop
from ._experimental import experimental, is_experimental
from ._spec import (
    VOID,
    Ctx,
    In,
    NetSpec,
    Place,
    Port,
    Timing,
    TransitionSpec,
    all_tokens,
    and_,
    at_least,
    deadline,
    delayed,
    exact,
    exactly,
    forward_input,
    lp_actions,
    one,
    out,
    place,
    timeout,
    window,
    xor,
)

__version__ = "0.1.0"

__all__ = [
    "VOID",
    "Ctx",
    "HotStream",
    "In",
    "NetSpec",
    "OrchestratorLoop",
    "Place",
    "Port",
    "Timing",
    "TransitionSpec",
    "__version__",
    "all_tokens",
    "and_",
    "at_least",
    "deadline",
    "delayed",
    "exact",
    "exactly",
    "experimental",
    "forward_input",
    "is_experimental",
    "lp_actions",
    "on_loop",
    "one",
    "out",
    "place",
    "timeout",
    "window",
    "xor",
]
