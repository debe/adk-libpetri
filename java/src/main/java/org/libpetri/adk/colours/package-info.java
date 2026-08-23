/**
 * Colour declarations shared by the ADK Petri integration.
 *
 * <p>The turn-based path built from {@code PetriAgent.of},
 * {@code LlmAgentSubnet}, and
 * {@code SessionExecutorRegistry.strongOwned()}/
 * {@code SessionExecutorRegistry.cleanerOwned()} is the settled part of
 * the 0.x surface. It preserves the contract that each invocation emits one
 * final event; the project is 0.x, so a minor may still break API here, and
 * any such change is called out in the CHANGELOG.
 */
package org.libpetri.adk.colours;
