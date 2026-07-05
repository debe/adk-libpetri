package org.libpetri.adk.demos;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.agents.LlmAgent;
import org.junit.jupiter.api.Test;

/**
 * ADK-only foil for multi-agent transfer, the sibling of
 * {@code MultiAgentDemoTest#hallucinated_agent_name_surfaces_as_typed_error_event_not_npe}.
 *
 * <p>Stock ADK gives a hallucinated transfer target no typed-error surface.
 * {@code AgentTransfer.transferToAgent(name, ctx)} records <i>any</i> string
 * into {@code EventActions} unvalidated (verified from its bytecode: it calls
 * {@code EventActions.Builder.transferToAgent(name)} with no membership check),
 * and the tree lookup {@code BaseAgent.findAgent(name)} returns
 * {@code Optional.empty()} for an unknown name with no error routing. So an
 * unknown target is accepted, recorded, and only surfaces as a failure (or a
 * silent no-op) downstream, at whichever site forgets to defensively unwrap the
 * {@code Optional}. This is the same "remember to check everywhere" failure mode
 * the README calls out for {@code endInvocation}.
 *
 * <p>The net's {@code TransferRouterSubnet} instead demuxes an unknown target to
 * a typed error {@code Event} once, in the topology (proven in the paired
 * {@code MultiAgentDemoTest} test). This foil green-locks ADK's behavior: if a
 * future release adds structural unknown-target handling, the {@code isEmpty()}
 * assertion flips red and the catalog gets updated.
 */
class TransferUnknownTargetAdkFoilTest {

    @Test
    void adk_agent_lookup_returns_empty_for_a_hallucinated_target_with_no_typed_error() {
        var billing = LlmAgent.builder()
                .name("billing").description("billing specialist").model("gemini-2.0-flash").build();
        var techSupport = LlmAgent.builder()
                .name("tech_support").description("tech specialist").model("gemini-2.0-flash").build();
        var router = LlmAgent.builder()
                .name("router").description("routes to a specialist").model("gemini-2.0-flash")
                .subAgents(billing, techSupport)
                .build();

        // Known targets resolve.
        assertThat(router.findAgent("billing")).isPresent();
        assertThat(router.findAgent("tech_support")).isPresent();

        // A hallucinated target resolves to Optional.empty(): no typed error, no
        // unknown-target routing. Nothing at the dispatch boundary distinguishes
        // this from a valid transfer until a downstream site unwraps the empty.
        assertThat(router.findAgent("hallucinated_typo")).isEmpty();
        assertThat(router.findSubAgent("hallucinated_typo")).isEmpty();
    }
}
