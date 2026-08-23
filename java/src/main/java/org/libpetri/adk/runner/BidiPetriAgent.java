package org.libpetri.adk.runner;

import com.google.adk.agents.LiveRequestQueue;
import com.google.adk.events.Event;
import com.google.genai.types.LiveServerMessage;
import io.reactivex.rxjava3.core.Completable;
import io.reactivex.rxjava3.core.Flowable;
import org.libpetri.adk.Experimental;
import io.reactivex.rxjava3.disposables.Disposable;
import java.util.Objects;
import java.util.function.BiConsumer;

/**
 * The generic bidirectional pump that wires an ADK BIDI/Live channel to a
 * per-session {@link PetriRunner}. This is the reusable core that every voice
 * consumer otherwise hand-writes (Marvin's {@code LivePetriAgent} was 543 lines;
 * the library's own {@code VoiceSessionDemoTest} rewrote the same loop).
 *
 * <h2>What the library owns (generic over {@link LiveConnection} + {@link PetriRunner})</h2>
 * <ul>
 *   <li><b>Input pump:</b> each inbound {@code LiveRequest} from the ADK
 *       {@link LiveRequestQueue} is forwarded to the connection
 *       ({@code blob -> sendRealtime}, {@code content -> sendContent},
 *       {@code shouldClose -> close}).</li>
 *   <li><b>Output pump:</b> the connection's raw server stream is tapped and each
 *       frame handed to the consumer's {@code onServerMessage} callback. The
 *       outbound {@code Flowable<Event>} the bridge returns is the net's egress
 *       ({@link PetriRunner#adkEvents()}) <i>only</i>.</li>
 *   <li><b>Dispose:</b> both pumps are torn down when the egress stream
 *       terminates or is cancelled.</li>
 * </ul>
 *
 * <h2>The net authors every event (no bridge-side event mapping)</h2>
 *
 * <p>The bridge does <b>not</b> map server frames to {@link Event}s. A model turn
 * enters the running net the same way every external signal does, through the
 * consumer's {@code onServerMessage} callback: {@code runner.inject(modelChunkPlace,
 * content)} for a model {@link com.google.genai.types.Content} turn, and
 * {@code runner.signal(turnCompletePlace)} / {@code runner.signal(interruptedPlace)}
 * for the turn edges. A net transition then authors the outbound {@code Event} and
 * sets {@code partial}/{@code turnComplete} from the marking (see
 * {@code VoiceSessionDemoTest}'s emit transition).
 *
 * <p>Keeping model content in the marking is what makes the rest of the turn
 * addressable by the net: a barge-in can wipe the queued backlog with a reset arc,
 * because the chunks are still tokens. Under the old merged path they were already
 * {@code Event}s on their way out of the process and no transition could reach them
 * (see {@code VoiceSessionDemoTest.barge_in_structurally_drops_the_queued_model_chunks}).
 *
 * <p><b>Egress order is a net-structure question, not a callback-discipline one.</b>
 * Injection order is preserved: {@link PetriRunner#inject(org.libpetri.core.Place,
 * Object)} and {@link PetriRunner#signal(org.libpetri.core.Place)} enqueue FIFO from
 * a given thread, and the callback runs serially on the transport's reader thread.
 * What is <i>not</i> ordered is firing. A burst of frames is admitted to the marking
 * in one pass, and each enabled transition then fires at most once per pass, so a
 * terminal transition enabled alongside still-queued chunks emits in between them:
 * three chunks and a turn-complete arriving together come out as
 * {@code [a, b, TURN_COMPLETE, c]}.
 *
 * <p>Order it with an arc, not with a rule the callback has to remember. Inhibit the
 * terminal transition on the chunk place, so it cannot fire while content is queued:
 *
 * <pre>{@code
 * Transition.builder("T_EmitFinal")
 *         .inputs(Arc.In.one(TURN_COMPLETE))
 *         .inhibitor(MODEL_CHUNK)          // no terminal while chunks are queued
 *         .outputs(Arc.Out.place(AdkColours.EVENT_OUT))
 *         .build();
 * }</pre>
 *
 * <p>With that arc the callback stays a plain fire-and-forget decode and never blocks
 * the reader thread. The acceptance futures the inject methods return are then only
 * needed when the caller wants to observe rejection (a drained or closed runner
 * returns {@code false}). See {@code BidiPetriAgentTest}, whose burst regression fails
 * without the inhibitor.
 *
 * <p>Transition priority is <i>not</i> a substitute here. Priority sorts the
 * transitions within a firing pass, but every ready transition still fires once in
 * that pass, so a terminal enabled alongside a chunk backlog still gets its turn.
 * {@code LlmStreamingStepSubnet} orders its SSE chunks with priority <i>plus</i> an
 * await on every injection's acceptance before it appends the terminal marker; the
 * inhibitor achieves the same ordering with neither.
 *
 * <p><b>Persistence cost (ADK &ge; 1.5):</b> {@code Runner.runLive} now persists
 * every emitted event ({@code concatMapSingle(sessionService::appendEvent)}), where
 * &le; 1.4 dropped the append's result. Each event the net emits therefore costs one
 * {@code appendEvent}; suppressing intermediate chunks in-net (or coalescing them
 * before {@code EVENT_OUT}) is now a real throughput lever, not just cosmetics.
 *
 * <h2>What the consumer supplies (app/transport-specific)</h2>
 * <ul>
 *   <li>The {@link LiveConnection} (genai-SDK-specific; see {@code SyncGeminiLiveConnection}).</li>
 *   <li>{@code onServerMessage}: decode the model content and signals the net cares
 *       about and inject them via {@link PetriRunner#inject(org.libpetri.core.Place, Object)}
 *       / {@link PetriRunner#signal(org.libpetri.core.Place)}, plus route tool calls.
 *       Place names, tool names and reconnect policy are the consumer's, not the
 *       library's.</li>
 * </ul>
 *
 * <h2>Usage from a consumer's {@code BaseAgent.runLiveImpl}</h2>
 * <pre>{@code
 * protected Flowable<Event> runLiveImpl(InvocationContext ctx) {
 *     PetriRunner runner = registry.getOrCreate(SessionKey.from(ctx.session()), owner, factory);
 *     LiveConnection conn = connectionFactory.apply(ctx);   // your SyncGeminiLiveConnection
 *     return BidiPetriAgent.bridge(ctx.liveRequestQueue(), conn, runner,
 *         (msg, r) -> {
 *             msg.serverContent().flatMap(LiveServerContent::modelTurn)
 *                .ifPresent(c -> r.inject(MODEL_CHUNK, c));   // net authors the Event
 *             for (var s : SyncGeminiLiveConnection.voiceSignals(msg)) r.signal(placeFor(s));
 *             routeToolCalls(msg);
 *         });
 * }
 * }</pre>
 *
 * <p>Scope: the bridge is generic across consumers of the genai Live transport (the
 * only transport in evidence), owning the pump/dispose concurrency. A genuinely
 * different transport would supply a different bridge.
 */
@Experimental
public final class BidiPetriAgent {

    private BidiPetriAgent() {}

    /**
     * Run the bidirectional pump and return the net's outbound event stream.
     *
     * <p>The returned stream is {@link PetriRunner#adkEvents()} alone: the net authors
     * every {@link Event}. Server frames reach the net through {@code onServerMessage},
     * which injects model content and signals; the bridge itself maps nothing.
     *
     * @param inbound         the ADK live-request queue (from {@code ctx.liveRequestQueue()})
     * @param connection      the consumer's live connection
     * @param runner          the per-session Petri runner
     * @param onServerMessage consumer hook: inject model content + signals, route tool calls
     * @return the net's egress {@code Flowable<Event>}
     */
    public static Flowable<Event> bridge(
            LiveRequestQueue inbound,
            LiveConnection connection,
            PetriRunner runner,
            BiConsumer<LiveServerMessage, PetriRunner> onServerMessage) {

        Objects.requireNonNull(inbound, "inbound");
        Objects.requireNonNull(connection, "connection");
        Objects.requireNonNull(runner, "runner");
        Objects.requireNonNull(onServerMessage, "onServerMessage");

        Disposable inputPump = inbound.get().subscribe(
                req -> {
                    // sendRealtime/sendContent return cold Completables; subscribe to fire and close on send failure.
                    req.blob().ifPresent(b -> connection.sendRealtime(b).subscribe(() -> {}, connection::close));
                    req.content().ifPresent(c -> connection.sendContent(c).subscribe(() -> {}, connection::close));
                    if (req.shouldClose()) {
                        connection.close();
                    }
                },
                err -> connection.close(err));

        // Output pump: drive the raw server stream for its side effect only. The
        // consumer callback injects model content and signals into the net. The bridge
        // maps no frames to events, so the frames are dropped after the callback
        // (ignoreElements) and the net's egress is the sole source of events.
        //
        // It is merged rather than separately subscribed so the transport's terminal
        // signals still reach the caller: a rawReceive error surfaces as onError on the
        // returned stream instead of silently stranding a consumer on a dead
        // connection. adkEvents() is hot and never completes, so a completed server
        // stream cannot end the turn on its own.
        Completable serverFrames = connection.rawReceive()
                .doOnNext(msg -> onServerMessage.accept(msg, runner))
                .ignoreElements();

        // Close the transport on ANY terminal outcome, cancellation included.
        // connection.close() is otherwise only reachable from the inbound side
        // (an explicit shouldClose, a send failure, an inbound error), so a
        // consumer that simply cancels -- a user hanging up, ADK abandoning the
        // turn -- disposed the input pump and left the websocket open with no
        // remaining handle to close it. doFinally covers complete, error and
        // cancel, and close() is idempotent on every LiveConnection we ship.
        return runner.adkEvents()
                .mergeWith(serverFrames)
                .doFinally(() -> {
                    inputPump.dispose();
                    connection.close();
                });
    }
}
