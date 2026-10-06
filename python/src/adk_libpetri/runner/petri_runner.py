"""Per-session handle around a long-lived libpetri executor.

One ``PetriRunner`` = one session's net = one executor on the caller's
:class:`~adk_libpetri._aio.OrchestratorLoop`. Built once at session start,
kept alive across many user messages, closed when the session ends.

**Interaction model** (commitment 1). Two surfaces, deliberately asymmetric:

* *Ingress (generic)*: declare typed env places with
  :meth:`Builder.environment_place` and :meth:`inject` from any thread. This
  is the only way external producers hand tokens to the net: chat content,
  scroll events, sensor readings, webhooks.
* *Egress (narrow, ADK-specific)*: :meth:`adk_events` is a hot stream
  hard-wired to ``EVENT_OUT``, the ADK ``Runner`` contract bridge, not a
  general observation API. Observe through the event-store chain
  (:meth:`Builder.event_store`) or model egress as an in-net transition.

**Where actions run.** On libpetri's Tokio threads, with no asyncio loop;
stock actions hop ADK coroutines to the orchestrator loop (``on_loop``).

**Lifecycle.** :meth:`Builder.start` (or ``await Builder.astart()``) starts
the executor on the orchestrator loop. :meth:`drain` stops accepting injects
and returns at once; :meth:`wait_closed` / :meth:`await_termination` wait
for the run to end; :meth:`close` drains and waits.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import contextlib
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import timedelta
from typing import Any

import libpetri as lp
from google.adk.events.event import Event

from .. import colours as C
from .._aio import HotStream, OrchestratorLoop, await_on
from .._experimental import experimental
from .._spec import NetSpec, Place
from ..bridge import EventStoreToStreamBridge, TransitionFailure
from .checkpoint_store import Checkpoint, SessionCheckpointStore
from .session_key import SessionKey

log = logging.getLogger("adk_libpetri.runner")

QUIESCENT = "quiescent"


class _NoopStore:
    """Innermost store of the default chain: records nothing."""

    def is_enabled(self) -> bool:
        return True

    def append(self, event: Any) -> None:
        pass

    def events(self, **_: Any) -> list[Any]:
        return []


class _LoudFailures:
    """Logs every action failure: a failed transition consumes its tokens and
    produces nothing (EXEC-031), and on a non-recording chain that would be a
    silent hole in the marking (commitment 2)."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    @property
    def captures_tokens(self) -> Any:
        return getattr(self._delegate, "captures_tokens", False)

    def is_enabled(self) -> bool:
        return True

    def append(self, event: Any) -> None:
        if event.type == "TransitionFailed":
            log.warning(
                "Transition '%s' action failed; its consumed tokens are lost "
                "(libpetri EXEC-031). The orchestrator continues. %s",
                event.transition_name,
                event.payload().get("error", ""),
            )
        self._delegate.append(event)

    def events(self, **filters: Any) -> list[Any]:
        return self._delegate.events(**filters)


class HandleRef:
    """Filled with the running executor's handle when the runner starts.

    Streaming subnets whose actions inject onto an env place of their own net
    need the handle, which exists only after start. Pass the same ref to the
    subnet config and to :meth:`Builder.handle_ref` (Java ``executorRef``).
    """

    def __init__(self) -> None:
        self._handle: lp.ExecutorHandle | None = None

    def set(self, handle: lp.ExecutorHandle) -> None:
        self._handle = handle

    def get(self) -> lp.ExecutorHandle:
        if self._handle is None:
            raise RuntimeError("HandleRef used before its runner started")
        return self._handle

    @property
    def is_set(self) -> bool:
        return self._handle is not None


def _name(p: Place[Any] | str) -> str:
    return p if isinstance(p, str) else p.name


class PetriRunner:
    def __init__(
        self,
        *,
        net: lp.BuiltNet,
        handle: lp.ExecutorHandle,
        done: cf.Future[lp.MarkingView],
        loop: OrchestratorLoop,
        env_places: frozenset[str],
        bridge: EventStoreToStreamBridge,
        checkpoint_excludes: frozenset[str],
        spec: NetSpec | None,
    ) -> None:
        self._net = net
        self._handle = handle
        self._done = done
        self._loop = loop
        self._env = env_places
        self._bridge = bridge
        self._excludes = checkpoint_excludes
        self._spec = spec
        self.attachments: dict[str, Any] = {}
        """Per-session objects that live as long as this runner (e.g. a turn scope)."""
        self._drain_hooks: list[Callable[[], None]] = []

    @staticmethod
    def builder(net: lp.BuiltNet | NetSpec, actions: Mapping[str, Any] | None = None) -> Builder:
        """Start building a runner for ``net``.

        Pass a built ``libpetri.Net``, or a :class:`NetSpec` plus its
        ``actions`` (checked, then built).
        """
        if isinstance(net, NetSpec):
            if actions is None:
                raise TypeError("a NetSpec needs its actions: builder(spec, actions)")
            return Builder(net.build(actions), net, dict(actions))
        if actions is not None:
            raise TypeError("actions are only taken with a NetSpec")
        return Builder(net, None, None)

    # -- ingress -----------------------------------------------------------

    def declares_environment_place(self, place: Place[Any] | str) -> bool:
        return _name(place) in self._env

    def inject(self, place: Place[Any] | str, token: Any) -> bool:
        """Inject ``token`` onto a declared env place. Thread-safe, never blocks.

        Returns ``False`` once the runner is draining or closed.
        """
        name = _name(place)
        if name not in self._env:
            raise ValueError(
                f"Place {name!r} was not declared as an env place on this PetriRunner. "
                f"Add .environment_place({name!r}) to the builder."
            )
        if isinstance(place, Place) and token is None and not place.is_unit:
            raise TypeError(f"inject(None) onto non-unit place {name!r}; use signal() for units")
        return self._handle.inject(name, token)

    def inject_many(self, place: Place[Any] | str, tokens: Iterable[Any]) -> bool:
        name = _name(place)
        if name not in self._env:
            raise ValueError(f"Place {name!r} was not declared as an env place")
        return self._handle.inject_many(name, list(tokens))

    def signal(self, place: Place[None] | str) -> bool:
        """Inject the unit token onto a unit (``Void``) signal place."""
        name = _name(place)
        if name not in self._env:
            raise ValueError(f"Place {name!r} was not declared as an env place")
        return self._handle.inject(name, None)

    # -- egress ------------------------------------------------------------

    def adk_events(self) -> HotStream[Event]:
        """Hot stream of ``Event`` tokens produced into ``EVENT_OUT``.

        Late subscribers miss earlier events: subscribe before you inject.
        """
        return self._bridge.stream()

    def failure_signal(self) -> HotStream[TransitionFailure]:
        """Hot stream of transition failures; completes only with the net.

        A failure does not end :meth:`adk_events`: libpetri contains an action
        failure to its transition (EXEC-031) and this runner keeps egress alive
        to match. A caller waiting for a turn's terminal event merges this for
        the turn's life (``PetriAgent`` does). For observability use the
        event-store chain instead.
        """
        return self._bridge.failure_signal()

    # -- lifecycle ---------------------------------------------------------

    @property
    def handle(self) -> lp.ExecutorHandle:
        return self._handle

    @property
    def net(self) -> lp.BuiltNet:
        return self._net

    @property
    def spec(self) -> NetSpec | None:
        return self._spec

    @property
    def loop(self) -> OrchestratorLoop:
        return self._loop

    @property
    def closed(self) -> bool:
        return self._done.done()

    def on_drain(self, hook: Callable[[], None]) -> None:
        """Call ``hook`` once, when the run starts to drain (or is killed).

        For an action that waits on something outside the net (a turn that may
        never come): the hook releases it, so the drain does not wait forever.
        """
        self._drain_hooks.append(hook)

    def _run_drain_hooks(self) -> None:
        hooks, self._drain_hooks = self._drain_hooks, []
        for hook in hooks:
            try:
                hook()
            except Exception:
                log.exception("A drain hook of a PetriRunner failed")

    def drain(self) -> bool:
        """Stop accepting injects; in-flight work finishes. Returns at once."""
        accepted = self._handle.drain()
        self._run_drain_hooks()
        return accepted

    def kill(self) -> bool:
        """Close at once: no drain, in-flight work is abandoned."""
        self._run_drain_hooks()
        return self._handle.close()

    def await_termination(self, timeout: timedelta | float | None = None) -> bool:
        """Block up to ``timeout`` for the run to end; ``True`` if it did."""
        secs = timeout.total_seconds() if isinstance(timeout, timedelta) else timeout
        try:
            self._done.result(secs)
            return True
        except cf.TimeoutError:
            return False
        except Exception:
            return True

    async def wait_closed(self) -> lp.MarkingView:
        """Await the end of the run from any loop; returns the final marking."""
        return await asyncio.wrap_future(self._done)

    def close(self) -> None:
        """Drain, then block until the run ends."""
        self.drain()
        self.await_termination(None)

    async def aclose(self) -> None:
        self.drain()
        with contextlib.suppress(Exception):
            await self.wait_closed()

    def __enter__(self) -> PetriRunner:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    async def __aenter__(self) -> PetriRunner:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    @property
    def termination_reason(self) -> str | None:
        return self._handle.termination_reason if self.closed else None

    @experimental
    async def snapshot(self) -> lp.SnapshotResult:
        """The running net's marking. Only ``is_restore_point`` results are safe
        to restore from: mid-action, consumed tokens are in no place."""

        async def take() -> lp.SnapshotResult:
            return await self._handle.snapshot()

        return await await_on(take(), self._loop.loop)

    def checkpoint_marking(self) -> Checkpoint | None:
        """The marking a checkpoint saves, or ``None`` if the run did not end
        quiescent (still running, stopped, ended at a terminal place)."""
        if not self._done.done() or self._done.exception() is not None:
            return None
        final = self._done.result()
        if final.termination_reason != QUIESCENT:
            return None
        snap = final.snapshot()
        return {p: ts for p, ts in snap.items() if p not in self._excludes}


class Builder:
    def __init__(
        self, net: lp.BuiltNet, spec: NetSpec | None, actions: dict[str, Any] | None
    ) -> None:
        self._net = net
        self._spec = spec
        self._actions = actions
        self._places = {p.name for p in net.places}
        self._env: dict[str, None] = {}
        self._event_store: Any = None
        self._loop: OrchestratorLoop | None = None
        self._initial: dict[str, list[Any]] = {}
        self._restore: Checkpoint | None = None
        self._restore_is_checkpoint = False
        self._excludes: set[str] = {C.EVENT_OUT.name}
        self._scope: str | None = None
        self._clock: Any = None
        self._tolerance_ms: float | None = None
        self._handle_ref: HandleRef | None = None
        self._watch: asyncio.Task[None] | None = None

    def environment_place(self, place: Place[Any] | str) -> Builder:
        name = _name(place)
        if name in self._env:
            raise ValueError(f"Place {name!r} already declared as an env place")
        self._env[name] = None
        return self

    def environment_places(self, *places: Place[Any] | str) -> Builder:
        for p in places:
            self.environment_place(p)
        return self

    def event_store(self, store: Any) -> Builder:
        """The observability chain inside the runner (OTel, logging, in-memory).

        The runner always wraps it with the ``EVENT_OUT`` egress bridge.
        """
        self._event_store = store
        return self

    def orchestrator(self, loop: OrchestratorLoop) -> Builder:
        """Required: the long-lived loop the executor starts on (one per process)."""
        self._loop = loop
        return self

    def initial_marking(self, marking: Mapping[Any, Iterable[Any]]) -> Builder:
        self._initial = {_name(p): list(ts) for p, ts in marking.items()}
        return self

    def handle_ref(self, ref: HandleRef) -> Builder:
        self._handle_ref = ref
        return self

    @experimental
    def restore(self, marking: Checkpoint) -> Builder:
        """Resume from a snapshot marking instead of an initial marking.

        Timers restart. Pair with a fresh :meth:`execution_scope`.
        """
        self._restore = marking
        self._restore_is_checkpoint = False
        return self

    @experimental
    def resume_from(self, store: SessionCheckpointStore, key: SessionKey) -> Builder:
        """Resume from ``key``'s latest checkpoint, if any (it beats the initial marking)."""
        m = store.load(key)
        if m is not None:
            self.restore(m)
            self._restore_is_checkpoint = True
            self.execution_scope(f"resume-{uuid.uuid4()}")
        return self

    @experimental
    def exclude_from_checkpoint(self, *places: Place[Any] | str) -> Builder:
        """Leave egress places nothing consumes out of checkpoints (``EVENT_OUT`` always)."""
        self._excludes.update(_name(p) for p in places)
        return self

    @experimental
    def execution_scope(self, scope: str) -> Builder:
        """Scope of minted ν-names; a resumed run needs one its earlier runs did not use."""
        self._scope = scope
        return self

    def clock(self, clock: Any) -> Builder:
        """An injectable libpetri clock (``SteppedClock``) for tests."""
        self._clock = clock
        return self

    def deadline_tolerance(self, tolerance: timedelta) -> Builder:
        """Slack before a missed deadline counts (libpetri: 5 ms; virtual clock: 0)."""
        self._tolerance_ms = tolerance.total_seconds() * 1000
        return self

    # -- start -------------------------------------------------------------

    def _seed(self) -> dict[str, list[Any]] | lp.MarkingView:
        if self._restore is not None:
            if self._initial and not self._restore_is_checkpoint:
                raise ValueError(
                    "restore(...) and initial_marking(...) are exclusive: a restored run "
                    "resumes from the snapshot's marking"
                )
            return lp.MarkingView.from_snapshot(self._restore)
        seed = dict(self._initial)
        if C.TURN_PERMIT.name in self._places and C.TURN_PERMIT.name not in seed:
            seed[C.TURN_PERMIT.name] = [None]
        suffix = "/" + C.TURN_PERMIT.name
        for name in self._places:
            if name.endswith(suffix) and name not in seed:
                raise ValueError(
                    f"Place {name} is the turn permit of an instantiated agent and needs one "
                    f"token to run its first turn. Add it to initial_marking(...): "
                    f"{{{name!r}: [None]}}."
                )
        return seed

    def _options(self, env: list[str]) -> Any:
        kwargs: dict[str, Any] = {"environment_places": tuple(env)}
        if self._scope is not None:
            kwargs["execution_scope"] = self._scope
        if self._tolerance_ms is not None:
            kwargs["deadline_tolerance_ms"] = self._tolerance_ms
        if self._clock is not None:
            kwargs["clock"] = self._clock
        return lp.ExecutorOptions(**kwargs)

    def _prepare(self) -> tuple[Any, Any, EventStoreToStreamBridge, list[str]]:
        if self._loop is None:
            raise ValueError("orchestrator(...) must be set")
        seed = self._seed()
        env = list(self._env)
        if C.TURN_ABORT.name in self._places and C.TURN_ABORT.name not in self._env:
            env.append(C.TURN_ABORT.name)
        primary = self._event_store if self._event_store is not None else _NoopStore()
        bridge = EventStoreToStreamBridge(C.EVENT_OUT, _LoudFailures(primary))
        return seed, self._options(env), bridge, env

    async def _start_on_loop(
        self, seed: Any, options: Any, bridge: EventStoreToStreamBridge
    ) -> tuple[lp.ExecutorHandle, cf.Future[lp.MarkingView]]:
        handle, awaitable = lp.start_async(
            self._net,
            initial=seed,
            options=options,
            event_store=bridge,
        )
        # Before anything awaits: a request seeded in the initial marking can
        # fire a streaming action that needs the handle (Java sets its ref
        # before run(), too).
        if self._handle_ref is not None:
            self._handle_ref.set(handle)
        done: cf.Future[lp.MarkingView] = cf.Future()
        loop = asyncio.get_running_loop()

        async def watch() -> None:
            # ExecutionCompleted completes the bridge's streams; an error ends them here.
            try:
                done.set_result(await awaitable)
            except BaseException as err:
                done.set_exception(err)
                bridge.stream().error(err)
                bridge.failure_signal().complete()

        self._watch = loop.create_task(watch())
        return handle, done

    def _finish(
        self,
        handle: lp.ExecutorHandle,
        done: cf.Future[lp.MarkingView],
        bridge: EventStoreToStreamBridge,
        env: list[str],
    ) -> PetriRunner:
        assert self._loop is not None
        return PetriRunner(
            net=self._net,
            handle=handle,
            done=done,
            loop=self._loop,
            env_places=frozenset(env),
            bridge=bridge,
            checkpoint_excludes=frozenset(self._excludes),
            spec=self._spec,
        )

    def start(self) -> PetriRunner:
        """Start on the orchestrator loop, blocking the calling thread until started.

        From async code prefer ``await astart()``.
        """
        seed, options, bridge, env = self._prepare()
        assert self._loop is not None
        if self._loop.on_thread:
            raise RuntimeError("Builder.start() on the orchestrator thread; use await astart()")
        handle, done = self._loop.call(self._start_on_loop(seed, options, bridge))
        return self._finish(handle, done, bridge, env)

    async def astart(self) -> PetriRunner:
        seed, options, bridge, env = self._prepare()
        assert self._loop is not None
        handle, done = await self._loop.run(self._start_on_loop(seed, options, bridge))
        return self._finish(handle, done, bridge, env)
