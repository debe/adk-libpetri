package org.libpetri.adk.runner;

import com.google.adk.models.BaseLlmConnection;
import com.google.genai.types.LiveServerMessage;
import io.reactivex.rxjava3.core.Flowable;
import org.libpetri.adk.Experimental;

/**
 * The live-connection contract the genai Live transport needs that ADK's
 * {@link BaseLlmConnection} lacks.
 *
 * <p>ADK's {@code BaseLlmConnection.receive()} returns
 * {@code Flowable<LlmResponse>}, which flattens each server frame and <b>drops</b>
 * the server-side voice-activity edges (Gemini's automatic VAD start/end). Those
 * edges are public, typed genai API. {@link #rawReceive()} exposes the
 * unabstracted {@link LiveServerMessage} stream so a {@link BidiPetriAgent#bridge}
 * consumer can decode the signals its net cares about (speech start/stop, barge-in
 * interrupt, turn complete) and inject them via {@link PetriRunner#signal(org.libpetri.core.Place)}.
 *
 * <p>The library ships no implementation: the wire binding is transport/SDK-specific
 * (genai SDK version, websocket lifecycle, the non-daemon-thread close quirk). Consumers
 * supply their own; {@code SyncGeminiLiveConnection} (under {@code demos/}) is the
 * copy-and-adapt exemplar over genai's {@code client.async.live}.
 */
@Experimental
public interface LiveConnection extends BaseLlmConnection {

    /** Raw inbound server stream, carrying the signals ADK's {@code LlmResponse} drops. */
    Flowable<LiveServerMessage> rawReceive();
}
