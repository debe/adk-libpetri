package org.libpetri.adk.runner;

import com.google.adk.sessions.Session;
import java.util.Objects;

/**
 * Identifier for a per-session executor instance — (appName, userId, sessionId).
 *
 * <p>Two sessions with the same identifier tuple share the same long-lived
 * {@link PetriRunner} in a {@link SessionExecutorRegistry}, preserving the
 * <b>one-net-per-user</b> invariant from the runtime model.
 */
public record SessionKey(String appName, String userId, String sessionId) {

    public SessionKey {
        Objects.requireNonNull(appName,   "appName");
        Objects.requireNonNull(userId,    "userId");
        Objects.requireNonNull(sessionId, "sessionId");
    }

    /** Derive from an ADK {@link Session}. */
    public static SessionKey from(Session session) {
        Objects.requireNonNull(session, "session");
        return new SessionKey(session.appName(), session.userId(), session.id());
    }
}
