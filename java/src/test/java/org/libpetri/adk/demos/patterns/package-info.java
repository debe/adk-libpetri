/**
 * Composition pattern catalog — three idioms that ADK's stock
 * orchestration agents ({@code SequentialAgent}, {@code ParallelAgent},
 * {@code LoopAgent}, {@code AgentTransfer}) cannot express, each
 * implemented as a small Petri-net demo paired with an ADK-only foil.
 *
 * <h2>Pair-per-pattern convention</h2>
 *
 * <p>Each pattern occupies two files in this package:
 * <ul>
 *   <li>{@code PatternX_<Name>DemoTest} — the Petri-net version,
 *       composed inline via {@code PetriNet.builder()} as ~40-50 LOC
 *       of user code (no new stock {@code SubnetDef} added). Includes
 *       at least one Z3 SMT proof gated on {@code z3Available()}.</li>
 *   <li>{@code PatternX_AdkOnlyFoilTest} — the stock-ADK attempt at
 *       the same pattern. Each test passes as a <i>green-locked
 *       assertion</i> of the broken behaviour ({@code
 *       assertThat(elapsedMs).isAtLeast(slowestSleepMs)}) or
 *       documents the framework-escape (custom {@code BaseAgent} +
 *       hand-rolled Rx) needed to make it work. If a future ADK
 *       release fixes one, the assertion flips red and the catalog
 *       gets updated.</li>
 * </ul>
 *
 * <h2>The three patterns</h2>
 * <ul>
 *   <li><b>Pattern A — Speculative race</b> with structural
 *       cancellation via {@code Place<Void> RACE_WON} inhibitor.</li>
 *   <li><b>Pattern B — Late-join / K-of-N quorum</b> via
 *       {@code Arc.In.exactly(K, RESULT)} cardinality input arc.</li>
 *   <li><b>Pattern C — Optimistic commit with fallback</b> via XOR
 *       validation output and a shared {@code COMMITTED} mutex.</li>
 * </ul>
 *
 * <p>None of these patterns add a new stock {@code SubnetDef} to the
 * library — branch count, branch identity, and result shapes are
 * per-demo concerns. The catalog demonstrates that the patterns are
 * <i>idioms</i> built on the composition primitives, not new
 * framework surface. See the root {@code README.md} ("Composition
 * patterns ADK orchestration can't express") for the rationale.
 */
package org.libpetri.adk.demos.patterns;
