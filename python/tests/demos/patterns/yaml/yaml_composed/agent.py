"""The node that joins the two races' answers into the assistant's question."""

from __future__ import annotations

from typing import Any

from google.adk.events.event import Event
from google.genai import types


def brief(node_input: Any) -> Any:
    """``{a1: Event, a2: Event}``: each race's winning answer, as one user message.

    The ``Content`` goes out as the event's ``output``: a FunctionNode that
    returns a ``Content`` makes it the event's content and has no output.
    """
    answers = [node_input[p].output.text for p in ("a1", "a2")]
    question = types.Content(
        role="user", parts=[types.Part(text="Summarize: " + " | ".join(answers))]
    )
    return Event(output=question)
