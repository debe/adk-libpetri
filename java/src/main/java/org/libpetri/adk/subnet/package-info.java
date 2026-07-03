/**
 * Reusable ADK Petri subnet definitions.
 *
 * <p>The turn-based path built from {@code PetriAgent.of},
 * {@code LlmAgentSubnet}, and
 * {@code SessionExecutorRegistry.strongOwned()}/
 * {@code SessionExecutorRegistry.cleanerOwned()} is stable for 1.x.
 * Within 1.x, this path preserves the contract that each invocation emits
 * one final event, and changes are limited to source- and binary-compatible
 * evolution.
 */
package org.libpetri.adk.subnet;
