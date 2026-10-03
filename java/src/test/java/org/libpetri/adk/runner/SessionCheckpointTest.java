package org.libpetri.adk.runner;

import static com.google.common.truth.Truth.assertThat;

import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.subscribers.TestSubscriber;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.libpetri.adk.colours.AdkColours;
import org.libpetri.adk.subnet.LlmAgentSubnet;
import org.libpetri.core.Arc;
import org.libpetri.core.PetriNet;
import org.libpetri.core.Place;
import org.libpetri.core.Transition;

class SessionCheckpointTest {

    private static final SessionKey KEY = new SessionKey("app", "user", "session");

    private static ExecutorService EXECUTOR;

    @BeforeAll
    static void setupExecutor() {
        EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    }

    @AfterAll
    static void shutdownExecutor() {
        EXECUTOR.shutdown();
    }

    /**
     * A session closed at rest is saved, and a new runner for the same key
     * resumes from it: the invocation's conversation and the unspent reask
     * budget are back in the marking, as they were.
     */
    @Test
    void a_session_closed_at_rest_resumes_with_its_marking() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var net = agentNet();
        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints)) {
            var runner = registry.getOrCreate(KEY, key -> startResuming(net, checkpoints, key));
            TestSubscriber<Event> egress = runner.adkEvents().test();
            runner.inject(AdkColours.USER_IN, userMessage("remember me")).get(1, TimeUnit.SECONDS);
            egress.awaitCount(1);
            var before = runner.snapshot();

            assertThat(registry.close(KEY)).isTrue();
            var saved = checkpoints.load(KEY).orElseThrow();
            assertThat(saved).isEqualTo(before.marking());
            assertThat(saved).containsKey(LlmAgentSubnet.CONVERSATION.name());

            var resumed = registry.getOrCreate(KEY, key -> startResuming(net, checkpoints, key));
            var conversation = (LlmAgentSubnet.Conversation)
                    resumed.snapshot().marking().get(LlmAgentSubnet.CONVERSATION.name())
                            .getFirst().value();
            assertThat(conversation.turns()).containsExactly(userMessage("remember me"));
            assertThat(resumed.snapshot().marking().get(LlmAgentSubnet.REASK_BUDGET.name()))
                    .hasSize(3);
        }
    }

    /**
     * A snapshot taken while an action runs would miss the tokens that action
     * consumed, and restoring it would silently lose them. Such a session is
     * not checkpointed at all.
     */
    @Test
    void a_session_with_an_action_in_flight_is_not_checkpointed() throws Exception {
        var checkpoints = SessionCheckpointStore.inMemory();
        var release = new CountDownLatch(1);
        var started = new CountDownLatch(1);
        var work = Place.of("work", String.class);
        var done = Place.of("done", String.class);
        var net = PetriNet.builder("slow")
                .transition(Transition.builder("Slow_Work")
                        .inputs(Arc.In.one(work))
                        .outputs(Arc.Out.place(done))
                        .action(ctx -> {
                            String in = ctx.input(work);
                            started.countDown();
                            return CompletableFuture.runAsync(() -> {
                                try {
                                    release.await();
                                } catch (InterruptedException e) {
                                    Thread.currentThread().interrupt();
                                }
                                ctx.output(done, in);
                            }, EXECUTOR);
                        })
                        .build())
                .build();

        try (var registry = SessionExecutorRegistry.strongOwned(checkpoints)) {
            var runner = registry.getOrCreate(KEY, key -> PetriRunner.builder(net)
                    .environmentPlace(work)
                    .orchestratorExecutor(EXECUTOR)
                    .start());
            runner.inject(work, "job").get(1, TimeUnit.SECONDS);
            assertThat(started.await(1, TimeUnit.SECONDS)).isTrue();

            // close() waits out the checkpoint window, then drains; the drain
            // needs the action to finish, so let it go once the window passed.
            var closing = CompletableFuture.runAsync(() -> registry.close(KEY), EXECUTOR);
            Thread.sleep(2_500);
            release.countDown();
            closing.get(5, TimeUnit.SECONDS);

            assertThat(checkpoints.load(KEY)).isEmpty();
        }
    }

    private static PetriRunner startResuming(PetriNet net, SessionCheckpointStore store, SessionKey key) {
        return PetriRunner.builder(net)
                .environmentPlace(AdkColours.USER_IN)
                .resumeFrom(store, key)
                .orchestratorExecutor(EXECUTOR)
                .start();
    }

    private static PetriNet agentNet() {
        var llm = new BaseLlm("echo") {
            @Override public Flowable<LlmResponse> generateContent(LlmRequest r, boolean s) {
                return Flowable.just(LlmResponse.builder()
                        .content(Content.builder().role("model")
                                .parts(List.of(Part.fromText("ok"))).build())
                        .build());
            }
            @Override public BaseLlmConnection connect(LlmRequest r) {
                throw new UnsupportedOperationException();
            }
        };
        var config = LlmAgentSubnet.Config.builder("agent", "fake-model")
                .reaskBudget(3)
                .dispatchExecutor(EXECUTOR)
                .build();
        return PetriNet.builder("checkpointed")
                .compose(LlmAgentSubnet.DEF)
                .build()
                .bindActions(LlmAgentSubnet.actionBindings(llm, config));
    }

    private static Content userMessage(String text) {
        return Content.builder().role("user").parts(List.of(Part.fromText(text))).build();
    }
}
