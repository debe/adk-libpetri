package org.libpetri.adk.runner;

import com.google.adk.agents.LiveRequestQueue;
import com.google.adk.events.Event;
import com.google.genai.types.Content;
import com.google.genai.types.LiveServerContent;
import com.google.genai.types.LiveServerMessage;
import io.reactivex.rxjava3.core.Flowable;
import org.libpetri.adk.Experimental;
import io.reactivex.rxjava3.disposables.Disposable;
import java.util.Objects;
import java.util.Optional;
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
 *   <li><b>Output pump + merge:</b> the connection's raw server stream is tapped,
 *       handed to the consumer's {@code onServerMessage} callback, mapped to ADK
 *       {@link Event}s (model content), and merged with the net's egress
 *       {@link PetriRunner#adkEvents()} into one outbound {@code Flowable<Event>}.</li>
 *   <li><b>Dispose:</b> the input pump is torn down when the merged stream
 *       terminates or is cancelled.</li>
 * </ul>
 *
 * <h2>What the consumer supplies (app/transport-specific)</h2>
 * <ul>
 *   <li>The {@link LiveConnection} (genai-SDK-specific; see {@code SyncGeminiLiveConnection}).</li>
 *   <li>{@code onServerMessage}: decode the signals the net cares about and inject
 *       them via {@link PetriRunner#signal(org.libpetri.core.Place)}, plus route tool
 *       calls. Place names, tool names and reconnect policy are the consumer's, not
 *       the library's.</li>
 * </ul>
 *
 * <h2>Usage from a consumer's {@code BaseAgent.runLiveImpl}</h2>
 * <pre>{@code
 * protected Flowable<Event> runLiveImpl(InvocationContext ctx) {
 *     PetriRunner runner = registry.getOrCreate(SessionKey.from(ctx.session()), owner, factory);
 *     LiveConnection conn = connectionFactory.apply(ctx);   // your SyncGeminiLiveConnection
 *     return BidiPetriAgent.bridge(ctx.liveRequestQueue(), conn, runner, name(),
 *         (msg, r) -> {
 *             for (var s : SyncGeminiLiveConnection.voiceSignals(msg)) r.signal(placeFor(s));
 *             routeToolCalls(msg);
 *         });
 * }
 * }</pre>
 *
 * <p>Scope: the bridge is generic across consumers of the genai Live transport (the
 * only transport in evidence), owning the pump/merge/dispose concurrency. A genuinely
 * different transport would supply a different bridge.
 */
@Experimental
public final class BidiPetriAgent {

    private BidiPetriAgent() {}

    /**
     * Run the bidirectional pump and return the merged outbound event stream.
     *
     * @param inbound         the ADK live-request queue (from {@code ctx.liveRequestQueue()})
     * @param connection      the consumer's live connection
     * @param runner          the per-session Petri runner
     * @param author          author for connection-derived (model-content) events; typically
     *                        the consumer agent's {@code name()}
     * @param onServerMessage consumer hook: decode and inject signals, route tool calls
     * @return one outbound {@code Flowable<Event>} merging model content and net egress
     */
    public static Flowable<Event> bridge(
            LiveRequestQueue inbound,
            LiveConnection connection,
            PetriRunner runner,
            String author,
            BiConsumer<LiveServerMessage, PetriRunner> onServerMessage) {

        Objects.requireNonNull(inbound, "inbound");
        Objects.requireNonNull(connection, "connection");
        Objects.requireNonNull(runner, "runner");
        Objects.requireNonNull(author, "author");
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

        String invocationId = Event.generateEventId();
        Flowable<Event> modelEvents = connection.rawReceive()
                .doOnNext(msg -> onServerMessage.accept(msg, runner))
                .map(msg -> toEvent(author, invocationId, msg))
                .filter(Optional::isPresent)
                .map(Optional::get);

        return Flowable.merge(modelEvents, runner.adkEvents())
                .doFinally(inputPump::dispose);
    }

    /** Generic egress mapping: a server frame carrying model content becomes an ADK Event. */
    private static Optional<Event> toEvent(String author, String invocationId, LiveServerMessage msg) {
        Optional<Content> modelTurn = msg.serverContent().flatMap(LiveServerContent::modelTurn);
        if (modelTurn.isEmpty()) {
            return Optional.empty();
        }
        return Optional.of(Event.builder()
                .id(Event.generateEventId())
                .invocationId(invocationId)
                .author(author)
                .content(modelTurn.get())
                .build());
    }
}
