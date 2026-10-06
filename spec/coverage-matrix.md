# Coverage matrix

Requirement -> the tests that cover it in each port. Paths are relative to
`java/src/test/java/org/libpetri/adk/` and `python/tests/`.

| Req | Java | Python |
|---|---|---|
| COL-001..004 | `subnet/*SubnetTest`, `demos/RawProviderPassthroughDemoTest` | `conformance/test_net_fixtures.py`, `demos/test_raw_provider_passthrough_demo.py` |
| COL-005 | (compile-time in Java) | `subnet/test_subnet_actions.py` (`NetSpec.compose` type check) |
| SUB-001 | `subnet/LlmStepSubnetTest` | `subnet/test_llm_step_subnet.py` |
| SUB-002 | `subnet/RouterSubnetTest` | `subnet/test_router_subnet.py` |
| SUB-003 | `subnet/ToolDispatchSubnetTest` | `subnet/test_tool_dispatch_subnet.py` |
| SUB-004 | `subnet/TransferRouterSubnetTest` | `subnet/test_transfer_router_subnet.py` |
| SUB-005 | `subnet/PersistStateSubnetTest` | `subnet/test_persist_state_subnet.py` |
| SUB-006..008 | `subnet/LlmAgentSubnetTest` | `subnet/test_llm_agent_subnet.py` |
| SUB-009..010 | `subnet/LlmStreamingStepSubnetTest`, `runner/PetriAgentSseTest` | `subnet/test_llm_streaming_step_subnet.py`, `runner/test_petri_agent_sse.py` |
| all SUB structure | `spec/SpecFixturesTest` | `conformance/test_net_fixtures.py` |
| RUN-001..008 | `runner/PetriRunnerTest`, `bridge/EventStoreToFlowableBridgeTest` | `runner/test_petri_runner.py`, `bridge/test_event_store_bridge.py`, `test_aio.py` |
| RUN-006..007 | `runner/SessionCheckpointTest` | `runner/test_session_checkpoint.py` |
| AGT-001..004 | `runner/PetriAgentIntegrationTest` | `runner/test_petri_agent_integration.py` |
| AGT-002 (SSE) | `runner/PetriAgentSseTest` | `runner/test_petri_agent_sse.py` |
| AGT-005 | `bridge/OtelEventStoreTest`, `demos/MultiAgentDemoTest` | `bridge/test_otel_event_store.py`, `demos/test_multi_agent_demo.py` |
| AGT-006 | `runner/PetriAgentLiveTest`, `runner/BidiPetriAgentTest` | `runner/test_petri_agent_live.py`, `runner/test_bidi_petri_agent.py` |
| AGT-007 | (Python only) | `runner/test_petri_agent_integration.py` (Workflow node) |
| REG-001..006 | `runner/SessionExecutorRegistryTest`, `runner/SessionExecutorRegistryStrongOwnedTest`, `runner/SessionCheckpointTest` | `runner/test_session_registry_finalizer_owned.py`, `runner/test_session_registry_strong_owned.py`, `runner/test_session_checkpoint.py` |
| VER-001..006 | `verify/StockSubnetProofsTest` | `verify/test_stock_subnet_proofs.py` |
| VER-007 | `verify/AdkNetInvariantsTest` | `verify/test_adk_net_invariants.py` |
| WF-001 | (Python only) | `workflow/test_runtime_parity.py`, `workflow/adk_samples/*` (ADK's own samples) |
| WF-002..005, 007..009 | (Python only) | `workflow/test_compile.py`, `workflow/test_runtime_parity.py` |
| WF-010 | (Python only) | `workflow/adk_samples/loop_config/test_petri_yaml.py` |
| WF-006 | (Python only) | `workflow/test_runtime_parity.py::test_request_input_interrupts_and_resumes_like_adk` |

## Demos and foils

| Demo | Java | Python |
|---|---|---|
| Multi-agent transfer | `demos/MultiAgentDemoTest` | `demos/test_multi_agent_demo.py` |
| Unknown transfer target (ADK foil) | `demos/TransferUnknownTargetAdkFoilTest` | `demos/test_transfer_unknown_target_adk_foil.py` (Python ADK raises `ValueError` instead) |
| Scroll-aware env place | `demos/ScrollAwareDemoTest` | `demos/test_scroll_aware_demo.py` |
| Checkpoint in ADK session history | `demos/AgentStateCheckpointStoreTest` | `demos/test_agent_state_checkpoint_store.py` |
| Direct model client | `demos/SyncGeminiLlmTest` | `demos/test_direct_genai_llm.py` (optional in Python) |
| Voice session | `demos/VoiceSessionDemoTest` | `demos/test_voice_session_demo.py` |
| VAD edges (ADK foil) | `demos/VoiceVadEdgeAdkFoilTest` | `demos/test_voice_vad_edge_adk_foil.py` (flipped: Python ADK keeps the edges) |
| Barge-in, VAD, Live recovery | `demos/voice/*SubnetTest` | `demos/voice/test_*_subnet.py` |
| Live connection exemplar | `demos/SyncGeminiLiveConnectionTest` | `demos/voice/test_genai_live_connection.py` |
| Patterns A/B/C | `demos/patterns/Pattern*DemoTest` | `demos/patterns/test_pattern_*_demo.py` |
| Patterns A/B/C (ADK foils) | `demos/patterns/Pattern*_AdkOnlyFoilTest` | `demos/patterns/test_pattern_*_adk_only_foil.py` (against ADK 2 `Workflow`) |
