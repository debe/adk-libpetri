package org.libpetri.adk.colours;

import static com.google.common.truth.Truth.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.google.genai.types.Content;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.FunctionResponse;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * The wrapper colours copy what they wrap: a token's value cannot change
 * after it is produced, whatever the producer does with its own collection.
 */
class AdkColoursTest {

    @Test
    void tool_calls_copies_its_batch_and_allows_a_missing_model_turn() {
        var calls = new ArrayList<>(List.of(FunctionCall.builder().name("lookup").build()));
        var token = new AdkColours.ToolCalls(calls);
        calls.clear();

        assertThat(token.calls()).hasSize(1);
        assertThat(token.modelTurn()).isNull();
        assertThrows(UnsupportedOperationException.class, () -> token.calls().clear());
        assertThrows(NullPointerException.class, () -> new AdkColours.ToolCalls(null));
    }

    @Test
    void legacy_session_write_copies_in_order_and_keeps_null_values() {
        var delta = new LinkedHashMap<String, Object>();
        delta.put("b", 1);
        delta.put("a", null);   // Map.copyOf would reject this
        var token = new AdkColours.LegacySessionWrite(delta);
        delta.put("c", 3);

        assertThat(token.delta().keySet()).containsExactly("b", "a").inOrder();
        assertThat(token.delta()).containsEntry("a", null);
        assertThrows(UnsupportedOperationException.class, () -> token.delta().put("d", 4));
        assertThrows(NullPointerException.class, () -> new AdkColours.LegacySessionWrite(null));
    }

    @Test
    void transfer_target_requires_a_name() {
        assertThrows(NullPointerException.class, () -> new AdkColours.TransferTarget(null));
    }

    @Test
    void tool_results_copies_its_results() {
        var results = new ArrayList<FunctionResponse>();
        var token = new AdkColours.ToolResults(results, Content.fromParts());
        results.add(FunctionResponse.builder().name("x").build());

        assertThat(token.results()).isEmpty();
    }
}
