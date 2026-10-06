"""Lazy ``SessionKey -> PetriRunner`` map: one net per user (commitment 8).

Two ownership modes:

* :meth:`SessionExecutorRegistry.strong_owned` (**the default**). Entries live
  until :meth:`close` / :meth:`close_all` from your session-end hook. A
  forgotten close is a *visible* leak: :meth:`size` grows.
* :meth:`SessionExecutorRegistry.finalizer_owned` (opt-in; Java
  ``cleanerOwned``). Each runner is bound to a caller-supplied *lifetime
  owner* through ``weakref.finalize``; when the owner is collected the runner
  is torn down. Its wrong-owner failure is invisible (silent teardown), so
  use it only with an owner whose collection genuinely tracks session end:
  a websocket connection held until disconnect, or a value from your own
  per-session map. **Not** ADK's ``Session`` under ``InMemorySessionService``,
  which hands out deep copies.

Owner identity (``is``) is load-bearing in both modes: it de-duplicates
concurrent first calls, and a different owner for a known key raises.

**Checkpointing** (``@experimental``). With a :class:`SessionCheckpointStore`
the registry saves a session's final marking at teardown. Teardown drains
first; until the save is done the key stays taken, so a replacement runner
resumes from what the old one left and a key never has two serving runners.
A run that does not drain quiescent within the timeout has its checkpoint
removed rather than left stale. :meth:`discard` ends a session unsaved.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import inspect
import logging
import threading
import weakref
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Literal

from .._experimental import experimental
from .checkpoint_store import SessionCheckpointStore
from .petri_runner import PetriRunner
from .session_key import SessionKey

log = logging.getLogger("adk_libpetri.registry")

DEFAULT_CHECKPOINT_TIMEOUT = timedelta(seconds=30)

Factory = Callable[[SessionKey], "PetriRunner | Awaitable[PetriRunner]"]


class _NoOwner:
    def __repr__(self) -> str:
        return "<no owner>"


NO_OWNER: Any = _NoOwner()


class _Live:
    __slots__ = ("__weakref__", "owner_ref", "runner")

    def __init__(self, runner: PetriRunner, owner_ref: Callable[[], Any]) -> None:
        self.runner = runner
        self.owner_ref = owner_ref


class _Closing:
    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done: cf.Future[None] = cf.Future()


class SessionExecutorRegistry:
    def __init__(
        self,
        mode: Literal["strong", "finalizer"],
        checkpoints: SessionCheckpointStore | None = None,
        checkpoint_timeout: timedelta = DEFAULT_CHECKPOINT_TIMEOUT,
    ) -> None:
        self._mode = mode
        self._checkpoints = checkpoints
        self._timeout = checkpoint_timeout
        self._slots: dict[SessionKey, _Live | _Closing] = {}
        self._lock = threading.RLock()

    @classmethod
    def strong_owned(
        cls,
        checkpoints: SessionCheckpointStore | None = None,
        checkpoint_timeout: timedelta = DEFAULT_CHECKPOINT_TIMEOUT,
    ) -> SessionExecutorRegistry:
        return cls("strong", checkpoints, checkpoint_timeout)

    @classmethod
    def finalizer_owned(
        cls,
        checkpoints: SessionCheckpointStore | None = None,
        checkpoint_timeout: timedelta = DEFAULT_CHECKPOINT_TIMEOUT,
    ) -> SessionExecutorRegistry:
        return cls("finalizer", checkpoints, checkpoint_timeout)

    @property
    def is_finalizer_owned(self) -> bool:
        return self._mode == "finalizer"

    # -- lookup ------------------------------------------------------------

    def get(self, key: SessionKey) -> PetriRunner | None:
        """The serving runner, or ``None`` (one being torn down is not serving)."""
        with self._lock:
            slot = self._slots.get(key)
        return slot.runner if isinstance(slot, _Live) else None

    def size(self) -> int:
        with self._lock:
            return sum(isinstance(s, _Live) for s in self._slots.values())

    def __len__(self) -> int:
        return self.size()

    def get_or_create(
        self, key: SessionKey, factory: Factory, owner: Any = NO_OWNER
    ) -> PetriRunner:
        """The runner for ``key``, created by ``factory`` on first call (sync).

        Waits for a teardown of ``key`` in progress, so a resuming factory
        reads what the previous runner saved. ``factory`` must be sync here.
        """
        owner = self._check_owner(owner)
        while True:
            reused, closing = self._lookup(key, owner)
            if reused is not None:
                return reused
            if closing is not None:
                closing.done.result()
                continue
            created = factory(key)
            if inspect.isawaitable(created):
                raise TypeError("an async factory needs aget_or_create")
            won = self._install(key, owner, created)
            if won is not None:
                return won

    async def aget_or_create(
        self, key: SessionKey, factory: Factory, owner: Any = NO_OWNER
    ) -> PetriRunner:
        """:meth:`get_or_create` for async callers; ``factory`` may be ``async``."""
        owner = self._check_owner(owner)
        while True:
            reused, closing = self._lookup(key, owner)
            if reused is not None:
                return reused
            if closing is not None:
                await asyncio.wrap_future(closing.done)
                continue
            created = factory(key)
            if inspect.isawaitable(created):
                created = await created
            won = self._install(key, owner, created)
            if won is not None:
                return won

    def _check_owner(self, owner: Any) -> Any:
        if owner is None:
            raise ValueError("owner must not be None")
        if owner is NO_OWNER and self._mode == "finalizer":
            raise ValueError(
                "A finalizer_owned() registry needs a lifetime owner: its runner is torn "
                "down when the owner is collected. Pass one, or use strong_owned() and "
                "close(key) from your session-end hook."
            )
        if self._mode == "finalizer":
            # Checked before the factory runs: failing in _install would orphan
            # a started runner that no slot, and so no teardown, ever reaches.
            try:
                weakref.ref(owner)
            except TypeError:
                raise TypeError(
                    f"A finalizer_owned() owner must be weakly referenceable; "
                    f"{type(owner).__name__} is not (object(), str, int and tuple are not). "
                    "Use the session's own object (a websocket connection, a holder instance)."
                ) from None
        return owner

    def _lookup(self, key: SessionKey, owner: Any) -> tuple[PetriRunner | None, _Closing | None]:
        with self._lock:
            slot = self._slots.get(key)
            if isinstance(slot, _Closing):
                return None, slot
            if slot is None:
                return None, None
            original = slot.owner_ref()
            if original is owner:
                return slot.runner, None
            if original is not None:
                self._owner_mismatch(key, original, owner)
            stale = slot  # owner collected: stale entry
        self._evict(key, stale)
        return None, None

    @staticmethod
    def _owner_mismatch(key: SessionKey, original: Any, candidate: Any) -> None:
        if original is NO_OWNER or candidate is NO_OWNER:
            first = "without an owner" if original is NO_OWNER else "with an owner"
            now = "without one" if candidate is NO_OWNER else "with one"
            raise RuntimeError(
                f"SessionKey {key} was first requested {first} and is now requested {now}. "
                "Use one form per key: ownerless everywhere, or the same owner everywhere. "
                "A PetriAgent built without an owner_extractor uses the ownerless form."
            )
        raise RuntimeError(
            f"SessionKey {key} is already bound to a different lifetime owner. One owner per "
            "key, ever. Hold a stable identity for the session (a websocket connection, a "
            "value from your own per-session map). ctx.session is NOT stable under "
            "InMemorySessionService, which returns copies."
        )

    def _install(self, key: SessionKey, owner: Any, created: PetriRunner) -> PetriRunner | None:
        with self._lock:
            winner = self._slots.get(key)
            if winner is None:
                if self._mode == "finalizer":
                    live = _Live(created, weakref.ref(owner))
                    slot_ref = weakref.ref(live)
                    weakref.finalize(owner, self._on_owner_collected, key, slot_ref)
                else:
                    live = _Live(created, lambda o=owner: o)
                self._slots[key] = live
                return created
        created.drain()  # lost a creation race
        if isinstance(winner, _Live):
            reused, _ = self._lookup(key, owner)
            return reused
        return None

    # -- teardown ----------------------------------------------------------

    def _on_owner_collected(self, key: SessionKey, slot_ref: weakref.ref[_Live]) -> None:
        """Finalizer callback: may run on any thread, mid-GC. Never blocks."""
        live = slot_ref()
        if live is None:
            return
        closing = _Closing()
        if not self._replace(key, live, closing):
            return
        if self._checkpoints is None:
            self._settle(key, live.runner, closing, save=True)
            return
        threading.Thread(
            target=self._settle,
            args=(key, live.runner, closing, True),
            name=f"petri-checkpoint-{key.session_id}",
            daemon=True,
        ).start()

    def _replace(self, key: SessionKey, old: _Live, new: _Closing) -> bool:
        with self._lock:
            if self._slots.get(key) is old:
                self._slots[key] = new
                return True
            return False

    def close(self, key: SessionKey) -> bool:
        """Tear ``key``'s runner down (checkpointing it, if configured) and wait.

        ``True`` if this call tore a runner down.
        """
        return self._end(key, save=True)

    @experimental
    def discard(self, key: SessionKey) -> bool:
        """End a session for good: no save, and its stored checkpoint is removed."""
        return self._end(key, save=False)

    def close_all(self) -> None:
        with self._lock:
            keys = list(self._slots)
        for k in keys:
            self.close(k)

    async def aclose(self, key: SessionKey) -> bool:
        return await asyncio.to_thread(self.close, key)

    async def aclose_all(self) -> None:
        await asyncio.to_thread(self.close_all)

    def __enter__(self) -> SessionExecutorRegistry:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close_all()

    def _end(self, key: SessionKey, save: bool) -> bool:
        while True:
            closing = _Closing()
            with self._lock:
                slot = self._slots.get(key)
                if isinstance(slot, _Live):
                    self._slots[key] = closing
            if slot is None:
                if not save:
                    self._remove_checkpoint(key)
                return False
            if isinstance(slot, _Closing):
                slot.done.result()
                if save:
                    return False
                continue
            try:
                self._settle(key, slot.runner, closing, save)
            finally:
                slot.runner.drain()
                slot.runner.await_termination(None)
            return True

    def _evict(self, key: SessionKey, stale: _Live) -> None:
        closing = _Closing()
        if not self._replace(key, stale, closing):
            return
        try:
            self._settle(key, stale.runner, closing, save=True)
        finally:
            stale.runner.drain()

    def _settle(self, key: SessionKey, runner: PetriRunner, closing: _Closing, save: bool) -> None:
        try:
            runner.drain()
            if self._checkpoints is not None:
                if save:
                    self._save_final_marking(key, runner)
                else:
                    self._remove_checkpoint(key)
        finally:
            with self._lock:
                if self._slots.get(key) is closing:
                    del self._slots[key]
            closing.done.set_result(None)

    def _save_final_marking(self, key: SessionKey, runner: PetriRunner) -> None:
        assert self._checkpoints is not None
        if not runner.await_termination(self._timeout):
            log.warning(
                "Session %s did not drain within %s; not checkpointed, and its earlier "
                "checkpoint is removed.",
                key,
                self._timeout,
            )
            self._remove_checkpoint(key)
            return
        try:
            marking = runner.checkpoint_marking()
        except Exception:
            log.warning(
                "Session %s cannot be checkpointed; its earlier checkpoint is removed.",
                key,
                exc_info=True,
            )
            self._remove_checkpoint(key)
            return
        if marking is None:
            reason = runner.termination_reason
            level = logging.DEBUG if reason == "terminal" else logging.WARNING
            log.log(
                level,
                "Session %s ended %s, not quiescent after its drain, so there is no marking "
                "to resume from; its earlier checkpoint is removed.",
                key,
                reason,
            )
            self._remove_checkpoint(key)
            return
        try:
            self._checkpoints.save(key, marking)
        except Exception:
            log.warning(
                "Checkpointing session %s failed; its earlier checkpoint is removed.",
                key,
                exc_info=True,
            )
            self._remove_checkpoint(key)
        except BaseException:
            # Not swallowed, but the checkpoint it could not replace must not
            # outlive it as this session's last word (Java: catch Error).
            self._remove_checkpoint(key)
            raise

    def _remove_checkpoint(self, key: SessionKey) -> None:
        if self._checkpoints is None:
            return
        try:
            self._checkpoints.remove(key)
        except Exception:
            log.warning(
                "Removing the checkpoint of session %s failed; a later resume may "
                "restore an older marking.",
                key,
                exc_info=True,
            )
