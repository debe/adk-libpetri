/**
 * Runner-facing ADK integration APIs.
 *
 * <p>The turn-based path built from {@code PetriAgent.of},
 * {@code LlmAgentSubnet}, and
 * {@code SessionExecutorRegistry.strongOwned()}/
 * {@code SessionExecutorRegistry.cleanerOwned()} is stable for 1.x.
 * Within 1.x, this path preserves the contract that each
 * {@code runAsync} invocation emits one final event, and changes are
 * limited to source- and binary-compatible evolution.
 *
 * <p>SSE streaming via {@code StreamingLlmAgentSubnet} with
 * {@code RunConfig.StreamingMode.SSE}, and BIDI/live via
 * {@code PetriAgent.ofLive}, {@code BidiPetriAgent}, and
 * {@code LiveConnection}, are beta surfaces and may change within 1.x.
 * {@code LiveConnection} is genai-transport-specific by design.
 */
package org.libpetri.adk.runner;
