package org.libpetri.adk.docs;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.fail;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.demos.VoiceSessionDemoTest;
import org.libpetri.adk.demos.patterns.PatternA_SpeculativeRaceDemoTest;
import org.libpetri.adk.demos.patterns.PatternB_QuorumDemoTest;
import org.libpetri.adk.demos.patterns.PatternC_OptimisticCommitDemoTest;
import org.libpetri.adk.demos.voice.BargeInSubnet;
import org.libpetri.adk.demos.voice.LiveApiRecoverySubnet;
import org.libpetri.adk.demos.voice.VadSubnet;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.adk.subnet.LlmStepSubnet;
import org.libpetri.adk.subnet.RouterSubnet;
import org.libpetri.adk.subnet.ToolDispatchSubnet;
import org.libpetri.adk.subnet.TransferRouterSubnet;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Transition;
import org.libpetri.export.DotExporter;
import org.libpetri.export.DotRenderer;
import org.libpetri.export.ExportConfig;
import org.libpetri.export.ExportConfig.ClusterSource;
import org.libpetri.export.PetriNetGraphMapper;
import org.libpetri.export.StyleConstants;
import org.libpetri.export.graph.ArcType;
import org.libpetri.export.graph.ArrowHead;
import org.libpetri.export.graph.EdgeLineStyle;
import org.libpetri.export.graph.Graph;
import org.libpetri.export.graph.GraphEdge;
import org.libpetri.export.graph.GraphNode;
import org.libpetri.export.graph.NodeShape;
import org.libpetri.export.graph.RankDir;
import org.libpetri.export.graph.Subgraph;

/**
 * Exports the README's Java-net diagrams from the nets the tests run (whole
 * nets or named-transition views), and fails the build when a committed {@code docs/diagrams/dot/*.dot}
 * file drifts from what the code now exports.
 *
 * <p>Regenerate after changing a net:
 * <pre>{@code
 * ./mvnw test -Dtest=ReadmeDiagramsTest -Dreadme.diagrams.write=true
 * cd ../docs/diagrams && npm run build   # dot -Tsvg over every dot/*.dot
 * }</pre>
 *
 * <p>The check compares DOT text only, so CI needs no graphviz.
 *
 * <p>Each diagram is the whole net or a <em>view</em>: a named subset of the
 * net's real transitions. A rename fails here, not silently in the README.
 * Five post-processing steps run on libpetri's export graph, matching
 * {@code docs/assets/diagram-legend.svg}:
 * <ul>
 *   <li><b>Names inside places.</b> A place is an ellipse that carries its
 *       name, instead of a fixed circle with the name as an {@code xlabel}:
 *       graphviz puts xlabels wherever there is room, often beside another
 *       node. An end place keeps its double outline ({@code peripheries=2}).
 *       Mid-edge labels that graphviz could park beside another arc go: the
 *       {@code read} and {@code reset} labels (the grey dashed and orange bold
 *       styles say them; {@code reset+out} stays) and an XOR branch label that
 *       repeats its target place's name.</li>
 *   <li><b>Seed suffix.</b> A seeded place's label gains {@code " ●"} or
 *       {@code " ●×K"}.</li>
 *   <li><b>Cut place.</b> In a view, a place that a transition outside the view
 *       produces or consumes is drawn dotted grey ("continues outside this
 *       view"), so it does not pass for a start or end place.</li>
 *   <li><b>Reset bundle.</b> On a named transition, reset arcs outside a kept set
 *       collapse into one orange edge to a note {@code "reset: +N places"}. N is
 *       counted from the real transition. A collapsed {@code reset+out} arc stays
 *       drawn as a plain output. In a view, the note names each bundled place
 *       the view still draws, so the drawn reset arcs do not read as the whole
 *       list, and N counts the places outside the view.</li>
 *   <li><b>Subnet prefix.</b> With {@code stripPrefix}, a place or transition
 *       inside a subnet cluster drops the {@code <Subnet>_} prefix from its
 *       label (the cluster label says it). Node ids keep the full name, and a
 *       node outside every cluster keeps its full label.</li>
 * </ul>
 * Every diagram also gets {@code bgcolor=white}, {@code pad=0.15},
 * {@code nodesep=0.3} and 12 pt edge labels, and each AND/XOR junction gets
 * {@code ordering=out} so its branches keep the net's order (triggers A, B, C;
 * 1 to 5). With intervals on, the untimed {@code [0, ∞]ms} label is dropped.
 *
 * <p>A view with clusters keeps the real net's subnet membership: the whole
 * net is exported with that cluster source and then pruned to the view's
 * transitions, their junctions and the places they touch.
 */
class ReadmeDiagramsTest {

    private static final boolean WRITE = Boolean.getBoolean("readme.diagrams.write");
    private static final Path DOT_DIR = Path.of(System.getProperty("user.dir"))
            .resolve("../docs/diagrams/dot").normalize();

    private static final String CUT_FILL = "#f6f8fa";
    private static final String CUT_STROKE = "#8c959f";
    private static final String NOTE_FILL = "#fff4e6";
    private static final String NOTE_STROKE = StyleConstants.RESET_EDGE.color(); // #fd7e14
    private static final String UNTIMED = " [0, ∞]ms";
    /** Minimum place size; a place grows to fit the name it carries. */
    private static final Double PLACE_SIZE = 0.3;
    /** Edge-label size: at least about 9 px at the README's display widths. */
    private static final String EDGE_FONT_SIZE = "12";
    /** Stands in for a DOT line break ({@code \\n}); the renderer would escape a backslash. */
    private static final String LINE_BREAK = "\u0001";

    /**
     * One README diagram.
     *
     * @param name           file stem under {@code docs/diagrams/dot/}
     * @param source         where the net comes from, written into the DOT header
     * @param net            the real net (unbound is fine; only the structure is read)
     * @param view           transition names to keep, or {@code null} for the whole net
     * @param env            environment place names
     * @param seeds          place name to seed suffix ({@code "●"}, {@code "●×K"})
     * @param bundleResetsOn transition name to the reset places it keeps drawn
     * @param direction      layout direction
     * @param showIntervals  show firing intervals (only for timed nets)
     * @param clusters       cluster source
     * @param stripPrefix    drop the {@code <Subnet>_} label prefix inside a cluster
     * @param graphAttrs     graph attributes for this diagram only (a tighter
     *                       {@code nodesep}, {@code newrank} so clusters rank with the
     *                       rest of the net), over the common ones
     * @param stackBelow     place name to a place name drawn below it, through an
     *                       invisible layout edge (stacks one cluster under another)
     */
    record DiagramSpec(
            String name,
            String source,
            PetriNet net,
            List<String> view,
            Set<String> env,
            Map<String, String> seeds,
            Map<String, Set<String>> bundleResetsOn,
            RankDir direction,
            boolean showIntervals,
            ClusterSource clusters,
            boolean stripPrefix,
            Map<String, String> graphAttrs,
            Map<String, String> stackBelow) {}

    // ======================== Diagram catalog ========================

    static List<DiagramSpec> specs() {
        var llmAgent = LlmAgentSubnet.DEF.body();
        var specs = new ArrayList<DiagramSpec>();

        // D4a, G1: the turn shell around LlmAgent's inner loop.
        specs.add(new DiagramSpec(
                "llm-agent-turn-shell",
                "LlmAgentSubnet.DEF.body(), view of the turn shell",
                llmAgent,
                List.of(LlmAgentSubnet.Transitions.START_TURN,
                        LlmAgentSubnet.Transitions.BUILD_PROMPT,
                        LlmAgentSubnet.Transitions.EMIT_ANSWER,
                        LlmAgentSubnet.Transitions.EMIT_TRANSFER,
                        LlmAgentSubnet.Transitions.ABORT_TURN,
                        LlmAgentSubnet.Transitions.DROP_ABORT),
                names(AdkColours.USER_IN, AdkColours.TURN_ABORT),
                Map.of(AdkColours.TURN_PERMIT.name(), "●"),
                Map.of(LlmAgentSubnet.Transitions.ABORT_TURN,
                        names(LlmAgentSubnet.TURN_INPUT, LlmAgentSubnet.CONVERSATION)),
                RankDir.TB, false, ClusterSource.NONE, false,
                Map.of("nodesep", "0.25"), Map.of()));

        // D4b, canonical composition: the inner LLM-and-tool loop.
        specs.add(new DiagramSpec(
                "llm-agent-inner-loop",
                "LlmAgentSubnet.DEF.body(), view of the inner loop",
                llmAgent,
                List.of(LlmAgentSubnet.Transitions.BUILD_PROMPT,
                        LlmStepSubnet.Transitions.BEFORE_MODEL,
                        LlmStepSubnet.Transitions.LLM_CALL,
                        LlmStepSubnet.Transitions.AFTER_MODEL,
                        LlmStepSubnet.Transitions.ON_MODEL_ERROR,
                        RouterSubnet.Transitions.ROUTE,
                        ToolDispatchSubnet.Transitions.DISPATCH,
                        LlmAgentSubnet.Transitions.RE_ASK,
                        LlmAgentSubnet.Transitions.RE_ASK_EXHAUSTED_FALLBACK),
                Set.of(),
                Map.of(LlmAgentSubnet.REASK_BUDGET.name(), "●×K"),
                Map.of(),
                RankDir.TB, false, ClusterSource.AUTO, true,
                Map.of("nodesep", "0.25", "newrank", "true"), Map.of()));

        // D4c, G2: the reask budget.
        specs.add(new DiagramSpec(
                "reask-budget",
                "LlmAgentSubnet.DEF.body(), view of the reask budget",
                llmAgent,
                List.of(LlmAgentSubnet.Transitions.BUILD_PROMPT,
                        LlmAgentSubnet.Transitions.RE_ASK,
                        LlmAgentSubnet.Transitions.RE_ASK_EXHAUSTED_FALLBACK,
                        LlmAgentSubnet.Transitions.EMIT_ANSWER),
                Set.of(),
                Map.of(LlmAgentSubnet.REASK_BUDGET.name(), "●×K"),
                Map.of(),
                RankDir.TB, false, ClusterSource.NONE, false,
                Map.of("nodesep", "0.2"), Map.of()));

        // D5, G4: first-wins race.
        specs.add(new DiagramSpec(
                "speculative-race",
                "PatternA_SpeculativeRaceDemoTest.buildNet()",
                PatternA_SpeculativeRaceDemoTest.buildNet(),
                null,
                names(AdkColours.USER_IN),
                Map.of(),
                Map.of("Race_Start", Set.of()),
                RankDir.TB, false, ClusterSource.AUTO, false,
                Map.of(), Map.of()));

        // D6, G4: optimistic commit.
        specs.add(new DiagramSpec(
                "optimistic-commit",
                "PatternC_OptimisticCommitDemoTest.buildNet()",
                PatternC_OptimisticCommitDemoTest.buildNet(),
                null,
                names(AdkColours.USER_IN),
                Map.of(),
                Map.of("Opt_StartBoth", Set.of()),
                RankDir.TB, false, ClusterSource.AUTO, false,
                Map.of(), Map.of()));

        // D7, G4: K-of-N quorum. Quorum_Start's three resets are bundled, as
        // the race and optimistic commit bundle their start transitions' resets.
        specs.add(new DiagramSpec(
                "quorum",
                "PatternB_QuorumDemoTest.buildNet()",
                PatternB_QuorumDemoTest.buildNet(),
                null,
                names(AdkColours.USER_IN),
                Map.of(),
                Map.of("Quorum_Start", Set.of()),
                RankDir.TB, false, ClusterSource.AUTO, false,
                Map.of(), Map.of()));

        // D10, G5: the fixed escalation ladder (cancel on activity). Composed
        // into a one-subnet net so it gets a LiveApiRecovery cluster and the
        // prefix can go from its labels.
        specs.add(new DiagramSpec(
                "escalation-ladder",
                "PetriNet.builder(\"escalation-ladder\").compose(LiveApiRecoverySubnet.def(Config.defaults()))",
                PetriNet.builder("escalation-ladder")
                        .compose(LiveApiRecoverySubnet.def(LiveApiRecoverySubnet.Config.defaults()))
                        .build(),
                null,
                names(LiveApiRecoverySubnet.Places.RESPONSE_AWAITED,
                        LiveApiRecoverySubnet.Places.MODEL_ACTIVE,
                        LiveApiRecoverySubnet.Places.MODEL_QUIET),
                Map.of(),
                Map.of(),
                RankDir.TB, true, ClusterSource.AUTO, true,
                Map.of(), Map.of()));

        // D12, G6: VAD window feeding barge-in, composed as
        // SyncGeminiLiveConnectionTest composes them.
        specs.add(new DiagramSpec(
                "vad-bargein",
                "PetriNet.builder(\"vad+bargein\").compose(VadSubnet.DEF).compose(BargeInSubnet.DEF)",
                PetriNet.builder("vad+bargein")
                        .compose(VadSubnet.DEF)
                        .compose(BargeInSubnet.DEF)
                        .build(),
                null,
                names(VadSubnet.Places.SPEECH_STARTED, VadSubnet.Places.SPEECH_STOPPED,
                        BargeInSubnet.Places.INTERRUPTED),
                Map.of(),
                Map.of(),
                RankDir.TB, false, ClusterSource.AUTO, true,
                Map.of("newrank", "true"),
                // Vad above the shared window place, BargeIn below it.
                Map.of(VadSubnet.Places.UTTERANCE_ENDED.name(),
                        BargeInSubnet.Places.INTERRUPTED.name())));

        // D13, G6: barge-in drops the queued model chunks.
        specs.add(new DiagramSpec(
                "barge-in-chunk-drop",
                "VoiceSessionDemoTest.bargeInDropNet()",
                VoiceSessionDemoTest.bargeInDropNet(),
                null,
                names(BargeInSubnet.Places.INTERRUPTED, BargeInSubnet.Places.VOICE_ACTIVITY_OPEN,
                        AdkColours.LLM_RESPONSE),
                Map.of(),
                Map.of(),
                RankDir.TB, false, ClusterSource.AUTO, true,
                Map.of(), Map.of()));

        // D14, G3: typed fallback for an unknown transfer target. The names are
        // MultiAgentDemoTest's, in a fixed order so the export is stable.
        specs.add(new DiagramSpec(
                "transfer-router",
                "TransferRouterSubnet.def([billing, tech_support]).body()",
                TransferRouterSubnet.def(new LinkedHashSet<>(List.of("billing", "tech_support")))
                        .body(),
                null,
                names(AdkColours.TRANSFER),
                Map.of(),
                Map.of(),
                RankDir.TB, false, ClusterSource.NONE, false,
                Map.of(), Map.of()));

        return specs;
    }

    // ======================== Tests ========================

    @TestFactory
    Stream<DynamicTest> readme_diagrams_match_the_java_nets() {
        return specs().stream().map(spec -> DynamicTest.dynamicTest(spec.name(), () -> check(spec)));
    }

    /** Spot checks that the D10 export carries the ladder fix, not the old two-rung shape. */
    @Test
    void escalation_ladder_export_carries_the_cancel_on_activity_transitions() {
        var spec = specs().stream().filter(s -> s.name().equals("escalation-ladder")).findFirst()
                .orElseThrow();
        String dot = render(spec);
        for (var t : List.of(
                LiveApiRecoverySubnet.Transitions.NUDGE,
                LiveApiRecoverySubnet.Transitions.RECOVER,
                LiveApiRecoverySubnet.Transitions.ANSWERED,
                LiveApiRecoverySubnet.Transitions.ANSWERED_LATE,
                LiveApiRecoverySubnet.Transitions.MODEL_QUIET,
                LiveApiRecoverySubnet.Transitions.IGNORE_QUIET)) {
            // Inside the LiveApiRecovery cluster the label drops the prefix.
            String label = t.substring(t.indexOf('_') + 1);
            assertTrue(dot.contains("t_" + DotExporter.sanitize(t) + " [label=\"" + label),
                    "D10 export lacks " + t);
        }
        assertTrue(dot.contains("[3000, ∞]ms"), "D10 lost its 3 s delays");
        assertTrue(!dot.contains(UNTIMED), "D10 still labels untimed transitions");
    }

    private static void check(DiagramSpec spec) throws IOException {
        String dot = render(spec);
        Path golden = DOT_DIR.resolve(spec.name() + ".dot");
        if (WRITE) {
            Files.createDirectories(DOT_DIR);
            Files.writeString(golden, dot, StandardCharsets.UTF_8);
            return;
        }
        if (!Files.exists(golden)) {
            fail(golden + " is missing; regenerate with "
                    + "./mvnw test -Dtest=ReadmeDiagramsTest -Dreadme.diagrams.write=true");
        }
        assertEquals(Files.readString(golden, StandardCharsets.UTF_8), dot,
                spec.name() + ".dot drifted from " + spec.source() + "; regenerate with "
                        + "./mvnw test -Dtest=ReadmeDiagramsTest -Dreadme.diagrams.write=true");
    }

    // ======================== Export and post-processing ========================

    static String render(DiagramSpec spec) {
        PetriNet full = spec.net();
        Map<String, Transition> byName = new LinkedHashMap<>();
        for (var t : full.transitions()) byName.put(t.name(), t);

        // Views: every listed transition must exist in the real net.
        PetriNet net;
        Set<String> viewNames;
        if (spec.view() == null) {
            net = full;
            viewNames = byName.keySet();
        } else {
            var missing = spec.view().stream().filter(n -> !byName.containsKey(n)).toList();
            assertTrue(missing.isEmpty(), spec.name() + ": view names not in "
                    + full.name() + ": " + missing + "; it has " + byName.keySet());
            var builder = PetriNet.builder(spec.name());
            for (var n : spec.view()) builder.transition(byName.get(n));
            net = builder.build();
            viewNames = new LinkedHashSet<>(spec.view());
        }
        for (var t : spec.bundleResetsOn().keySet()) {
            assertTrue(viewNames.contains(t), spec.name() + ": bundle target " + t + " not drawn");
        }

        // Every named place must be drawn; a renamed place fails here.
        Set<String> drawnPlaces = placeNames(net);
        var named = new LinkedHashSet<String>();
        named.addAll(spec.env());
        named.addAll(spec.seeds().keySet());
        spec.bundleResetsOn().values().forEach(named::addAll);
        spec.stackBelow().forEach((above, below) -> {
            named.add(above);
            named.add(below);
        });
        for (var p : named) {
            assertTrue(drawnPlaces.contains(p), spec.name() + ": place " + p + " is not drawn; "
                    + "drawn: " + drawnPlaces);
        }

        var config = new ExportConfig(
                spec.direction(), true, spec.showIntervals(), true, spec.env(), spec.clusters());
        Graph g;
        if (spec.view() != null && spec.clusters() != ClusterSource.NONE) {
            // A view net built from bare transitions has no subnet membership,
            // so export the whole net with its clusters and prune to the view.
            var keep = new HashSet<String>();
            drawnPlaces.forEach(p -> keep.add("p_" + DotExporter.sanitize(p)));
            var junctionPrefixes = new ArrayList<String>();
            for (var n : viewNames) {
                keep.add("t_" + DotExporter.sanitize(n));
                junctionPrefixes.add("j_" + DotExporter.sanitize(n) + "__");
            }
            Graph whole = PetriNetGraphMapper.map(full, config);
            var prune = new Prune(keep, junctionPrefixes);
            g = new Graph(DotExporter.sanitize(net.name()), whole.rankdir(),
                    prune.nodes(whole.nodes()), prune.edges(whole.edges()),
                    prune.subgraphs(whole.subgraphs()), whole.graphAttrs(), whole.nodeDefaults(),
                    whole.edgeDefaults());
        } else {
            g = PetriNetGraphMapper.map(net, config);
        }

        // Label prefix to strip per node id: the label of the cluster it sits in.
        var clusterOf = new HashMap<String, String>();
        if (spec.stripPrefix()) clusterLabels(g.subgraphs(), clusterOf);

        // --- Reset bundles: decide which edges collapse.
        Set<GraphEdge> removeEdges = Collections.newSetFromMap(new IdentityHashMap<>());
        Set<GraphEdge> toPlainOutput = Collections.newSetFromMap(new IdentityHashMap<>());
        var notes = new ArrayList<GraphNode>();
        var noteEdges = new ArrayList<GraphEdge>();
        var allEdges = allEdges(g);
        for (var entry : spec.bundleResetsOn().entrySet()) {
            Transition t = byName.get(entry.getKey());
            Set<String> keep = entry.getValue();
            String san = DotExporter.sanitize(t.name());
            String tid = "t_" + san;
            var bundled = t.resets().stream().map(r -> r.place().name())
                    .filter(n -> !keep.contains(n)).collect(Collectors.toCollection(LinkedHashSet::new));
            assertTrue(bundled.size() >= 2, spec.name() + ": bundle on " + t.name()
                    + " collapses " + bundled.size() + " resets; draw them instead");
            var bundledIds = bundled.stream().map(n -> "p_" + DotExporter.sanitize(n))
                    .collect(Collectors.toSet());
            int matched = 0;
            for (var e : allEdges) {
                boolean fromT = e.from().equals(tid) || e.from().startsWith("j_" + san + "__");
                if (!fromT || !bundledIds.contains(e.to())) continue;
                if (e.arcType() == ArcType.RESET) {
                    removeEdges.add(e);
                    matched++;
                } else if (e.arcType() == ArcType.RESET_OUTPUT) {
                    toPlainOutput.add(e);
                    matched++;
                }
            }
            assertEquals(bundled.size(), matched, spec.name() + ": reset edges of " + t.name());
            // In a view, a bundled place that is still drawn (it has another
            // arc) is named in the note; the rest lie outside the view.
            var namedInNote = new ArrayList<String>();
            if (spec.view() != null) {
                for (var name : bundled) {
                    String pid = "p_" + DotExporter.sanitize(name);
                    boolean drawn = allEdges.stream().anyMatch(e -> !removeEdges.contains(e)
                            && (e.from().equals(pid) || e.to().equals(pid)));
                    if (drawn) namedInNote.add(displayName(pid, name, clusterOf));
                }
            }
            String noteId = "n_" + san + "__resets";
            notes.add(new GraphNode(noteId, noteLabel(namedInNote, bundled.size()),
                    NodeShape.BOX, NOTE_FILL, NOTE_STROKE, 1.5, noteId, null, null, null,
                    Map.of()));
            var reset = StyleConstants.RESET_EDGE;
            noteEdges.add(new GraphEdge(tid, noteId, null, reset.color(), reset.style(),
                    reset.arrowhead(), reset.penwidth(), ArcType.RESET, Map.of()));
        }

        // Places left with no edge once their reset arcs are bundled are dropped.
        var remainingEdges = allEdges.stream().filter(e -> !removeEdges.contains(e)).toList();
        var connected = new HashSet<String>();
        for (var e : remainingEdges) {
            connected.add(e.from());
            connected.add(e.to());
        }

        // --- Cut places: produced or consumed by a transition outside the view.
        var cut = new HashSet<String>();
        if (spec.view() != null) {
            for (var t : full.transitions()) {
                if (viewNames.contains(t.name())) continue;
                var touched = new LinkedHashSet<String>();
                t.inputSpecs().forEach(in -> touched.add(in.place().name()));
                t.reads().forEach(r -> touched.add(r.place().name()));
                if (t.outputSpec() != null) t.outputSpec().allPlaces().forEach(p -> touched.add(p.name()));
                for (var p : touched) {
                    if (drawnPlaces.contains(p) && !spec.env().contains(p)
                            && !spec.seeds().containsKey(p)) {
                        cut.add("p_" + DotExporter.sanitize(p));
                    }
                }
            }
        }

        var seedIds = new LinkedHashMap<String, String>();
        spec.seeds().forEach((p, s) -> seedIds.put("p_" + DotExporter.sanitize(p), s));

        var edgeOrder = new LinkedHashMap<String, Integer>();
        for (var e : remainingEdges) {
            edgeOrder.putIfAbsent(e.from(), edgeOrder.size());
            edgeOrder.putIfAbsent(e.to(), edgeOrder.size());
        }
        var post = new PostProcess(removeEdges, toPlainOutput, connected, cut, seedIds,
                spec.showIntervals(), edgeOrder, clusterOf);
        var graphAttrs = new LinkedHashMap<>(g.graphAttrs());
        graphAttrs.put("bgcolor", "white");
        // A margin around the white card, so content does not touch its edge
        // on a dark page.
        graphAttrs.put("pad", "0.15");
        // Places carry their names, so they are wider; tighter rows keep each
        // diagram narrow enough for about 9 px text at its README width.
        graphAttrs.put("nodesep", "0.3");
        // Per-diagram overrides; a key already set keeps its position, and new
        // keys go in sorted order (Map.of iteration order varies between runs).
        new java.util.TreeMap<>(spec.graphAttrs()).forEach(graphAttrs::put);
        var nodes = new ArrayList<>(post.nodes(g.nodes()));
        nodes.addAll(notes);
        var edges = new ArrayList<>(post.edges(g.edges()));
        edges.addAll(noteEdges);
        new java.util.TreeMap<>(spec.stackBelow()).forEach((above, below) -> edges.add(new GraphEdge(
                "p_" + DotExporter.sanitize(above), "p_" + DotExporter.sanitize(below), null,
                "#000000", EdgeLineStyle.INVIS, ArrowHead.NONE, null, ArcType.OUTPUT, Map.of())));
        var edgeDefaults = new LinkedHashMap<>(g.edgeDefaults());
        edgeDefaults.put("fontsize", EDGE_FONT_SIZE);
        var out = new Graph(g.id(), g.rankdir(), nodes, edges, post.subgraphs(g.subgraphs()),
                graphAttrs, g.nodeDefaults(), edgeDefaults);

        String dot = DotRenderer.render(out);
        // libpetri's NodeShape has no note; patch the bundle notes' shape.
        for (var n : notes) {
            String before = "    " + n.id() + " [label=";
            var lines = dot.lines().map(l -> l.startsWith(before)
                    ? l.replace("shape=\"box\"", "shape=\"note\"") : l).toList();
            String patched = String.join("\n", lines);
            assertTrue(!patched.equals(dot), spec.name() + ": note " + n.id() + " not found");
            dot = patched;
        }
        for (var id : seedIds.keySet()) {
            assertTrue(dot.contains(" " + seedIds.get(id) + "\""),
                    spec.name() + ": seed suffix missing on " + id);
        }
        dot = dot.replace(LINE_BREAK, "\\n");

        return "// GENERATED by java/src/test/java/org/libpetri/adk/docs/ReadmeDiagramsTest.java\n"
                + "// from " + spec.source() + ". Do not edit: rerun the test with\n"
                + "// -Dreadme.diagrams.write=true, then `npm run build` in docs/diagrams.\n"
                + dot + "\n";
    }

    /** Node and edge rewrites, applied at every cluster depth. */
    private record PostProcess(
            Set<GraphEdge> removeEdges,
            Set<GraphEdge> toPlainOutput,
            Set<String> connected,
            Set<String> cut,
            Map<String, String> seeds,
            boolean stripUntimed,
            Map<String, Integer> edgeOrder,
            Map<String, String> clusterOf) {

        List<GraphNode> nodes(List<GraphNode> in) {
            var places = new ArrayList<GraphNode>();
            var others = new ArrayList<GraphNode>();
            for (var n : in) {
                if (!n.id().startsWith("p_")) {
                    others.add(node(n));
                } else if (connected.contains(n.id())) {
                    places.add(node(n));
                }
            }
            // libpetri's PlaceAnalysis walks Out.allPlaces(), a hash set, so its
            // place order varies between runs. Order places by first use in the
            // (deterministic) edge list instead, so the golden file is stable.
            places.sort(java.util.Comparator
                    .comparingInt((GraphNode n) -> edgeOrder.getOrDefault(n.id(), Integer.MAX_VALUE))
                    .thenComparing(GraphNode::id));
            var out = new ArrayList<GraphNode>(places);
            out.addAll(others);
            return out;
        }

        GraphNode node(GraphNode n) {
            if (n.id().startsWith("t_")) {
                String label = displayName(n.id(), n.label(), clusterOf);
                if (stripUntimed) label = label.replace(UNTIMED, "");
                return new GraphNode(n.id(), label, n.shape(), n.fill(),
                        n.stroke(), n.penwidth(), n.semanticId(), n.style(), n.height(), n.width(),
                        n.attrs());
            }
            if (n.id().startsWith("j_")) {
                // Keep the branches in the net's order (A, B, C; 1 to 5):
                // otherwise graphviz may reorder a junction's children.
                var attrs = new LinkedHashMap<>(n.attrs());
                attrs.put("ordering", "out");
                return new GraphNode(n.id(), n.label(), n.shape(), n.fill(), n.stroke(),
                        n.penwidth(), n.semanticId(), n.style(), n.height(), n.width(), attrs);
            }
            if (!n.id().startsWith("p_")) return n;
            // The name goes inside the place, not beside it: graphviz places
            // xlabels wherever there is room, often next to another node.
            var attrs = new LinkedHashMap<>(n.attrs());
            String name = attrs.remove("xlabel");
            assertTrue(name != null && !name.isEmpty(), "place " + n.id() + " has no xlabel");
            name = displayName(n.id(), name, clusterOf);
            if (seeds.containsKey(n.id())) name = name + " " + seeds.get(n.id());
            attrs.put("fixedsize", "false");
            attrs.put("margin", "0.08,0.03");
            if (cut.contains(n.id())) {
                return new GraphNode(n.id(), name, NodeShape.ELLIPSE, CUT_FILL, CUT_STROKE, 2.0,
                        n.semanticId(), "dotted", PLACE_SIZE, PLACE_SIZE, attrs);
            }
            if (n.shape() == NodeShape.DOUBLECIRCLE) attrs.put("peripheries", "2");
            return new GraphNode(n.id(), name, NodeShape.ELLIPSE, n.fill(), n.stroke(), n.penwidth(),
                    n.semanticId(), n.style(), PLACE_SIZE, PLACE_SIZE, attrs);
        }

        List<GraphEdge> edges(List<GraphEdge> in) {
            var out = new ArrayList<GraphEdge>();
            for (var e : in) {
                if (removeEdges.contains(e)) continue;
                if (toPlainOutput.contains(e)) {
                    var o = StyleConstants.OUTPUT_EDGE;
                    out.add(new GraphEdge(e.from(), e.to(), null, o.color(), o.style(),
                            o.arrowhead(), o.penwidth(), ArcType.OUTPUT, e.attrs()));
                } else if (redundantLabel(e)) {
                    out.add(new GraphEdge(e.from(), e.to(), null, e.color(), e.style(),
                            e.arrowhead(), e.penwidth(), e.arcType(), e.attrs()));
                } else {
                    out.add(e);
                }
            }
            return out;
        }

        /**
         * A mid-edge label graphviz may park beside another arc: "read" and
         * "reset" (the grey dashed and orange bold styles already say them;
         * "reset+out" stays) and an XOR branch label that repeats the name of
         * the place it points at (now inside that place).
         */
        private static boolean redundantLabel(GraphEdge e) {
            if (e.label() == null) return false;
            if (e.arcType() == ArcType.READ || e.arcType() == ArcType.RESET) return true;
            return e.from().startsWith("j_") && e.to().equals("p_" + DotExporter.sanitize(e.label()));
        }

        List<Subgraph> subgraphs(List<Subgraph> in) {
            // Cluster order follows node order, which is unstable upstream; sort by id.
            return in.stream().sorted(java.util.Comparator.comparing(Subgraph::id))
                    .map(s -> new Subgraph(s.id(), s.label(), nodes(s.nodes()),
                            edges(s.edges()), subgraphs(s.subgraphs()), s.attrs())).toList();
        }
    }

    /** Cuts a whole-net export down to a view: kept node ids, their edges, non-empty clusters. */
    private record Prune(Set<String> keep, List<String> junctionPrefixes) {

        boolean kept(String id) {
            return keep.contains(id) || junctionPrefixes.stream().anyMatch(id::startsWith);
        }

        List<GraphNode> nodes(List<GraphNode> in) {
            return in.stream().filter(n -> kept(n.id())).toList();
        }

        List<GraphEdge> edges(List<GraphEdge> in) {
            return in.stream().filter(e -> kept(e.from()) && kept(e.to())).toList();
        }

        List<Subgraph> subgraphs(List<Subgraph> in) {
            var out = new ArrayList<Subgraph>();
            for (var s : in) {
                var sub = new Subgraph(s.id(), s.label(), nodes(s.nodes()), edges(s.edges()),
                        subgraphs(s.subgraphs()), s.attrs());
                if (!sub.nodes().isEmpty() || !sub.subgraphs().isEmpty()) out.add(sub);
            }
            return out;
        }
    }

    // ======================== Helpers ========================

    /** Records, per node id, the label of the innermost cluster that holds it. */
    private static void clusterLabels(List<Subgraph> sgs, Map<String, String> out) {
        for (var s : sgs) {
            if (s.label() != null) s.nodes().forEach(n -> out.put(n.id(), s.label()));
            clusterLabels(s.subgraphs(), out);
        }
    }

    /** A node's label without its cluster's {@code <Subnet>_} prefix, when it has one. */
    private static String displayName(String id, String label, Map<String, String> clusterOf) {
        String cluster = clusterOf.get(id);
        if (cluster == null) return label;
        String prefix = cluster + "_";
        return label.startsWith(prefix) && label.length() > prefix.length()
                ? label.substring(prefix.length()) : label;
    }

    /**
     * {@code "reset: +N places"}, or, when the view draws some bundled places,
     * those names (one per line, to keep the note narrow) and the count of the
     * places outside the view.
     */
    private static String noteLabel(List<String> drawn, int bundled) {
        if (drawn.isEmpty()) return "reset: +" + bundled + " places";
        var lines = new ArrayList<String>(drawn);
        lines.set(0, "reset: " + lines.get(0));
        int rest = bundled - drawn.size();
        if (rest > 0) lines.add("+" + rest + (rest == 1 ? " place" : " places") + " outside this view");
        return String.join(LINE_BREAK, lines);
    }

    private static Set<String> names(Place<?>... places) {
        var s = new LinkedHashSet<String>();
        for (var p : places) s.add(p.name());
        return s;
    }

    private static Set<String> placeNames(PetriNet net) {
        var s = new LinkedHashSet<String>();
        for (var t : net.transitions()) {
            t.inputSpecs().forEach(in -> s.add(in.place().name()));
            t.reads().forEach(r -> s.add(r.place().name()));
            t.inhibitors().forEach(i -> s.add(i.place().name()));
            t.resets().forEach(r -> s.add(r.place().name()));
            if (t.outputSpec() != null) t.outputSpec().allPlaces().forEach(p -> s.add(p.name()));
        }
        return s;
    }

    private static List<GraphEdge> allEdges(Graph g) {
        var out = new ArrayList<GraphEdge>(g.edges());
        collect(g.subgraphs(), out);
        return out;
    }

    private static void collect(List<Subgraph> sgs, List<GraphEdge> out) {
        for (var s : sgs) {
            out.addAll(s.edges());
            collect(s.subgraphs(), out);
        }
    }
}
