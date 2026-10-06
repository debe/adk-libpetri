"""Port of Java ``RawProviderPassthroughDemoTest``.

Worked example for the **generic raw-provider passthrough** escape hatch:
when ADK's typed surface does not model a provider feature yet, the user
reaches it without forking ADK and without waiting for a release, while ADK
and the stock subnets stay the brain for everything they do model.

The library contributes **nothing** here, deliberately. The colours, the
records and the transition are all declared below, in user code. There is no
stock subnet, no SPI, and no shared ``RAW_PROVIDER_*`` colour in
``adk_libpetri.colours``: an opaque ``(feature, payload)`` pair on one global
place would be the state-bag shape the catalog exists to forbid, and two
unrelated escape hatches consuming it would each be enabled by the other's
token. Declare a place typed to *your* feature instead, as here.

The pattern::

    [VAD_FRAME]env --T_CallRaw--> [VAD_RESULT]
        the action calls the raw genai/transport API directly

In production ``VAD_FRAME`` is an env place injected with
``runner.inject(VAD_FRAME, frame)`` when the application needs the feature;
downstream transitions read ``VAD_RESULT`` and fold the result back into the
typed flow. Here the initial marking is seeded and the net runs to
quiescence, to keep the example deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

import libpetri as lp

from adk_libpetri import colours as C
from adk_libpetri._spec import Ctx, NetSpec, Place, TransitionSpec, one, out
from adk_libpetri.subnet import bind_composed


@dataclass(frozen=True, slots=True)
class VadFrame:
    """The feature-specific payload, typed. Not ``Any``: the escape hatch stays
    as typed as the feature allows, so the marking still says what it carries."""

    session_id: str
    audio: bytes


@dataclass(frozen=True, slots=True)
class VadResult:
    """The raw call's result, likewise typed to this feature."""

    session_id: str
    speech_detected: bool


VAD_FRAME: Place[VadFrame] = Place("demo.vadFrame", VadFrame)
"""Declared here, by the caller, for this feature only."""
VAD_RESULT: Place[VadResult] = Place("demo.vadResult", VadResult)

T_CALL_RAW = "T_CallRaw"


def call_raw_api(audio: bytes) -> bool:
    """Stand-in for the raw provider call the typed SDK does not expose yet (a
    new Live-API control frame, an experimental ``generate_content`` config
    field). In real code this is a direct genai or HTTP call; here it
    transforms the payload so the test can assert the round trip."""
    return len(audio) > 0


def call_raw(ctx: Ctx) -> None:
    """The user-supplied escape-hatch transition: typed frame in, raw call, typed result out."""
    frame = ctx.input(VAD_FRAME)
    ctx.output(VAD_RESULT, VadResult(frame.session_id, call_raw_api(frame.audio)))


async def test_raw_request_is_routed_through_a_user_transition_to_a_raw_event() -> None:
    spec = NetSpec(
        "raw-passthrough",
        (TransitionSpec(T_CALL_RAW, (one(VAD_FRAME),), out(VAD_RESULT)),),
    )
    net = bind_composed(spec, {T_CALL_RAW: call_raw})

    quiescent = await lp.run_async(
        net,
        initial={VAD_FRAME.name: [VadFrame("sess-1", bytes([1, 2, 3]))]},
        event_store=lp.InMemoryEventStore(),
    )

    results = list(quiescent.tokens(VAD_RESULT.name))
    assert results == [VadResult("sess-1", speech_detected=True)]
    # The request token was consumed: nothing is left on the inbound boundary.
    assert quiescent.count(VAD_FRAME.name) == 0


def test_the_catalog_carries_no_raw_provider_colour() -> None:
    """The escape hatch is user code by design: no bag-shaped raw colour ships."""
    exported = {name for name in dir(C) if not name.startswith("_")}
    assert not [n for n in exported if "RAW" in n.upper()]
