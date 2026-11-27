"""Token types for the name tests: a nested path, a pydantic model, a ``str | None``
field, a ``Job | None`` step, a non-str field, an annotation only a type checker
resolves, typed ids, and an InitVar a copy cannot pass."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, NewType

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from decimal import Decimal


@dataclass(frozen=True)
class Job:
    id: str
    n: int = 0


@dataclass(frozen=True)
class Order:
    job: Job
    note: str | None = None


@dataclass(frozen=True)
class Held:
    """A name one step past an Optional: a ``None`` job has none."""

    job: Job | None = None


@dataclass(frozen=True)
class Seeded:
    """A str field next to an InitVar with no default, which ``replace`` cannot pass."""

    turn: str
    seed: InitVar[int]


class PJob(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str = ""


@dataclass(frozen=True)
class Priced:
    """``amount`` resolves only under ``TYPE_CHECKING``; ``turn`` still does."""

    turn: str
    amount: Decimal | None = None


class Sid(str):
    """A typed id: a str subclass."""


Tid = NewType("Tid", str)


class Lane(StrEnum):
    A = "a"


@dataclass(frozen=True)
class Typed:
    """Names typed four ways, and a str field a copy cannot set."""

    sid: Sid
    tid: Tid
    lane: Lane
    kind: Literal["a", "b"] = "a"
    stamp: str = field(default="", init=False)
