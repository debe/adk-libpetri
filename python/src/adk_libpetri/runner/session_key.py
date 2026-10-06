"""Identity of one ADK session's runner."""

from __future__ import annotations

from dataclasses import dataclass

from google.adk.sessions.session import Session


@dataclass(frozen=True, slots=True)
class SessionKey:
    """``(app_name, user_id, session_id)``, plus an optional ``scope``.

    ``scope`` tells apart several PetriAgents serving one session, as happens
    when two of them are nodes of one ADK ``Workflow``. Java has no such
    case, so it has no scope.
    """

    app_name: str
    user_id: str
    session_id: str
    scope: str = ""

    @staticmethod
    def of(session: Session, scope: str = "") -> SessionKey:
        return SessionKey(session.app_name, session.user_id, session.id, scope)
