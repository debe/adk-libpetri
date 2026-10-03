package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.sessions.InMemorySessionService;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.runner.SessionKey;
import org.libpetri.core.Token;

class AgentStateCheckpointStoreTest {

    /** Strings stay strings; unit tokens ({@code Void}) are null either way. */
    private static final AgentStateCheckpointStore.Codec CODEC = new AgentStateCheckpointStore.Codec() {
        @Override public Object encode(String place, Object value) { return value; }
        @Override public Object decode(String place, Object encoded) { return encoded; }
    };

    @Test
    void the_marking_round_trips_through_an_adk_session_event() {
        var sessions = new InMemorySessionService();
        var session = sessions.createSession("app", "user", (Map<String, Object>) null, "s1")
                .blockingGet();
        var key = SessionKey.from(session);
        var store = new AgentStateCheckpointStore(sessions, "agent", CODEC);

        assertThat(store.load(key)).isEmpty();

        var at = Instant.parse("2026-10-03T12:00:00Z");
        Map<String, List<Token<?>>> marking = Map.of(
                "notes", List.of(new Token<>("first", at), new Token<>("second", at.plusSeconds(1))),
                "budget", List.of(new Token<>(null, at), new Token<>(null, at)));
        store.save(key, marking);

        assertThat(store.load(key)).hasValue(marking);
        // It is an ordinary ADK event, carried in agentState.
        var stored = sessions.getSession("app", "user", "s1", java.util.Optional.empty())
                .blockingGet().events().getLast();
        assertThat(stored.actions().agentState().orElseThrow())
                .containsKey(AgentStateCheckpointStore.MARKING_KEY);
    }

    @Test
    void the_latest_checkpoint_wins() {
        var sessions = new InMemorySessionService();
        var session = sessions.createSession("app", "user", (Map<String, Object>) null, "s1")
                .blockingGet();
        var key = SessionKey.from(session);
        var store = new AgentStateCheckpointStore(sessions, "agent", CODEC);
        var at = Instant.parse("2026-10-03T12:00:00Z");

        store.save(key, Map.of("notes", List.of(new Token<>("old", at))));
        store.save(key, Map.of("notes", List.of(new Token<>("new", at))));

        assertThat(store.load(key).orElseThrow().get("notes").getFirst().value()).isEqualTo("new");
    }
}
