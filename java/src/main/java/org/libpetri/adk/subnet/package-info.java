/**
 * Reusable ADK Petri subnet definitions.
 *
 * <p>The turn-based path built from {@code PetriAgent.builder},
 * {@code LlmAgentSubnet}, and
 * {@code SessionExecutorRegistry.strongOwned()}/
 * {@code SessionExecutorRegistry.cleanerOwned()} is the settled part of
 * the 0.x surface. It preserves the contract that each invocation emits one
 * final event; the project is 0.x, so a minor may still break API here, and
 * any such change is called out in the CHANGELOG.
 *
 * <p>Construct each subnet's {@code Config} record (and
 * {@code LlmStepSubnet.Callbacks}) with its builder or static factory: that
 * is the stable construction path. Canonical constructors, and with them
 * record patterns over these types, may change in a 0.x minor as components
 * are added.
 */
package org.libpetri.adk.subnet;
