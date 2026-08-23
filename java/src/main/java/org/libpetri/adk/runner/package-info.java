/**
 * Runner-facing ADK integration APIs.
 *
 * <p>The turn-based path built from {@code PetriAgent.of},
 * {@code LlmAgentSubnet}, and
 * {@code SessionExecutorRegistry.strongOwned()}/
 * {@code SessionExecutorRegistry.cleanerOwned()} is the settled part of
 * the 0.x surface. It preserves the contract that each {@code runAsync}
 * invocation emits one final event; the project is 0.x, so a minor may still
 * break API here, and any such change is called out in the CHANGELOG.
 *
 * <p>SSE streaming via {@code StreamingLlmAgentSubnet} with
 * {@code RunConfig.StreamingMode.SSE}, and BIDI/live via
 * {@code PetriAgent.ofLive}, {@code BidiPetriAgent}, and
 * {@code LiveConnection}, are beta surfaces and move faster still: they may
 * change incompatibly in any release, including a 0.x minor.
 * {@code LiveConnection} is genai-transport-specific by design.
 */
package org.libpetri.adk.runner;
