package org.libpetri.adk.demos;

import com.google.adk.events.Event;
import com.google.adk.events.EventActions;
import com.google.adk.sessions.BaseSessionService;
import com.google.adk.sessions.Session;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.UUID;
import org.libpetri.adk.runner.SessionCheckpointStore;
import org.libpetri.adk.runner.SessionKey;
import org.libpetri.core.Token;

/**
 * Exemplar: checkpoints a session's marking into ADK's own session history,
 * as the {@code agentState} of an event (ADK 1.10's resumability field), so
 * the resume data lives where ADK keeps the rest of the session and goes
 * wherever its {@link BaseSessionService} persists to.
 *
 * <p>Thin user code, not library: what a token value turns into is the
 * caller's decision, made by the {@link Codec}. {@code agentState} is a JSON
 * map, so a durable session service needs JSON-friendly encodings.
 *
 * <p>Like any {@link SessionCheckpointStore}, it is written at session end
 * and read before a runner starts, never during execution, so ADK's session
 * stays a write-only legacy bridge as far as a running net is concerned
 * (design commitment 2).
 */
public final class AgentStateCheckpointStore implements SessionCheckpointStore {

    /** The {@code agentState} key the marking is stored under. */
    public static final String MARKING_KEY = "adk-libpetri.marking";

    /** Turns one place's token values into JSON-friendly values and back. */
    public interface Codec {
        Object encode(String place, Object value);

        Object decode(String place, Object encoded);
    }

    private final BaseSessionService sessions;
    private final String author;
    private final Codec codec;

    public AgentStateCheckpointStore(BaseSessionService sessions, String author, Codec codec) {
        this.sessions = Objects.requireNonNull(sessions, "sessions");
        this.author = Objects.requireNonNull(author, "author");
        this.codec = Objects.requireNonNull(codec, "codec");
    }

    @Override
    public void save(SessionKey key, Map<String, List<Token<?>>> marking) {
        var encoded = new LinkedHashMap<String, Object>();
        marking.forEach((place, tokens) -> {
            var list = new ArrayList<Map<String, Object>>();
            for (Token<?> token : tokens) {
                var entry = new LinkedHashMap<String, Object>();
                entry.put("value", codec.encode(place, token.value()));
                entry.put("createdAt", token.createdAt().toString());
                list.add(entry);
            }
            encoded.put(place, list);
        });
        var event = Event.builder()
                .id(UUID.randomUUID().toString())
                .invocationId("checkpoint-" + UUID.randomUUID())
                .author(author)
                .actions(EventActions.builder()
                        .agentState(Map.of(MARKING_KEY, encoded))
                        .build())
                .build();
        sessions.appendEvent(session(key), event).blockingGet();
    }

    @Override
    @SuppressWarnings("unchecked")
    public Optional<Map<String, List<Token<?>>>> load(SessionKey key) {
        var events = session(key).events();
        for (int i = events.size() - 1; i >= 0; i--) {
            var state = events.get(i).actions().agentState()
                    .map(s -> s.get(MARKING_KEY))
                    .orElse(null);
            if (state == null) continue;
            var marking = new LinkedHashMap<String, List<Token<?>>>();
            ((Map<String, List<Map<String, Object>>>) state).forEach((place, list) -> {
                var tokens = new ArrayList<Token<?>>();
                for (var entry : list) {
                    tokens.add(new Token<>(codec.decode(place, entry.get("value")),
                            Instant.parse((String) entry.get("createdAt"))));
                }
                marking.put(place, tokens);
            });
            return Optional.of(marking);
        }
        return Optional.empty();
    }

    private Session session(SessionKey key) {
        return sessions.getSession(key.appName(), key.userId(), key.sessionId(), Optional.empty())
                .blockingGet();
    }
}
