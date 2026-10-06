package org.libpetri.adk.spec;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.fail;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.stream.Stream;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.subnet.LlmStepSubnet;
import org.libpetri.adk.subnet.LlmStreamingStepSubnet;
import org.libpetri.adk.subnet.PersistStateSubnet;
import org.libpetri.adk.subnet.PromptBuilderSubnet;
import org.libpetri.adk.subnet.RouterSubnet;
import org.libpetri.adk.subnet.StreamingLlmAgentSubnet;
import org.libpetri.adk.subnet.ToolDispatchSubnet;
import org.libpetri.adk.subnet.TransferRouterSubnet;
import org.libpetri.core.Arc;
import org.libpetri.core.Interface;
import org.libpetri.core.Place;
import org.libpetri.core.SubnetDef;
import org.libpetri.core.Timing;
import org.libpetri.core.Transition;

/**
 * Writes, and golden-checks, the canonical structure of every stock subnet to
 * {@code spec/fixtures/nets/<name>.json}. The Python port builds the same
 * fingerprint from its own nets ({@code NetSpec.fingerprint()}) and its
 * {@code tests/conformance} suite asserts it equals these files, so a
 * topology drift in either language fails that language's build.
 *
 * <p>The format is {@code json.dumps(obj, indent=2, sort_keys=True)} plus a
 * trailing newline, byte for byte, so both sides compare files exactly:
 * places and transitions sorted by name, ports by name, inputs by place,
 * output trees in declaration order.
 *
 * <p>Regenerate after changing a net:
 * <pre>
 * ./mvnw test -Dtest=SpecFixturesTest -Dspec.fixtures.write=true
 * </pre>
 */
class SpecFixturesTest {

    private static final boolean WRITE = Boolean.getBoolean("spec.fixtures.write");
    private static final Path DIR = Path.of(System.getProperty("user.dir"))
            .resolve("../spec/fixtures/nets").normalize();

    /** The known-agent set the TransferRouter fixture is built for. */
    static final Set<String> TRANSFER_TARGETS = new java.util.LinkedHashSet<>(List.of("Sales", "Support"));

    record Fixture(String file, SubnetDef<?> def) {
        @Override public String toString() { return file; }
    }

    static Stream<Fixture> fixtures() {
        return Stream.of(
                new Fixture("llm-step", LlmStepSubnet.DEF),
                new Fixture("llm-streaming-step", LlmStreamingStepSubnet.DEF),
                new Fixture("prompt-builder", PromptBuilderSubnet.DEF),
                new Fixture("router", RouterSubnet.DEF),
                new Fixture("tool-dispatch", ToolDispatchSubnet.DEF),
                new Fixture("transfer-router", TransferRouterSubnet.def(TRANSFER_TARGETS)),
                new Fixture("persist-state", PersistStateSubnet.DEF),
                new Fixture("llm-agent", LlmAgentSubnet.DEF),
                new Fixture("streaming-llm-agent", StreamingLlmAgentSubnet.DEF));
    }

    @ParameterizedTest
    @MethodSource("fixtures")
    void fixtureMatchesNet(Fixture f) throws IOException {
        String json = Json.write(fingerprint(f.def())) + "\n";
        Path golden = DIR.resolve(f.file() + ".json");
        if (WRITE) {
            Files.createDirectories(DIR);
            Files.writeString(golden, json, StandardCharsets.UTF_8);
            return;
        }
        if (!Files.exists(golden)) {
            fail(golden + " is missing; regenerate with "
                    + "./mvnw test -Dtest=SpecFixturesTest -Dspec.fixtures.write=true");
        }
        assertEquals(Files.readString(golden, StandardCharsets.UTF_8), json,
                f.file() + ".json drifted from the Java net; regenerate with "
                        + "./mvnw test -Dtest=SpecFixturesTest -Dspec.fixtures.write=true");
    }

    // ======================== Fingerprint ========================

    static Map<String, Object> fingerprint(SubnetDef<?> def) {
        var body = def.body();
        var places = new ArrayList<Map<String, Object>>();
        for (var p : sortedByName(body.places(), Place::name)) {
            places.add(obj("name", p.name(), "type", typeName(p)));
        }
        var transitions = new ArrayList<Map<String, Object>>();
        for (var t : sortedByName(body.transitions(), Transition::name)) {
            transitions.add(transition(t));
        }
        var ports = new ArrayList<Map<String, Object>>();
        for (var port : sortedByName(def.iface().ports(), Interface.Port::name)) {
            String dir = switch (port) {
                case Interface.Port.Input<?> in -> "in";
                case Interface.Port.Output<?> o -> "out";
                case Interface.Port.InOut<?> io -> "inout";
            };
            ports.add(obj("direction", dir, "name", port.name(), "place", port.place().name()));
        }
        return obj("name", body.name(), "places", places, "ports", ports, "transitions", transitions);
    }

    private static Map<String, Object> transition(Transition t) {
        var inputs = new ArrayList<Map<String, Object>>();
        var specs = new ArrayList<>(t.inputSpecs());
        specs.sort(Comparator.comparing(i -> i.place().name()));
        for (var in : specs) {
            inputs.add(switch (in) {
                case Arc.In.One o -> obj("count", 1, "kind", "one", "place", o.place().name());
                case Arc.In.Exactly e -> obj("count", e.count(), "kind", "exactly", "place", e.place().name());
                case Arc.In.All a -> obj("count", 1, "kind", "all", "place", a.place().name());
                case Arc.In.AtLeast a -> obj("count", a.minimum(), "kind", "at_least", "place", a.place().name());
            });
        }
        return obj(
                "inhibitors", sortedNames(t.inhibitors().stream().map(a -> a.place().name()).toList()),
                "inputs", inputs,
                "match", t.matchSpec() == null ? null : "present",
                "name", t.name(),
                "output", t.outputSpec() == null ? null : out(t.outputSpec()),
                "priority", t.priority(),
                "reads", sortedNames(t.reads().stream().map(a -> a.place().name()).toList()),
                "resets", sortedNames(t.resets().stream().map(a -> a.place().name()).toList()),
                "timing", timing(t.timing()));
    }

    private static Object out(Arc.Out o) {
        return switch (o) {
            case Arc.Out.Place p -> p.place().name();
            case Arc.Out.And a -> obj("and", a.children().stream().map(SpecFixturesTest::out).toList());
            case Arc.Out.Xor x -> obj("xor", x.children().stream().map(SpecFixturesTest::out).toList());
            case Arc.Out.Timeout tm -> obj("child", out(tm.child()), "timeout", tm.after().toMillis());
            case Arc.Out.ForwardInput f -> obj("forward", List.of(f.from().name(), f.to().name()));
        };
    }

    private static Object timing(Timing timing) {
        return switch (timing) {
            case Timing.Immediate i -> null;
            case Timing.Unconstrained u -> obj("earliest_ms", 0, "kind", "unconstrained", "latest_ms", null);
            case Timing.Deadline d -> obj("earliest_ms", 0, "kind", "deadline", "latest_ms", ms(d.by()));
            case Timing.Delayed d -> obj("earliest_ms", ms(d.after()), "kind", "delayed", "latest_ms", null);
            case Timing.Window w -> obj("earliest_ms", ms(w.earliest()), "kind", "window", "latest_ms", ms(w.latest()));
            case Timing.Exact e -> obj("earliest_ms", ms(e.at()), "kind", "exact", "latest_ms", ms(e.at()));
        };
    }

    private static long ms(Duration d) { return d.toMillis(); }

    private static String typeName(Place<?> p) {
        return p.tokenType().getSimpleName();
    }

    private static <T> List<T> sortedByName(java.util.Collection<T> items, java.util.function.Function<T, String> name) {
        var list = new ArrayList<>(items);
        list.sort(Comparator.comparing(name));
        return list;
    }

    private static List<String> sortedNames(List<String> names) {
        var list = new ArrayList<>(names);
        list.sort(Comparator.naturalOrder());
        return list;
    }

    private static Map<String, Object> obj(Object... kv) {
        var m = new TreeMap<String, Object>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    /** Python {@code json.dumps(indent=2, sort_keys=True)}, for ASCII names. */
    static final class Json {
        static String write(Object v) {
            var sb = new StringBuilder();
            write(sb, v, 0);
            return sb.toString();
        }

        @SuppressWarnings("unchecked")
        private static void write(StringBuilder sb, Object v, int indent) {
            if (v == null) {
                sb.append("null");
            } else if (v instanceof String s) {
                sb.append('"');
                for (char c : s.toCharArray()) {
                    switch (c) {
                        case '"' -> sb.append("\\\"");
                        case '\\' -> sb.append("\\\\");
                        case '\n' -> sb.append("\\n");
                        default -> {
                            if (c < 0x20 || c > 0x7e) sb.append(String.format("\\u%04x", (int) c));
                            else sb.append(c);
                        }
                    }
                }
                sb.append('"');
            } else if (v instanceof Number || v instanceof Boolean) {
                sb.append(v);
            } else if (v instanceof Map<?, ?> m) {
                if (m.isEmpty()) { sb.append("{}"); return; }
                var sorted = new TreeMap<String, Object>((Map<String, Object>) m);
                sb.append("{\n");
                int i = 0;
                for (var e : sorted.entrySet()) {
                    pad(sb, indent + 2);
                    write(sb, e.getKey(), 0);
                    sb.append(": ");
                    write(sb, e.getValue(), indent + 2);
                    if (++i < sorted.size()) sb.append(',');
                    sb.append('\n');
                }
                pad(sb, indent);
                sb.append('}');
            } else if (v instanceof List<?> l) {
                if (l.isEmpty()) { sb.append("[]"); return; }
                sb.append("[\n");
                for (int i = 0; i < l.size(); i++) {
                    pad(sb, indent + 2);
                    write(sb, l.get(i), indent + 2);
                    if (i + 1 < l.size()) sb.append(',');
                    sb.append('\n');
                }
                pad(sb, indent);
                sb.append(']');
            } else {
                throw new IllegalArgumentException("unsupported JSON value: " + v.getClass());
            }
        }

        private static void pad(StringBuilder sb, int n) {
            sb.append(" ".repeat(n));
        }
    }
}
