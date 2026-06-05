package org.libpetri.adk.subnet;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;
import java.util.Collection;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import org.junit.jupiter.api.Test;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Token;
import org.libpetri.runtime.BitmapNetExecutor;

/**
 * Proves the stock {@link LlmStepSubnet} is instanceable on libpetri &gt;= 2.7.1
 * (MOD-031 &cap; CORE-042). The subnet keeps its <b>hardcoded-constant</b> actions
 * ({@code ctx.input(AdkColours.LLM_REQUEST)}, {@code ctx.output(Places.READY_TO_CALL, …)});
 * libpetri transparently resolves those declared places to the per-instance
 * bound/renamed places, and the alias survives {@code Instance.bindActions} (the
 * gap fixed in 2.7.1). Two instances with <b>distinct</b> bound places therefore
 * run in one net with no cross-talk via
 * {@code DEF.instantiate(prefix).bindActions(…)} + {@code compose(instance, bindPort(…))}.
 *
 * <p>Before MOD-031 (and, for the late-bindActions idiom, before 2.7.1) this path
 * threw {@code IllegalArgumentException: Place 'llmRequest' not in declared inputs}.
 */
class LlmStepSubnetInstancingTest {

    @Test
    void two_instances_with_distinct_bound_places_do_not_cross_talk() {
        Place<LlmRequest>  intentReq  = Place.of("intentReq",  LlmRequest.class);
        Place<LlmResponse> intentResp = Place.of("intentResp", LlmResponse.class);
        Place<LlmRequest>  guardReq   = Place.of("guardReq",   LlmRequest.class);
        Place<LlmResponse> guardResp  = Place.of("guardResp",  LlmResponse.class);

        var intentRequest  = request("intent-q");
        var guardRequest   = request("guard-q");
        var intentResponse = textResponse("intent-a");
        var guardResponse  = textResponse("guard-a");

        // One shared BaseLlm; each request object maps to its own response.
        // Cross-talk between the instances would land the wrong response on a place.
        Map<LlmRequest, LlmResponse> table = new IdentityHashMap<>();
        table.put(intentRequest, intentResponse);
        table.put(guardRequest,  guardResponse);
        var llm = new FakeBaseLlm(req -> Flowable.just(table.get(req)));

        // Two instances of the SAME stock subnet, distinct prefixes + bound places.
        var intent = LlmStepSubnet.DEF.instantiate("intentLlm")
                .bindActions(LlmStepSubnet.actionBindings(llm));
        var guard = LlmStepSubnet.DEF.instantiate("guardLlm")
                .bindActions(LlmStepSubnet.actionBindings(llm));

        var net = PetriNet.builder("two-llm-net")
                .compose(intent, b -> b.bindPort("llmRequest", intentReq)
                                       .bindPort("llmResponse", intentResp))
                .compose(guard,  b -> b.bindPort("llmRequest", guardReq)
                                       .bindPort("llmResponse", guardResp))
                .build();

        List<Token<?>> intentTokens = List.of(Token.of(intentRequest));
        List<Token<?>> guardTokens  = List.of(Token.of(guardRequest));
        Map<Place<?>, List<Token<?>>> initial = Map.of(
                intentReq, intentTokens,
                guardReq,  guardTokens);

        var finalMarking = BitmapNetExecutor.builder(net, initial).build().run();

        assertThat(values(finalMarking.peekTokens(intentResp))).containsExactly(intentResponse);
        assertThat(values(finalMarking.peekTokens(guardResp))).containsExactly(guardResponse);
    }

    @Test
    void single_instance_round_trips_via_instantiate_bindport() {
        // The original Marvin probe that used to throw — now green on MOD-031 (2.7.1).
        Place<LlmRequest>  req  = Place.of("probeReq",  LlmRequest.class);
        Place<LlmResponse> resp = Place.of("probeResp", LlmResponse.class);

        var response = textResponse("pong");
        var llm = new FakeBaseLlm(r -> Flowable.just(response));

        var net = PetriNet.builder("probe-net")
                .compose(LlmStepSubnet.DEF.instantiate("probe")
                                 .bindActions(LlmStepSubnet.actionBindings(llm)),
                         b -> b.bindPort("llmRequest", req).bindPort("llmResponse", resp))
                .build();

        List<Token<?>> tokens = List.of(Token.of(request("ping")));
        Map<Place<?>, List<Token<?>>> initial = Map.of(req, tokens);

        var finalMarking = BitmapNetExecutor.builder(net, initial).build().run();

        assertThat(values(finalMarking.peekTokens(resp))).containsExactly(response);
    }

    // ===================== helpers =====================

    private static List<LlmResponse> values(Collection<Token<LlmResponse>> tokens) {
        return tokens.stream().map(Token::value).toList();
    }

    private static LlmRequest request(String userText) {
        return LlmRequest.builder()
                .model("fake-model")
                .contents(List.of(Content.builder()
                        .role("user")
                        .parts(List.of(Part.fromText(userText)))
                        .build()))
                .build();
    }

    private static LlmResponse textResponse(String text) {
        return LlmResponse.builder()
                .content(Content.builder()
                        .role("model")
                        .parts(List.of(Part.fromText(text)))
                        .build())
                .build();
    }

    /** Minimal {@link BaseLlm} delegating {@code generateContent} to a supplier. */
    private static final class FakeBaseLlm extends BaseLlm {
        private final Function<LlmRequest, Flowable<LlmResponse>> impl;
        FakeBaseLlm(Function<LlmRequest, Flowable<LlmResponse>> impl) {
            super("fake");
            this.impl = impl;
        }
        @Override public Flowable<LlmResponse> generateContent(LlmRequest req, boolean stream) {
            return impl.apply(req);
        }
        @Override public BaseLlmConnection connect(LlmRequest req) {
            throw new UnsupportedOperationException("FakeBaseLlm.connect()");
        }
    }
}
