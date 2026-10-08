"""ADK's ``AgentLoader`` for ``adk-libpetri web``: the Petri builder, and traced nets.

Three changes to what ADK's loader returns, nothing else:

* the dev UI's builder assistant (``__adk_agent_builder_assistant``) is
  :func:`~adk_libpetri.web.builder.create_petri_builder_assistant`;
* every ``PetriNet`` and ``PetriWorkflow`` in a loaded app (the root, and
  each one mounted under it) records its sessions' firings and markings in
  the server's :class:`~adk_libpetri.bridge.marking_trace.MarkingTraces`,
  wrapping whatever event store it was given;
* a package folder that defines no agent (a helper package of ``.agent.fn``
  refs, say: an ``__init__.py`` and an ``agent.py`` of functions, no
  ``root_agent.yaml``) is not listed as an app. ADK lists every folder with
  an ``agent.py``, and picking such a helper in the dev UI fails.

It is ADK's ``NestedAgentLoader``, the one stock ``adk web`` uses: apps in
sub-folders (``group/app``, listed as ``group.app``) are found as there.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any

from google.adk.cli.utils._nested_agent_loader import NestedAgentLoader

from .._experimental import experimental
from .._net_node import NetNodeBase
from ..bridge.marking_trace import MarkingTraces
from .builder import ADK_ASSISTANT, create_petri_builder_assistant


def net_nodes(root: Any) -> list[NetNodeBase]:
    """Every net node reachable from ``root`` (an ``App`` or a node), once each."""
    found: list[NetNodeBase] = []
    seen: set[int] = set()
    stack = [getattr(root, "root_agent", root)]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, NetNodeBase):
            found.append(node)
        for item in getattr(node, "nodes", None) or ():
            stack.extend(item if isinstance(item, list | tuple) else (item,))
        stack.extend(getattr(node, "sub_agents", None) or ())
        graph = getattr(node, "graph", None)
        stack.extend(getattr(graph, "adk_nodes", {}).values())
    return found


def trace(root: Any, traces: MarkingTraces) -> None:
    """Have every net node under ``root`` record into ``traces`` (once, whatever its store)."""
    for node in net_nodes(root):
        current = node._event_store
        if current is traces or isinstance(current, _Chained):
            continue
        node._event_store = traces if current is None else _Chained(traces, current)


class _Chained:
    """The server's traces in front of a node's own event store."""

    def __init__(self, traces: MarkingTraces, own: Any) -> None:
        self._traces = traces
        self._own = own

    def for_session(self, key: Any, initial: Any = None) -> Any:
        per_session = getattr(self._own, "for_session", None)
        own = per_session(key, initial) if callable(per_session) else self._own
        return self._traces.for_session(key, initial, delegate=own)


_DEFINES_AGENT = re.compile(r"\broot_agent\b|^\s*app\s*[:=]", re.MULTILINE)


def defines_agent(folder: Path) -> bool:
    """Whether a package folder can hold an app: a ``root_agent.yaml``, or Python naming one.

    ADK loads ``root_agent`` (or ``app``) from the package or its ``agent``
    module, which may be a package of its own (``agent/__init__.py``, with
    the agent in any module beside it). A folder whose ``__init__.py``,
    ``agent.py`` and ``agent/*.py`` never mention either cannot be loaded as
    an app. In doubt (a file that cannot be read), it is an app: a helper
    listed by mistake fails to load, an app hidden by mistake is lost.
    """
    if (folder / "root_agent.yaml").is_file():
        return True
    if not (folder / "__init__.py").is_file():
        return True  # ADK's other kinds: its own call
    sub = folder / "agent"
    candidates = [folder / "__init__.py", folder / "agent.py"]
    if (sub / "__init__.py").is_file():
        candidates += sorted(sub.glob("*.py"))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            return True
        if _DEFINES_AGENT.search(text):
            return True
    return False


@experimental
class PetriAgentLoader(NestedAgentLoader):
    """ADK's dev-UI loader, with the Petri builder assistant and traced nets."""

    @staticmethod
    def _is_valid_agent_dir(path: Path) -> bool:
        return NestedAgentLoader._is_valid_agent_dir(path) and defines_agent(path)

    def list_agents(self) -> list[str]:
        """ADK's nested listing, plus what its flat loader lists at the top: a package
        whose ``agent`` is itself a package (``agent/__init__.py``), which the nested
        listing (``agent.py`` or ``root_agent.yaml`` only) misses."""
        names = set(super().list_agents())
        if not self._is_single_agent:
            base = Path(self.agents_dir)
            with contextlib.suppress(OSError):
                for d in base.iterdir():
                    if (
                        d.is_dir()
                        and d.name.isidentifier()
                        and not d.name.startswith((".", "_"))
                        and (d / "__init__.py").is_file()
                        and defines_agent(d)
                    ):
                        names.add(d.name)
        return sorted(names)

    def __init__(self, agents_dir: str, traces: MarkingTraces | None = None) -> None:
        super().__init__(agents_dir)
        self.traces = traces if traces is not None else MarkingTraces()

    def _perform_load(self, agent_path: str) -> Any:
        if agent_path == ADK_ASSISTANT:
            self._validate_agent_name(agent_path)
            return create_petri_builder_assistant()
        loaded = super()._perform_load(agent_path)
        trace(loaded, self.traces)
        return loaded


__all__ = ["PetriAgentLoader", "defines_agent", "net_nodes", "trace"]
