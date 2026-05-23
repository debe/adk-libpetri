package org.libpetri.adk.subnet;

import com.google.adk.events.Event;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.function.Supplier;
import org.libpetri.core.Arc;
import org.libpetri.core.Interface;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Transition;
import org.libpetri.core.TransitionAction;
import org.libpetri.adk.colours.AdkColours;

/**
 * Demultiplexes {@link AdkColours.TransferTarget} tokens via an
 * {@code Out.xor} over <b>compile-time-known</b> per-agent target
 * places — eliminating ADK's "hallucinated agent name → NPE" failure
 * mode by reframing it as a typed control-flow edge.
 *
 * <h2>The bug class this eliminates</h2>
 * <p>ADK's {@code AgentTransfer} injects a {@code transfer_to_agent}
 * function the LLM can call. The flow then does
 * {@code rootAgent.findAgent(name)} at runtime — if the LLM
 * hallucinates a target ("Salez" instead of "Sales"), {@code findAgent}
 * returns {@code null} and the next access raises an
 * {@link NullPointerException} deep in {@code runOneStep}. Same for
 * typos in the agent-tree wiring.
 *
 * <p>{@code TransferRouterSubnet} takes the set of <b>known agent
 * names at compile time</b> and builds an {@code Out.xor} over one
 * output place per name plus an {@code unknown} place. The demux
 * transition's action looks the name up in a hash map and produces to
 * exactly one of those places — caught structurally by libpetri's XOR
 * validator if the action ever produces to zero or more than one
 * child. The unknown branch flows into a second transition that emits
 * a structured error {@link Event} to {@link AdkColours#EVENT_OUT}
 * (the same canned-response shape used by
 * {@link LlmAgentSubnet}'s reask-exhausted-fallback).
 *
 * <p>Result: no {@link NullPointerException}, no runtime tree walk,
 * and the set of reachable transfer destinations is part of the net
 * topology and visible in the rendered DOT diagram. A typo in the
 * known-agent set is caught at net-build time; a hallucinated name at
 * runtime surfaces as a normal {@link Event} downstream consumers
 * already handle.
 *
 * <h2>Topology</h2>
 * <pre>
 *   [TRANSFER] --T_Demux--> Out.xor([target/A], [target/B], ..., [target/_unknown])
 *
 *   [target/_unknown] --T_EmitUnknownError--> [EVENT_OUT]
 * </pre>
 *
 * <h2>Interface</h2>
 * <ul>
 *   <li>{@code transfer}                 — input,  {@link AdkColours#TRANSFER}</li>
 *   <li>{@code target/<agentName>}       — output, per known agent: {@link Place}{@code <TransferTarget>}</li>
 *   <li>{@code eventOut}                 — output, {@link AdkColours#EVENT_OUT}
 *       (carries the unknown-target error event)</li>
 * </ul>
 *
 * <h2>Usage</h2>
 * <p>Built per-app via {@link #def(Set)} since the topology depends on
 * the known-agent set. {@link #targetPlace(String)} gives the {@link
 * Place} a caller composes against for a particular known name.
 */
public final class TransferRouterSubnet {

    public static final String NAME = "TransferRouter";

    public static final class Transitions {
        public static final String DEMUX               = NAME + "_Demux";
        public static final String EMIT_UNKNOWN_ERROR  = NAME + "_EmitUnknownError";
        private Transitions() {}
    }

    /** Prefix for per-target output places: {@code "TransferRouter_target/<agentName>"}. */
    public static final String TARGET_PLACE_PREFIX = NAME + "_target/";

    /** The "unknown-target" fallback place — fed when the LLM names an unrecognised agent. */
    public static final Place<AdkColours.TransferTarget> UNKNOWN_TARGET =
            Place.of(NAME + "_target/_unknown", AdkColours.TransferTarget.class);

    /**
     * Returns the {@link Place} for a particular known agent name. Use
     * this when composing the subnet into a host net to wire each
     * per-target output to a downstream agent's input.
     */
    public static Place<AdkColours.TransferTarget> targetPlace(String agentName) {
        Objects.requireNonNull(agentName, "agentName");
        return Place.of(TARGET_PLACE_PREFIX + agentName, AdkColours.TransferTarget.class);
    }

    public record Config(
            String author,
            Supplier<String> invocationIdSupplier) {

        public Config {
            Objects.requireNonNull(author, "author");
            Objects.requireNonNull(invocationIdSupplier, "invocationIdSupplier");
        }

        public static Config of(String author) {
            return new Config(author, () -> UUID.randomUUID().toString());
        }
    }

    /**
     * Build a {@link SubnetDef} whose XOR demux has one branch per
     * known agent name plus the {@link #UNKNOWN_TARGET} fallback.
     *
     * @param knownAgentNames the compile-time-known set of valid
     *                        transfer targets. Order is preserved
     *                        across iterations for deterministic
     *                        topology/DOT output. May be empty (every
     *                        runtime transfer would then route to
     *                        unknown).
     */
    public static SubnetDef<Void> def(Set<String> knownAgentNames) {
        Objects.requireNonNull(knownAgentNames, "knownAgentNames");
        var orderedNames = new LinkedHashSet<>(knownAgentNames);

        // Build per-target output places (in insertion order) + the unknown sink.
        var targets = new LinkedHashMap<String, Place<AdkColours.TransferTarget>>();
        for (var name : orderedNames) {
            targets.put(name, targetPlace(name));
        }

        // Collect all output children: per-target places + unknown.
        var allOutPlaces = new ArrayList<Place<?>>(orderedNames.size() + 1);
        allOutPlaces.addAll(targets.values());
        allOutPlaces.add(UNKNOWN_TARGET);

        // Out.xor demands >=2 children. When the known-agent set is empty,
        // there's only the unknown branch — degrade to Out.place so the
        // demux topology stays valid (every transfer routes to unknown).
        Arc.Out demuxOutput = allOutPlaces.size() == 1
                ? Arc.Out.place(allOutPlaces.get(0))
                : Arc.Out.xor(allOutPlaces.toArray(new Place<?>[0]));

        var demux = Transition.builder(Transitions.DEMUX)
                .inputs(Arc.In.one(AdkColours.TRANSFER))
                .outputs(demuxOutput)
                .build();

        var emitUnknown = Transition.builder(Transitions.EMIT_UNKNOWN_ERROR)
                .inputs(Arc.In.one(UNKNOWN_TARGET))
                .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
                .build();

        var body = PetriNet.builder(NAME)
                .transition(demux)
                .transition(emitUnknown)
                .build();

        // Interface: input transfer + per-target outputs + eventOut for unknown-error path.
        var ifaceBuilder = Interface.builder()
                .inputPort("transfer", AdkColours.TRANSFER)
                .outputPort("eventOut", AdkColours.EVENT_OUT);
        for (var entry : targets.entrySet()) {
            ifaceBuilder.outputPort("target/" + entry.getKey(), entry.getValue());
        }
        ifaceBuilder.outputPort("target/_unknown", UNKNOWN_TARGET);

        return SubnetDef.fromNet(body, ifaceBuilder.build());
    }

    /**
     * Action bindings for the subnet. Pass the same set of known names
     * used when calling {@link #def(Set)} — the action's lookup table
     * is built from it.
     */
    public static Map<String, TransitionAction> actionBindings(Set<String> knownAgentNames, Config config) {
        Objects.requireNonNull(knownAgentNames, "knownAgentNames");
        Objects.requireNonNull(config, "config");
        var orderedNames = new LinkedHashSet<>(knownAgentNames);

        var lookupByName = new LinkedHashMap<String, Place<AdkColours.TransferTarget>>();
        for (var name : orderedNames) {
            lookupByName.put(name, targetPlace(name));
        }

        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.DEMUX,              demuxAction(lookupByName));
        session.put(Transitions.EMIT_UNKNOWN_ERROR, emitUnknownErrorAction(config));
        return SubnetActions.bind(def(orderedNames), session);
    }

    private static TransitionAction demuxAction(Map<String, Place<AdkColours.TransferTarget>> lookup) {
        return ctx -> {
            AdkColours.TransferTarget target = ctx.input(AdkColours.TRANSFER);
            Place<AdkColours.TransferTarget> destination = lookup.get(target.agentName());
            if (destination != null) {
                ctx.output(destination, target);
            } else {
                ctx.output(UNKNOWN_TARGET, target);
            }
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction emitUnknownErrorAction(Config config) {
        return ctx -> {
            AdkColours.TransferTarget bad = ctx.input(UNKNOWN_TARGET);
            String message = "Cannot transfer to unknown agent: '" + bad.agentName() + "'";
            Event errorEvent = Event.builder()
                    .invocationId(config.invocationIdSupplier().get())
                    .author(config.author())
                    .content(Content.builder()
                            .role("model")
                            .parts(List.of(Part.fromText(message)))
                            .build())
                    .build();
            ctx.output(AdkColours.EVENT_OUT, errorEvent);
            return CompletableFuture.completedFuture(null);
        };
    }

    private TransferRouterSubnet() {}
}
