package org.libpetri.adk.colours;

import com.google.adk.events.Event;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.FunctionResponse;
import java.util.List;
import java.util.Map;
import org.libpetri.core.Place;

/**
 * Standard typed boundary places (colours) for adk-libpetri.
 *
 * <p>Every stock subnet declares interface ports using these colours, and
 * {@link org.libpetri.core.PetriNet.Builder#compose} auto-binds them by
 * (name, tokenType) structural equality. Users compose by re-using the
 * same constants; the wiring is type-checked at net-build time.
 *
 * <p>Place identity is the (name, tokenType) record pair — two places
 * referenced from different subnets fuse iff both fields match exactly.
 *
 * <p>The wrapper records ({@link ToolCalls}, {@link ToolResults},
 * {@link LegacySessionWrite}, {@link TransferTarget}) exist because Petri
 * places are typed by a raw {@link Class}, which cannot capture
 * parameterized generics like {@code List<FunctionCall>} directly. A
 * typed wrapper gives the colour an unambiguous identity that survives
 * the {@code Class<T>} type check.
 *
 * <h2>What this catalog deliberately does NOT contain</h2>
 * <p>There is no general-purpose "state" colour. The marking IS the
 * state. In-net state-sharing patterns (per-session conversation
 * history, current product focus, feature flags) belong in
 * <i>user-declared typed Places</i> read via {@link org.libpetri.core.Arc.Read}
 * arcs — the standard in-net conversation-place pattern.
 * A single {@code Map<String, Object>}
 * "state bag" place would destroy the colour discipline: no
 * compose-time type matching, no per-domain typed access, no diagram-
 * as-domain-process. The only thing close to that shape in this
 * catalog is {@link LegacySessionWrite} — and it is named loudly
 * because it exists for exactly one purpose: write-only export to
 * ADK's {@code Session.state} legacy API.
 */
public final class AdkColours {

    /** User message inbound. Inject via {@code executor.inject(USER_IN, Token.of(content))}. */
    public static final Place<Content> USER_IN =
            Place.of("userIn", Content.class);

    /** Agent event outbound. Observe via EventStore decorator or env-place subscription. */
    public static final Place<Event> EVENT_OUT =
            Place.of("eventOut", Event.class);

    /** Prepared LLM request. Output of PromptBuilderSubnet, input of LlmStepSubnet. */
    public static final Place<LlmRequest> LLM_REQUEST =
            Place.of("llmRequest", LlmRequest.class);

    /** LLM response. Output of LlmStepSubnet, input of RouterSubnet. */
    public static final Place<LlmResponse> LLM_RESPONSE =
            Place.of("llmResponse", LlmResponse.class);

    /** Tool calls the LLM requested. Output of RouterSubnet, input of ToolDispatchSubnet. */
    public static final Place<ToolCalls> TOOL_CALLS =
            Place.of("toolCalls", ToolCalls.class);

    /** Tool results to feed back to the LLM. */
    public static final Place<ToolResults> TOOL_RESULTS =
            Place.of("toolResults", ToolResults.class);

    /**
     * Write-only envelope for ADK {@code Session.state} mutations — drained by
     * the single {@code PersistStateSubnet} transition.
     *
     * <p><b>Not the in-net state primitive.</b> See {@link LegacySessionWrite}
     * for the full rationale.
     */
    public static final Place<LegacySessionWrite> LEGACY_SESSION_WRITE =
            Place.of("legacySessionWrite", LegacySessionWrite.class);

    /** Agent-transfer target name. Routed via Out.xor over per-target places at compose time. */
    public static final Place<TransferTarget> TRANSFER =
            Place.of("transfer", TransferTarget.class);

    /**
     * Termination signal. Inhibitor source for every "advancing" transition.
     * Being a {@code Place<Void>}, it is injected with the unit-token path
     * {@link org.libpetri.adk.runner.PetriRunner#signal(Place)}, not the
     * value overload (which rejects {@code null}).
     */
    public static final Place<Void> END_INVOCATION =
            Place.of("endInvocation", Void.class);

    /**
     * <b>Escape hatch — inbound.</b> The seam where a feature the typed ADK /
     * genai surface does not model yet enters the net. Inject via
     * {@code executor.inject(RAW_PROVIDER_REQUEST, Token.of(new RawProviderRequest(...)))}.
     *
     * <p>This is the one colour in the catalog whose payload is deliberately
     * <b>opaque</b> — like {@link LegacySessionWrite}, it is named loudly because
     * it suspends the typed-per-concept discipline on purpose. A small
     * user-supplied transition consumes it, calls the raw provider API directly,
     * and emits the result onto {@link #RAW_PROVIDER_EVENT}. ADK and the stock
     * subnets remain the brain for everything they <i>do</i> model; the raw flow
     * runs concurrently as just another coloured token, fully visible in the
     * marking and the EventStore chain. This is the raw-provider passthrough
     * escape hatch: ADK stays the brain for everything it models, and a
     * not-yet-modelled provider feature can still be driven without a fork.
     */
    public static final Place<RawProviderRequest> RAW_PROVIDER_REQUEST =
            Place.of("rawProviderRequest", RawProviderRequest.class);

    /**
     * <b>Escape hatch — outbound.</b> Where the raw result of a not-yet-modelled
     * provider feature re-enters the net so downstream transitions can fold it
     * back into the typed flow. See {@link #RAW_PROVIDER_REQUEST}.
     */
    public static final Place<RawProviderEvent> RAW_PROVIDER_EVENT =
            Place.of("rawProviderEvent", RawProviderEvent.class);

    private AdkColours() {
        // colour catalog — no instances
    }

    /** Wrapper colour for {@code List<FunctionCall>} — see class-level note. */
    public record ToolCalls(List<FunctionCall> calls) {}

    /** Wrapper colour for {@code List<FunctionResponse>} — see class-level note. */
    public record ToolResults(List<FunctionResponse> results) {}

    /**
     * Envelope colour for a write-only export to ADK's legacy
     * {@code Session.state} API. The wrapped {@code Map<String, Object>}
     * matches what ADK's {@code Session.state} expects — the bag shape
     * exists at this boundary because that's the shape of the external
     * system, not because the kitchen-sink pattern is endorsed for
     * in-net use.
     *
     * <p><b>Do not use this as the general in-net state primitive.</b>
     * If a subnet needs shared in-net state (conversation history,
     * per-session context, feature flags), declare a <i>typed Place</i>
     * per domain concept and Read it from transitions that need
     * access — the standard in-net conversation-place pattern.
     * Funnelling arbitrary state through {@link LegacySessionWrite} would
     * destroy the colour discipline.
     *
     * <p>Tokens on the {@link AdkColours#LEGACY_SESSION_WRITE} place are
     * consumed exclusively by {@code PersistStateSubnet}, which calls
     * {@code BaseSessionService.appendEvent} — a one-way bridge to the
     * external legacy store for downstream consumers that haven't
     * migrated away from {@code Session.state}.
     */
    public record LegacySessionWrite(Map<String, Object> delta) {}

    /** Wrapper colour for an agent-transfer target. */
    public record TransferTarget(String agentName) {}

    /**
     * Opaque escape-hatch envelope — inbound. {@code feature} is a free-form tag
     * the user-supplied raw transition switches on (e.g. {@code "live.newModality"},
     * {@code "models.experimentalConfig"}); {@code payload} is whatever the raw
     * provider call needs and is intentionally untyped (the net does not interpret
     * it). See {@link AdkColours#RAW_PROVIDER_REQUEST}.
     */
    public record RawProviderRequest(String feature, Object payload) {}

    /**
     * Opaque escape-hatch envelope — outbound. Mirror of {@link RawProviderRequest};
     * {@code payload} carries the raw provider result for downstream transitions to
     * fold back into the typed flow. See {@link AdkColours#RAW_PROVIDER_EVENT}.
     */
    public record RawProviderEvent(String feature, Object payload) {}
}
