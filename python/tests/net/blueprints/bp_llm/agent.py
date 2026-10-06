"""The tool the helper agent offers."""

from __future__ import annotations


def get_weather(city: str) -> dict[str, str]:
    """The weather in a city."""
    return {"city": city, "forecast": "sunny"}
