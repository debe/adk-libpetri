package org.libpetri.adk.subnet;

import com.google.adk.events.Event;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import java.util.ArrayList;
import java.util.Collections;
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
 * <p>Built per-app since the topology depends on the known-agent set.
 * {@link #forTargets(Set)} returns the definition and its action bindings
 * from one set, so the two cannot disagree:
 *
 * <pre>{@code
 * var router = TransferRouterSubnet.forTargets(Set.of("billing", "sales"));
 * var net = SubnetActions.bindComposed(
 *     PetriNet.builder("app").compose(LlmAgentSubnet.DEF).compose(router.def()).build(),
 *     LlmAgentSubnet.actionBindings(llm, agentConfig),
 *     router.actionBindings(TransferRouterSubnet.Config.builder("router").build()));
 * }</pre>
 *
 * <p>{@link Places#targetPlace(String)} gives the {@link Place} a caller
 * composes against for a particular known name.
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

    /** The subnet's own places: the unknown-target fallback and the per-target outputs. */
    public static final class Places {
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

        private Places() {}
    }

    /** @deprecated Use {@link Places#UNKNOWN_TARGET}, the same place. */
    @Deprecated
    public static final Place<AdkColours.TransferTarget> UNKNOWN_TARGET = Places.UNKNOWN_TARGET;

    /** @deprecated Use {@link Places#targetPlace(String)}. */
    @Deprecated
    public static Place<AdkColours.TransferTarget> targetPlace(String agentName) {
        return Places.targetPlace(agentName);
    }

    /**
     * Configuration for the unknown-target error event: its author and
     * invocation id.
     *
     * <p>Construct it with {@link #builder} (or {@link #of}): that is the
     * stable construction path. The canonical constructor, and with it record
     * patterns over this type, may change in a 0.x minor as components are
     * added.
     */
    public record Config(
            String author,
            Supplier<String> invocationIdSupplier) {

        public Config {
            Objects.requireNonNull(author, "author");
            Objects.requireNonNull(invocationIdSupplier, "invocationIdSupplier");
        }

        /** Default config: caller-supplied author, random UUID per invocation id. */
        public static Config of(String author) {
            return builder(author).build();
        }

        public static Builder builder(String author) {
            return new Builder(author);
        }

        public static final class Builder {
            private final String author;
            private Supplier<String> invocationIdSupplier = () -> UUID.randomUUID().toString();

            private Builder(String author) {
                this.author = Objects.requireNonNull(author, "author");
            }

            public Builder invocationIdSupplier(Supplier<String> s) {
                this.invocationIdSupplier = Objects.requireNonNull(s, "invocationIdSupplier");
                return this;
            }

            public Config build() {
                return new Config(author, invocationIdSupplier);
            }
        }
    }

    /**
     * A router for one known-agent set: its {@link #def()} and its
     * {@link #actionBindings(Config)}, both built from the same set, so a
     * net bound with them cannot have a demux whose lookup table disagrees
     * with its branches. Obtain one with {@link TransferRouterSubnet#forTargets(Set)}.
     */
    public static final class Targets {
        private final Set<String> names;
        private final SubnetDef<Void> def;

        private Targets(Set<String> names) {
            this.names = Collections.unmodifiableSet(new LinkedHashSet<>(names));
            this.def = TransferRouterSubnet.def(this.names);
        }

        /** The known agent names, in the order the demux branches follow. */
        public Set<String> names() {
            return names;
        }

        /** The subnet definition for {@link #names()}. */
        public SubnetDef<Void> def() {
            return def;
        }

        /** Action bindings for {@link #def()}, validated against it. */
        public Map<String, TransitionAction> actionBindings(Config config) {
            return bindings(names, def, config);
        }
    }

    /**
     * The router for {@code knownAgentNames}: one value holding the
     * definition and the action bindings for that set. Prefer this to
     * calling {@link #def(Set)} and {@link #actionBindings(Set, Config)}
     * separately, which take the set twice.
     *
     * @param knownAgentNames the compile-time-known set of valid transfer
     *                        targets; see {@link #def(Set)}
     */
    public static Targets forTargets(Set<String> knownAgentNames) {
        Objects.requireNonNull(knownAgentNames, "knownAgentNames");
        return new Targets(knownAgentNames);
    }

    /**
     * Build a {@link SubnetDef} whose XOR demux has one branch per
     * known agent name plus the {@link Places#UNKNOWN_TARGET} fallback.
     * To bind it as well, use {@link #forTargets(Set)}.
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
            targets.put(name, Places.targetPlace(name));
        }

        // Collect all output children: per-target places + unknown.
        var allOutPlaces = new ArrayList<Place<?>>(orderedNames.size() + 1);
        allOutPlaces.addAll(targets.values());
        allOutPlaces.add(Places.UNKNOWN_TARGET);

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
                .inputs(Arc.In.one(Places.UNKNOWN_TARGET))
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
        ifaceBuilder.outputPort("target/_unknown", Places.UNKNOWN_TARGET);

        return SubnetDef.fromNet(body, ifaceBuilder.build());
    }

    /**
     * Action bindings for the subnet. Pass the same set of known names
     * used when calling {@link #def(Set)} — the action's lookup table
     * is built from it.
     *
     * @deprecated Use {@link #forTargets(Set)}, whose
     *             {@link Targets#actionBindings(Config)} cannot be given a
     *             different set from its {@link Targets#def()}.
     */
    @Deprecated
    public static Map<String, TransitionAction> actionBindings(Set<String> knownAgentNames, Config config) {
        Objects.requireNonNull(knownAgentNames, "knownAgentNames");
        var orderedNames = new LinkedHashSet<>(knownAgentNames);
        return bindings(orderedNames, def(orderedNames), config);
    }

    private static Map<String, TransitionAction> bindings(
            Set<String> orderedNames, SubnetDef<Void> def, Config config) {
        Objects.requireNonNull(config, "config");
        var lookupByName = new LinkedHashMap<String, Place<AdkColours.TransferTarget>>();
        for (var name : orderedNames) {
            lookupByName.put(name, Places.targetPlace(name));
        }

        var session = new LinkedHashMap<String, TransitionAction>();
        session.put(Transitions.DEMUX,              demuxAction(lookupByName));
        session.put(Transitions.EMIT_UNKNOWN_ERROR, emitUnknownErrorAction(config));
        return SubnetActions.bind(def, session);
    }

    private static TransitionAction demuxAction(Map<String, Place<AdkColours.TransferTarget>> lookup) {
        return ctx -> {
            AdkColours.TransferTarget target = ctx.input(AdkColours.TRANSFER);
            Place<AdkColours.TransferTarget> destination = lookup.get(target.agentName());
            if (destination != null) {
                ctx.output(destination, target);
            } else {
                ctx.output(Places.UNKNOWN_TARGET, target);
            }
            return CompletableFuture.completedFuture(null);
        };
    }

    private static TransitionAction emitUnknownErrorAction(Config config) {
        return ctx -> {
            AdkColours.TransferTarget bad = ctx.input(Places.UNKNOWN_TARGET);
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
