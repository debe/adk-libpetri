"""The live-connection contract the BIDI bridge needs (``@experimental``).

ADK's ``BaseLlmConnection.receive()`` yields ``LlmResponse``s. ADK Python
2.11 maps more of a server frame onto them than ADK Java did (voice-activity
edges included), but a net may still want the raw provider frame.
:meth:`LiveConnection.raw_receive` exposes it so a bridge consumer decodes
what its net cares about and injects it through env places.

The library ships no implementation: the wire binding is SDK-specific. See
the ``GenaiLiveConnection`` demo exemplar.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from google.genai import types

from .._experimental import experimental


@experimental
@runtime_checkable
class LiveConnection(Protocol):
    async def send_content(self, content: types.Content) -> None: ...
    async def send_realtime(self, blob: types.Blob) -> None: ...
    def raw_receive(self) -> AsyncIterator[Any]: ...
    async def close(self) -> None: ...
