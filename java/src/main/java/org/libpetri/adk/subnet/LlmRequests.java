package org.libpetri.adk.subnet;

import com.google.adk.models.LlmRequest;
import com.google.adk.tools.BaseTool;
import com.google.genai.types.Content;
import com.google.genai.types.GenerateContentConfig;
import com.google.genai.types.Part;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Single sanctioned {@link LlmRequest} factory for the stock subnets.
 *
 * <p>Collapses the three repeated {@code LlmRequest.builder()...build()}
 * chains in {@link PromptBuilderSubnet}, {@link LlmAgentSubnet}'s
 * {@code BuildPrompt} action and its {@code ReAsk} continuation action
 * into one place. The four arguments are the contract — model,
 * optional system instruction, tool registry (omit if empty),
 * caller-shaped contents. Role assignment stays at the call site
 * (BuildPrompt forwards the user's content unchanged, ReAsk wraps tool
 * responses in a {@code "tool"}-role {@link Content}).
 *
 * <p>Package-private on purpose: not a public API surface. Users who
 * want to compose their own subnets build {@link LlmRequest}s directly.
 */
final class LlmRequests {

    private LlmRequests() {}

    static LlmRequest build(String model,
                            Optional<String> systemInstruction,
                            Map<String, BaseTool> tools,
                            List<Content> contents) {
        var builder = LlmRequest.builder()
                .model(model)
                .contents(contents);
        systemInstruction.ifPresent(text -> builder.config(
                GenerateContentConfig.builder()
                        .systemInstruction(Content.fromParts(Part.fromText(text)))
                        .build()));
        if (!tools.isEmpty()) {
            builder.tools(tools);
        }
        return builder.build();
    }
}
