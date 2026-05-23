package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.models.LlmRequest;
import com.google.adk.tools.BaseTool;
import com.google.adk.tools.ToolContext;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Single;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.runtime.BitmapNetExecutor;

class PromptBuilderSubnetTest {

    @Test
    void minimal_config_produces_request_with_model_and_user_content() {
        var userContent = Content.fromParts(Part.fromText("hello"));
        var fixture = run(PromptBuilderSubnet.Config.of("fake-model"), userContent);

        assertThat(fixture).hasSize(1);
        var req = fixture.get(0);
        assertThat(req.model()).hasValue("fake-model");
        assertThat(req.contents()).hasSize(1);
        assertThat(req.contents().get(0).text()).isEqualTo("hello");
        assertThat(req.config()).isEmpty();
    }

    @Test
    void system_instruction_propagates_to_config() {
        var userContent = Content.fromParts(Part.fromText("hi"));
        var config = PromptBuilderSubnet.Config.builder("fake-model")
                .systemInstruction("You are a helpful assistant.")
                .build();

        var fixture = run(config, userContent);

        var req = fixture.get(0);
        assertThat(req.config()).isPresent();
        var sysInst = req.config().get().systemInstruction();
        assertThat(sysInst).isPresent();
        assertThat(sysInst.get().text()).isEqualTo("You are a helpful assistant.");
    }

    @Test
    void tools_map_propagates_to_request() {
        var tool = new BaseTool("noop", "no-op") {
            @Override public Single<Map<String, Object>> runAsync(Map<String, Object> a, ToolContext c) {
                return Single.just(Map.of());
            }
        };
        var config = PromptBuilderSubnet.Config.builder("fake-model")
                .tools(Map.of("noop", tool))
                .build();

        var fixture = run(config, Content.fromParts(Part.fromText("hi")));

        var req = fixture.get(0);
        assertThat(req.tools()).containsKey("noop");
    }

    @Test
    void empty_tools_map_does_not_appear_in_request() {
        var fixture = run(PromptBuilderSubnet.Config.of("fake-model"),
                Content.fromParts(Part.fromText("hi")));

        // The default LlmRequest.tools() returns an empty map either way; we just
        // confirm that an empty config doesn't accidentally add some sentinel.
        assertThat(fixture.get(0).tools()).isEmpty();
    }

    @Test
    void subnet_def_declares_exactly_one_transition_and_two_ports() {
        var transitions = PromptBuilderSubnet.DEF.body().transitions().stream()
                .map(t -> t.name()).toList();
        assertThat(transitions).containsExactly(PromptBuilderSubnet.Transitions.BUILD_PROMPT);

        var ports = PromptBuilderSubnet.DEF.iface().ports().stream()
                .map(p -> p.name()).sorted().toList();
        assertThat(ports).containsExactly("llmRequest", "userIn").inOrder();
    }

    // ============================================================
    //  Fixture
    // ============================================================

    private static List<LlmRequest> run(PromptBuilderSubnet.Config config, Content... initialUserMessages) {
        var net = PetriNet.builder("test")
                .compose(PromptBuilderSubnet.DEF)
                .build()
                .bindActions(PromptBuilderSubnet.actionBindings(config));

        List<Token<?>> initialTokens = new ArrayList<>();
        for (var c : initialUserMessages) initialTokens.add(Token.of(c));
        Map<Place<?>, List<Token<?>>> initial = Map.of(AdkColours.USER_IN, initialTokens);

        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(EventStore.inMemory())
                .build();
        var marking = executor.run();
        return marking.peekTokens(AdkColours.LLM_REQUEST).stream()
                .map(Token::value).toList();
    }
}
