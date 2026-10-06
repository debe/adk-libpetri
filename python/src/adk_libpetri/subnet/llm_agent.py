"""Stock LLM-agent subnet: LlmStep + Router + ToolDispatch in a reask-budgeted loop,
run one turn at a time under a turn permit.

Boundary ports: ``userIn`` and ``turnAbort`` in; ``eventOut`` and ``transfer``
out. The net also holds ``TURN_PERMIT``, which must carry one token at start
(``PetriRunner`` seeds it).

Topology::

    [USER_IN] + [TURN_PERMIT] --StartTurn--> and([TURN_ACTIVE], [TURN_INPUT])
    [TURN_INPUT] --BuildPrompt--> and([LLM_REQUEST], [REASK_BUDGET]*N, [CONVERSATION])

    [LLM_REQUEST]  --LlmStep------> [LLM_RESPONSE]
    [LLM_RESPONSE] --Router_Route-> xor([TOOL_CALLS], [HANDOFF], [ANSWER])
    [TOOL_CALLS]   --ToolDispatch-> [TOOL_RESULTS]

    [TOOL_RESULTS] + [REASK_BUDGET] + [CONVERSATION]
                   --ReAsk (prio 10)--> and([LLM_REQUEST], [CONVERSATION])
    [TOOL_RESULTS] + inhibitor(REASK_BUDGET)
                   --ReAskExhaustedFallback (prio -10)--> [ANSWER] (canned)

    [ANSWER]  + [TURN_ACTIVE] + [CONVERSATION], reset(REASK_BUDGET)
                   --EmitAnswer--> and([EVENT_OUT], [TURN_PERMIT])
    [HANDOFF] + [TURN_ACTIVE] + [CONVERSATION], reset(REASK_BUDGET)
                   --EmitTransfer--> and([TRANSFER], [TURN_PERMIT])

    [TURN_ABORT] + [TURN_ACTIVE], reset(every place the turn holds)
                   --AbortTurn (prio 30)--> [TURN_PERMIT]
    [TURN_ABORT] + read(TURN_PERMIT) --DropAbort (prio 30)--> (nothing)

**One turn at a time.** ``StartTurn`` consumes the single permit and only a
turn's end returns it, so a second ``USER_IN`` waits structurally. Every way
a turn ends goes through ``EmitAnswer``, ``EmitTransfer`` or ``AbortTurn``,
and each returns the permit. The permit-moving actions only move tokens, so
none can fail and lose it.

**Abort.** A failed transition consumes its inputs and produces nothing.
``TURN_ABORT`` is the way out: ``AbortTurn`` clears what the turn holds and
returns the permit; ``DropAbort`` drops an abort when no turn is in flight,
ranking above ``StartTurn`` so an abort and an input landing in one pass drop
the abort first (ADR 0005).

**Reask budget** (commitment 6). ``BuildPrompt`` seeds N unit tokens; each
re-ask consumes one; when the place is empty the inhibitor-guarded fallback
answers with ``fallback_content``. The decision lives in marking and
priority, never in action-level branching.

A convenience template, not the framework: compose your own nets from the
stock building blocks with :meth:`~adk_libpetri._spec.NetSpec.compose`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from .. import colours as C
from .._spec import (
    Action,
    Ctx,
    NetSpec,
    Place,
    Port,
    TransitionSpec,
    and_,
    one,
    out,
)
from . import llm_requests, llm_step, router, tool_dispatch
from ._common import IdSupplier, random_id
from .actions import bind, merge

NAME = "LlmAgent"


class Transitions:
    START_TURN = f"{NAME}_StartTurn"
    BUILD_PROMPT = f"{NAME}_BuildPrompt"
    RE_ASK = f"{NAME}_ReAsk"
    RE_ASK_EXHAUSTED_FALLBACK = f"{NAME}_ReAskExhaustedFallback"
    EMIT_ANSWER = f"{NAME}_EmitAnswer"
    EMIT_TRANSFER = f"{NAME}_EmitTransfer"
    ABORT_TURN = f"{NAME}_AbortTurn"
    DROP_ABORT = f"{NAME}_DropAbort"


@dataclass(frozen=True, slots=True)
class Conversation:
    """The invocation's turns, oldest first: the user turn, then each model
    function-call turn followed by its function-response turn."""

    turns: tuple[types.Content, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "turns", tuple(self.turns))

    def append(self, *more: types.Content) -> Conversation:
        return Conversation((*self.turns, *more))


REASK_BUDGET: Place[None] = Place(f"{NAME}_reaskBudget")
"""Reask-budget counter: cardinality is the remaining attempts."""

CONVERSATION: Place[Conversation] = Place(f"{NAME}_conversation", Conversation)
"""The current invocation's turns; every re-ask carries the whole exchange."""

TURN_ACTIVE: Place[None] = Place(f"{NAME}_turnActive")
TURN_INPUT: Place[types.Content] = Place(f"{NAME}_turnInput", types.Content)
ANSWER: Place[Event] = Place(f"{NAME}_answer", Event)
HANDOFF: Place[C.TransferTarget] = Place(f"{NAME}_handoff", C.TransferTarget)

DEFAULT_FALLBACK = types.Content(
    role="model",
    parts=[
        types.Part(
            text="I couldn't complete all the requested steps. Please rephrase your question."
        )
    ],
)


@dataclass(frozen=True)
class Config:
    name: str
    model: str
    system_instruction: str | None = None
    tools: Mapping[str, BaseTool] = field(default_factory=dict)
    reask_budget: int = 3
    fallback_content: types.Content = field(
        default_factory=lambda: DEFAULT_FALLBACK.model_copy(deep=True)
    )
    invocation_id_supplier: IdSupplier = field(default=random_id)
    callbacks: llm_step.Callbacks = field(default_factory=llm_step.Callbacks.none)
    tool_context_supplier: tool_dispatch.ToolContextSupplier = lambda: None

    def __post_init__(self) -> None:
        if self.reask_budget < 1:
            raise ValueError(f"reask_budget must be >= 1, got: {self.reask_budget}")
        object.__setattr__(self, "tools", dict(self.tools))


def _abort_turn(step_def: NetSpec) -> TransitionSpec:
    held: dict[str, Place[Any]] = {
        p.name: p
        for p in (
            TURN_INPUT,
            REASK_BUDGET,
            CONVERSATION,
            ANSWER,
            HANDOFF,
            C.LLM_REQUEST,
            C.LLM_RESPONSE,
            C.TOOL_CALLS,
            C.TOOL_RESULTS,
        )
    }
    for p in step_def.places:
        if p.name != C.EVENT_OUT.name:
            held.setdefault(p.name, p)
    # Above every step of the turn, the streaming step's chunk emission (20)
    # included: a chunk admitted in the same pass as the abort is reset with
    # the turn instead of being emitted as a partial of it.
    return TransitionSpec(
        Transitions.ABORT_TURN,
        (one(C.TURN_ABORT), one(TURN_ACTIVE)),
        out(C.TURN_PERMIT),
        resets=tuple(held.values()),
        priority=30,
    )


def build_composed_def(net_name: str, step_def: NetSpec) -> NetSpec:
    """The agent shell around ``step_def`` (shared with the streaming agent)."""
    own = (
        TransitionSpec(
            Transitions.START_TURN,
            (one(C.USER_IN), one(C.TURN_PERMIT)),
            and_(TURN_ACTIVE, TURN_INPUT),
        ),
        TransitionSpec(
            Transitions.BUILD_PROMPT,
            (one(TURN_INPUT),),
            and_(C.LLM_REQUEST, REASK_BUDGET, CONVERSATION),
        ),
        TransitionSpec(
            Transitions.RE_ASK,
            (one(C.TOOL_RESULTS), one(REASK_BUDGET), one(CONVERSATION)),
            and_(C.LLM_REQUEST, CONVERSATION),
            priority=10,
        ),
        TransitionSpec(
            Transitions.RE_ASK_EXHAUSTED_FALLBACK,
            (one(C.TOOL_RESULTS),),
            out(ANSWER),
            inhibitors=(REASK_BUDGET,),
            priority=-10,
        ),
        router.route_transition(HANDOFF, ANSWER),
        TransitionSpec(
            Transitions.EMIT_ANSWER,
            (one(ANSWER), one(TURN_ACTIVE), one(CONVERSATION)),
            and_(C.EVENT_OUT, C.TURN_PERMIT),
            resets=(REASK_BUDGET,),
        ),
        TransitionSpec(
            Transitions.EMIT_TRANSFER,
            (one(HANDOFF), one(TURN_ACTIVE), one(CONVERSATION)),
            and_(C.TRANSFER, C.TURN_PERMIT),
            resets=(REASK_BUDGET,),
        ),
        _abort_turn(step_def),
        # Above StartTurn: an abort and an input that land in one pass with
        # the permit at rest drop the abort first. At equal priority StartTurn
        # would take the permit and AbortTurn would then wipe the fresh turn.
        TransitionSpec(
            Transitions.DROP_ABORT,
            (one(C.TURN_ABORT),),
            reads=(C.TURN_PERMIT,),
            priority=30,
        ),
    )
    return NetSpec.compose(
        net_name,
        *own,
        step_def,
        tool_dispatch.DEF,
        extra_places=(
            C.TURN_PERMIT,
            TURN_ACTIVE,
            TURN_INPUT,
            REASK_BUDGET,
            CONVERSATION,
            ANSWER,
            HANDOFF,
        ),
        ports=(
            Port("userIn", "in", C.USER_IN),
            Port("turnAbort", "in", C.TURN_ABORT),
            Port("eventOut", "out", C.EVENT_OUT),
            Port("transfer", "out", C.TRANSFER),
        ),
    )


DEF = build_composed_def(NAME, llm_step.DEF)
"""Stateless definition: composed body plus the 4-port interface."""


def router_config(c: Config) -> router.Config:
    return router.Config(c.name, c.invocation_id_supplier)


def own_actions(config: Config) -> dict[str, Action]:
    """The agent's own transitions, router included (shared with the streaming agent).

    The permit-moving actions (StartTurn, the emits, AbortTurn) only move
    tokens: a failure there would lose the permit with no turn left to abort.
    """

    def start_turn(ctx: Ctx) -> None:
        ctx.input(C.TURN_PERMIT)
        ctx.output(TURN_INPUT, ctx.input(C.USER_IN))
        ctx.signal(TURN_ACTIVE)

    def build_prompt(ctx: Ctx) -> None:
        user = ctx.input(TURN_INPUT)
        ctx.output(
            C.LLM_REQUEST,
            llm_requests.build(config.model, config.system_instruction, config.tools, [user]),
        )
        ctx.output(CONVERSATION, Conversation((user,)))
        # The place is empty: the transition that ended the last turn reset it.
        ctx.output_many(REASK_BUDGET, [None] * config.reask_budget)

    def re_ask(ctx: Ctx) -> None:
        ctx.input(REASK_BUDGET)
        results = ctx.input(C.TOOL_RESULTS)
        conversation = ctx.input(CONVERSATION)
        # The continuation carries the whole invocation so far: the model's
        # call turn goes back verbatim (thought signatures included) ahead of
        # the responses, in a "user"-role turn, as ADK's own flow sends them.
        response_turn = types.Content(
            role="user", parts=[types.Part(function_response=fr) for fr in results.results]
        )
        nxt = conversation.append(results.model_turn, response_turn)
        ctx.output(
            C.LLM_REQUEST,
            llm_requests.build(config.model, config.system_instruction, config.tools, nxt.turns),
        )
        ctx.output(CONVERSATION, nxt)

    def re_ask_exhausted(ctx: Ctx) -> None:
        ctx.input(C.TOOL_RESULTS)
        ctx.output(
            ANSWER,
            Event(
                invocation_id=config.invocation_id_supplier(),
                author=config.name,
                content=config.fallback_content,
            ),
        )

    def emit(outcome: Place[Any], boundary: Place[Any]) -> Action:
        def run(ctx: Ctx) -> None:
            ctx.input(TURN_ACTIVE)
            ctx.input(CONVERSATION)
            ctx.output(boundary, ctx.input(outcome))
            ctx.signal(C.TURN_PERMIT)

        return run

    def abort_turn(ctx: Ctx) -> None:
        ctx.input(C.TURN_ABORT)
        ctx.input(TURN_ACTIVE)
        ctx.signal(C.TURN_PERMIT)

    def drop_abort(ctx: Ctx) -> None:
        ctx.input(C.TURN_ABORT)  # no turn in flight: nothing to abort

    return {
        Transitions.START_TURN: start_turn,
        Transitions.BUILD_PROMPT: build_prompt,
        Transitions.RE_ASK: re_ask,
        Transitions.RE_ASK_EXHAUSTED_FALLBACK: re_ask_exhausted,
        router.Transitions.ROUTE: router.route_action(router_config(config), HANDOFF, ANSWER),
        Transitions.EMIT_ANSWER: emit(ANSWER, C.EVENT_OUT),
        Transitions.EMIT_TRANSFER: emit(HANDOFF, C.TRANSFER),
        Transitions.ABORT_TURN: abort_turn,
        Transitions.DROP_ABORT: drop_abort,
    }


def action_bindings(llm: BaseLlm, config: Config) -> dict[str, Action]:
    """Full binding map: the step's and dispatch's bindings plus the agent's own."""
    return bind(
        DEF,
        merge(
            llm_step.action_bindings(llm, config.callbacks),
            tool_dispatch.action_bindings(config.tools, config.tool_context_supplier),
            own_actions(config),
        ),
    )
