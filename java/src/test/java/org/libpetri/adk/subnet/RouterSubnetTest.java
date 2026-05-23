package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.events.Event;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.Part;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.event.EventStore;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.runtime.BitmapNetExecutor;

class RouterSubnetTest {

    // ============================================================
    //  Text-only responses → EVENT_OUT
    // ============================================================

    @Test
    void text_only_response_routes_to_event_out_wrapping_content() {
        var response = textResponse("hello world");
        var fixture = run(routerConfig("test-agent"), response);

        assertThat(fixture.events()).hasSize(1);
        assertThat(fixture.toolCalls()).isEmpty();
        assertThat(fixture.transfers()).isEmpty();

        var event = fixture.events().get(0);
        assertThat(event.author()).isEqualTo("test-agent");
        assertThat(event.invocationId()).isEqualTo("invocation-fixed");
        assertThat(event.content()).isPresent();
        assertThat(event.content().get().text()).isEqualTo("hello world");
    }

    @Test
    void empty_response_still_routes_to_event_out_with_null_content() {
        var response = LlmResponse.builder().build();
        var fixture = run(routerConfig("agent"), response);

        assertThat(fixture.events()).hasSize(1);
        assertThat(fixture.events().get(0).content()).isEmpty();
    }

    // ============================================================
    //  Function calls → TOOL_CALLS
    // ============================================================

    @Test
    void single_function_call_routes_to_tool_calls() {
        var call = FunctionCall.builder().name("get_weather").args(Map.of("city", "Oslo")).id("c1").build();
        var response = responseWithCalls(call);

        var fixture = run(routerConfig("agent"), response);

        assertThat(fixture.events()).isEmpty();
        assertThat(fixture.transfers()).isEmpty();
        assertThat(fixture.toolCalls()).hasSize(1);
        assertThat(fixture.toolCalls().get(0).calls()).containsExactly(call);
    }

    @Test
    void multiple_function_calls_bundle_into_one_tool_calls_token() {
        var c1 = FunctionCall.builder().name("a").args(Map.of()).build();
        var c2 = FunctionCall.builder().name("b").args(Map.of()).build();
        var response = responseWithCalls(c1, c2);

        var fixture = run(routerConfig("agent"), response);

        assertThat(fixture.toolCalls()).hasSize(1);
        assertThat(fixture.toolCalls().get(0).calls()).containsExactly(c1, c2).inOrder();
    }

    // ============================================================
    //  transfer_to_agent → TRANSFER (precedence over other calls)
    // ============================================================

    @Test
    void transfer_to_agent_call_routes_to_transfer_place() {
        var transferCall = FunctionCall.builder()
                .name(RouterSubnet.TRANSFER_TO_AGENT_FN)
                .args(Map.of(RouterSubnet.TRANSFER_AGENT_NAME_ARG, "billing"))
                .build();
        var response = responseWithCalls(transferCall);

        var fixture = run(routerConfig("router"), response);

        assertThat(fixture.toolCalls()).isEmpty();
        assertThat(fixture.events()).isEmpty();
        assertThat(fixture.transfers()).hasSize(1);
        assertThat(fixture.transfers().get(0).agentName()).isEqualTo("billing");
    }

    @Test
    void transfer_takes_precedence_over_sibling_function_calls() {
        var transferCall = FunctionCall.builder()
                .name(RouterSubnet.TRANSFER_TO_AGENT_FN)
                .args(Map.of(RouterSubnet.TRANSFER_AGENT_NAME_ARG, "sales"))
                .build();
        var siblingCall = FunctionCall.builder().name("other_tool").args(Map.of()).build();
        var response = responseWithCalls(siblingCall, transferCall);

        var fixture = run(routerConfig("router"), response);

        assertThat(fixture.transfers()).hasSize(1);
        assertThat(fixture.transfers().get(0).agentName()).isEqualTo("sales");
        assertThat(fixture.toolCalls()).isEmpty();
    }

    @Test
    void transfer_with_missing_agent_name_arg_yields_empty_string() {
        var bad = FunctionCall.builder()
                .name(RouterSubnet.TRANSFER_TO_AGENT_FN)
                .args(Map.of())  // missing agent_name
                .build();
        var fixture = run(routerConfig("router"), responseWithCalls(bad));

        assertThat(fixture.transfers()).hasSize(1);
        assertThat(fixture.transfers().get(0).agentName()).isEmpty();
    }

    // ============================================================
    //  Structural — XOR enforcement and topology
    // ============================================================

    @Test
    void each_fire_produces_to_exactly_one_xor_child() {
        // Three different responses, three different branches — assert
        // total tokens across (toolCalls + transfer + eventOut) equals 3.
        var text     = textResponse("hi");
        var withCall = responseWithCalls(FunctionCall.builder().name("t").args(Map.of()).build());
        var withXfer = responseWithCalls(FunctionCall.builder()
                .name(RouterSubnet.TRANSFER_TO_AGENT_FN)
                .args(Map.of(RouterSubnet.TRANSFER_AGENT_NAME_ARG, "x"))
                .build());

        var fixture = run(routerConfig("agent"), text, withCall, withXfer);

        assertThat(fixture.events()).hasSize(1);
        assertThat(fixture.toolCalls()).hasSize(1);
        assertThat(fixture.transfers()).hasSize(1);
    }

    @Test
    void subnet_def_declares_one_transition_and_four_ports() {
        assertThat(RouterSubnet.DEF.body().transitions().stream()
                .map(t -> t.name()).toList())
                .containsExactly(RouterSubnet.Transitions.ROUTE);

        var ports = RouterSubnet.DEF.iface().ports().stream()
                .map(p -> p.name()).sorted().toList();
        assertThat(ports).containsExactly(
                "eventOut", "llmResponse", "toolCalls", "transfer")
                .inOrder();
    }

    @Test
    void invocation_id_supplier_called_per_event_emission() {
        var counter = new AtomicInteger();
        var config = new RouterSubnet.Config("agent",
                () -> "inv-" + counter.incrementAndGet());

        var fixture = run(config, textResponse("a"), textResponse("b"));

        assertThat(fixture.events()).hasSize(2);
        var ids = fixture.events().stream().map(Event::invocationId).toList();
        assertThat(ids).containsExactly("inv-1", "inv-2");
    }

    // ============================================================
    //  Fixtures and helpers
    // ============================================================

    private static RouterSubnet.Config routerConfig(String author) {
        return new RouterSubnet.Config(author, () -> "invocation-fixed");
    }

    private static LlmResponse textResponse(String text) {
        return LlmResponse.builder()
                .content(Content.fromParts(Part.fromText(text)))
                .build();
    }

    private static LlmResponse responseWithCalls(FunctionCall... calls) {
        var parts = new ArrayList<Part>();
        for (var c : calls) parts.add(Part.builder().functionCall(c).build());
        return LlmResponse.builder()
                .content(Content.builder().role("model").parts(parts).build())
                .build();
    }

    private static Fixture run(RouterSubnet.Config config, LlmResponse... responses) {
        var net = PetriNet.builder("test")
                .compose(RouterSubnet.DEF)
                .build()
                .bindActions(RouterSubnet.actionBindings(config));

        List<Token<?>> tokens = new ArrayList<>();
        for (var r : responses) tokens.add(Token.of(r));
        Map<Place<?>, List<Token<?>>> initial = Map.of(AdkColours.LLM_RESPONSE, tokens);

        var executor = BitmapNetExecutor.builder(net, initial)
                .eventStore(EventStore.inMemory())
                .build();
        var marking = executor.run();

        return new Fixture(
                marking.peekTokens(AdkColours.EVENT_OUT).stream().map(Token::value).toList(),
                marking.peekTokens(AdkColours.TOOL_CALLS).stream().map(Token::value).toList(),
                marking.peekTokens(AdkColours.TRANSFER).stream().map(Token::value).toList());
    }

    private record Fixture(
            List<Event> events,
            List<AdkColours.ToolCalls> toolCalls,
            List<AdkColours.TransferTarget> transfers) {}
}
