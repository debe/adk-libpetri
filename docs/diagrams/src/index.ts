/**
 * Renders the canonical adk-libpetri net topologies to DOT files.
 *
 * Each function in this module builds one PetriNet via the libpetri npm
 * package and exports it via dotExport. The shell wrapper (npm run build in
 * the parent package.json) then runs graphviz dot to produce SVGs that the
 * README embeds inline.
 *
 * The topologies here mirror the Java subnet sources under
 * java/src/main/java/org/libpetri/adk/subnet/ and the voice demo subnets
 * under java/src/test/java/org/libpetri/adk/demos/voice/. They are not
 * intended to compile-equivalent. They are intended to be readable diagrams.
 */
import {
  PetriNet,
  Transition,
  place,
  one,
  outPlace,
  and,
  xor,
  delayed,
} from 'libpetri';
import { dotExport, type DotConfig } from 'libpetri/export';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';

const OUT_DIR = join(import.meta.dirname, '..', 'dot');

function write(name: string, dot: string): void {
  writeFileSync(join(OUT_DIR, `${name}.dot`), dot);
  process.stdout.write(`wrote dot/${name}.dot\n`);
}

function baseConfig(env: string[], direction: 'LR' | 'TB' = 'LR'): DotConfig {
  return {
    direction,
    showTypes: true,
    showIntervals: true,
    showPriority: true,
    environmentPlaces: new Set(env),
  };
}

// ============================================================
// 1. LlmAgentSubnet: the canonical agent loop.
// ============================================================
function llmAgentSubnet(): void {
  const USER_IN = place<unknown>('USER_IN');
  const EVENT_OUT = place<unknown>('EVENT_OUT');
  const LLM_REQUEST = place<unknown>('LLM_REQUEST');
  const LLM_RESPONSE = place<unknown>('LLM_RESPONSE');
  const TOOL_CALLS = place<unknown>('TOOL_CALLS');
  const TOOL_RESULTS = place<unknown>('TOOL_RESULTS');
  const TRANSFER = place<unknown>('TRANSFER');
  const LEGACY_SESSION_WRITE = place<unknown>('LEGACY_SESSION_WRITE');
  const REASK_BUDGET = place<unknown>('REASK_BUDGET');

  const buildPrompt = Transition.builder('BuildPrompt')
    .inputs(one(USER_IN))
    .outputs(and(outPlace(LLM_REQUEST), outPlace(REASK_BUDGET)))
    .reset(REASK_BUDGET)
    .build();

  const llmCall = Transition.builder('LlmStep_Call')
    .inputs(one(LLM_REQUEST))
    .outputs(outPlace(LLM_RESPONSE))
    .build();

  const route = Transition.builder('Router_Route')
    .inputs(one(LLM_RESPONSE))
    .outputs(xor(outPlace(TOOL_CALLS), outPlace(TRANSFER), and(outPlace(EVENT_OUT), outPlace(LEGACY_SESSION_WRITE))))
    .build();

  const dispatchTools = Transition.builder('ToolDispatch')
    .inputs(one(TOOL_CALLS))
    .outputs(outPlace(TOOL_RESULTS))
    .build();

  const reAsk = Transition.builder('ReAsk')
    .inputs(one(TOOL_RESULTS))
    .outputs(outPlace(LLM_REQUEST))
    .read(REASK_BUDGET)
    .priority(10)
    .build();

  const reAskExhausted = Transition.builder('ReAskExhausted')
    .inputs(one(TOOL_RESULTS))
    .outputs(and(outPlace(EVENT_OUT), outPlace(LEGACY_SESSION_WRITE)))
    .inhibitor(REASK_BUDGET)
    .priority(-10)
    .build();

  const net = PetriNet.builder('LlmAgentSubnet')
    .transition(buildPrompt)
    .transition(llmCall)
    .transition(route)
    .transition(dispatchTools)
    .transition(reAsk)
    .transition(reAskExhausted)
    .build();

  write('llm-agent-subnet', dotExport(net, baseConfig(['USER_IN', 'EVENT_OUT'], 'LR')));
}

// ============================================================
// 2. Stale-result detection: a single commit transition reads
//    LATEST_GENERATION and XOR-routes to COMMITTED or DISCARDED.
//    BumpGeneration, driven by USER_NEW_TURN, resets the generation
//    token and invalidates every in-flight result at once. Each
//    additional commit site is one more transition carrying the same
//    read(LATEST_GENERATION) arc.
// ============================================================
function staleResultValidation(): void {
  const INCOMING_RESULT = place<unknown>('INCOMING_RESULT');
  const LATEST_GENERATION = place<unknown>('LATEST_GENERATION');
  const USER_NEW_TURN = place<unknown>('USER_NEW_TURN');
  const COMMITTED = place<unknown>('COMMITTED');
  const DISCARDED = place<unknown>('DISCARDED');

  const commitResult = Transition.builder('CommitResult')
    .inputs(one(INCOMING_RESULT))
    .read(LATEST_GENERATION)
    .outputs(xor(outPlace(COMMITTED), outPlace(DISCARDED)))
    .build();

  const bumpGeneration = Transition.builder('BumpGeneration')
    .inputs(one(USER_NEW_TURN))
    .reset(LATEST_GENERATION)
    .outputs(outPlace(LATEST_GENERATION))
    .build();

  const net = PetriNet.builder('StaleResultValidation')
    .transition(commitResult)
    .transition(bumpGeneration)
    .build();

  write(
    'stale-result-validation',
    dotExport(
      net,
      baseConfig(['INCOMING_RESULT', 'USER_NEW_TURN', 'COMMITTED', 'DISCARDED'], 'LR'),
    ),
  );
}

// ============================================================
// 3. Speculative race in composition: both paths fire from t=0;
//    slow wins if it completes within the deadline; otherwise the
//    fast result commits when TIMER_EXPIRED lands. At-most-once
//    enforced by inhibitor(RESPONSE_SENT). A user barge-in or new
//    turn atomically cancels the in-flight race and re-arms the
//    lock for the next round via reset arcs on every race place.
// ============================================================
function speculativeRace(): void {
  const REQUEST = place<unknown>('REQUEST');
  const SLOW_INFLIGHT = place<unknown>('SLOW_INFLIGHT');
  const FAST_INFLIGHT = place<unknown>('FAST_INFLIGHT');
  const SLOW_DONE = place<unknown>('SLOW_DONE');
  const FAST_DONE = place<unknown>('FAST_DONE');
  const TIMER_PENDING = place<unknown>('TIMER_PENDING');
  const TIMER_EXPIRED = place<unknown>('TIMER_EXPIRED');
  const RESPONSE = place<unknown>('RESPONSE');
  const RESPONSE_SENT = place<unknown>('RESPONSE_SENT');
  const USER_INTERRUPT = place<unknown>('USER_INTERRUPT');
  const CANCELED = place<unknown>('CANCELED');

  const startBoth = Transition.builder('StartBoth')
    .inputs(one(REQUEST))
    .outputs(and(outPlace(SLOW_INFLIGHT), outPlace(FAST_INFLIGHT), outPlace(TIMER_PENDING)))
    .build();

  const timerFires = Transition.builder('TimerFires')
    .inputs(one(TIMER_PENDING))
    .outputs(outPlace(TIMER_EXPIRED))
    .timing(delayed(2000))
    .build();

  const slowCompletes = Transition.builder('SlowCompletes')
    .inputs(one(SLOW_INFLIGHT))
    .outputs(outPlace(SLOW_DONE))
    .build();

  const fastCompletes = Transition.builder('FastCompletes')
    .inputs(one(FAST_INFLIGHT))
    .outputs(outPlace(FAST_DONE))
    .build();

  const commitSlow = Transition.builder('CommitSlow')
    .inputs(one(SLOW_DONE))
    .inhibitor(RESPONSE_SENT)
    .outputs(and(outPlace(RESPONSE), outPlace(RESPONSE_SENT)))
    .build();

  const commitFastOnTimeout = Transition.builder('CommitFastOnTimeout')
    .inputs(one(FAST_DONE), one(TIMER_EXPIRED))
    .inhibitor(RESPONSE_SENT)
    .outputs(and(outPlace(RESPONSE), outPlace(RESPONSE_SENT)))
    .build();

  const onBargeIn = Transition.builder('OnBargeInOrNewTurn')
    .inputs(one(USER_INTERRUPT))
    .reset(SLOW_INFLIGHT)
    .reset(FAST_INFLIGHT)
    .reset(SLOW_DONE)
    .reset(FAST_DONE)
    .reset(TIMER_PENDING)
    .reset(TIMER_EXPIRED)
    .reset(RESPONSE_SENT)
    .outputs(outPlace(CANCELED))
    .build();

  const net = PetriNet.builder('SpeculativeRace')
    .transition(startBoth)
    .transition(timerFires)
    .transition(slowCompletes)
    .transition(fastCompletes)
    .transition(commitSlow)
    .transition(commitFastOnTimeout)
    .transition(onBargeIn)
    .build();

  write(
    'speculative-race',
    dotExport(net, baseConfig(['REQUEST', 'RESPONSE', 'USER_INTERRUPT', 'CANCELED'], 'LR')),
  );
}

// ============================================================
// 4. Reask-budget pattern (isolated): the SMT-bounded autonomous loop.
// ============================================================
function reaskBudget(): void {
  const TOOL_RESULTS = place<unknown>('TOOL_RESULTS');
  const LLM_REQUEST = place<unknown>('LLM_REQUEST');
  const EVENT_OUT = place<unknown>('EVENT_OUT');
  const REASK_BUDGET = place<unknown>('REASK_BUDGET');

  const reAsk = Transition.builder('ReAsk_priority_10')
    .inputs(one(TOOL_RESULTS), one(REASK_BUDGET))
    .outputs(outPlace(LLM_REQUEST))
    .priority(10)
    .build();

  const exhausted = Transition.builder('ReAskExhausted_priority_minus_10')
    .inputs(one(TOOL_RESULTS))
    .outputs(outPlace(EVENT_OUT))
    .inhibitor(REASK_BUDGET)
    .priority(-10)
    .build();

  const net = PetriNet.builder('ReaskBudgetPattern')
    .transition(reAsk)
    .transition(exhausted)
    .build();

  write('reask-budget', dotExport(net, baseConfig(['TOOL_RESULTS', 'LLM_REQUEST', 'EVENT_OUT'], 'LR')));
}

// ============================================================
// 5. BIDI composition: streaming + barge-in + silence recovery.
// ============================================================
function bidiComposition(): void {
  const USER_IN = place<unknown>('USER_IN');
  const CHUNK_OUT = place<unknown>('CHUNK_OUT');
  const INTERRUPTED = place<unknown>('INTERRUPTED');
  const VOICE_ACTIVITY_OPEN = place<unknown>('VOICE_ACTIVITY_OPEN');
  const MODEL_ACTIVE = place<unknown>('MODEL_ACTIVE');
  const RESPONSE_AWAITED = place<unknown>('RESPONSE_AWAITED');
  const EVENT_OUT = place<unknown>('EVENT_OUT');

  const LLM_STREAMING = place<unknown>('LLM_STREAMING');
  const CHUNK_BUDGET = place<unknown>('CHUNK_BUDGET');
  const NUDGE_SENT = place<unknown>('NUDGE_SENT');
  const RECONNECT_NEEDED = place<unknown>('RECONNECT_NEEDED');
  const BARGE_DISCARDED = place<unknown>('BARGE_DISCARDED');

  const beginStream = Transition.builder('BeginStream')
    .inputs(one(USER_IN))
    .outputs(and(outPlace(LLM_STREAMING), outPlace(CHUNK_BUDGET), outPlace(CHUNK_BUDGET), outPlace(CHUNK_BUDGET)))
    .reset(CHUNK_BUDGET)
    .build();

  const emitChunk = Transition.builder('EmitChunk')
    .inputs(one(CHUNK_BUDGET))
    .outputs(and(outPlace(CHUNK_BUDGET), outPlace(CHUNK_OUT)))
    .read(LLM_STREAMING)
    .build();

  const bargeSend = Transition.builder('BargeInSend')
    .inputs(one(INTERRUPTED))
    .outputs(outPlace(EVENT_OUT))
    .read(VOICE_ACTIVITY_OPEN)
    .build();

  const bargeDiscard = Transition.builder('BargeInDiscard')
    .inputs(one(INTERRUPTED))
    .outputs(outPlace(BARGE_DISCARDED))
    .inhibitor(VOICE_ACTIVITY_OPEN)
    .build();

  const nudge = Transition.builder('NudgeAfterTn')
    .inputs(one(RESPONSE_AWAITED))
    .outputs(and(outPlace(RESPONSE_AWAITED), outPlace(NUDGE_SENT)))
    .inhibitor(MODEL_ACTIVE)
    .timing(delayed(2000))
    .build();

  const reconnect = Transition.builder('ReconnectAfterTr')
    .inputs(one(RESPONSE_AWAITED), one(NUDGE_SENT))
    .outputs(outPlace(RECONNECT_NEEDED))
    .inhibitor(MODEL_ACTIVE)
    .timing(delayed(5000))
    .build();

  const net = PetriNet.builder('BidiComposition')
    .transition(beginStream)
    .transition(emitChunk)
    .transition(bargeSend)
    .transition(bargeDiscard)
    .transition(nudge)
    .transition(reconnect)
    .build();

  write(
    'bidi-composition',
    dotExport(
      net,
      baseConfig(
        ['USER_IN', 'CHUNK_OUT', 'INTERRUPTED', 'VOICE_ACTIVITY_OPEN', 'MODEL_ACTIVE', 'RESPONSE_AWAITED', 'EVENT_OUT'],
        'LR',
      ),
    ),
  );
}

// ============================================================
// 6. Stateful monitor in composition: COLLECTOR holds batch-scoped
//    state, CollectResult merges and XOR-routes, an orthogonal
//    subnet reads COLLECTOR via a read arc for atomic snapshots,
//    and OnNewUserTurn resets the in-flight batch on a new turn.
// ============================================================
function statefulMonitor(): void {
  const SEARCH_REQUEST = place<unknown>('SEARCH_REQUEST');
  const COLLECTOR = place<unknown>('COLLECTOR');
  const JOB_A = place<unknown>('JOB_A');
  const JOB_B = place<unknown>('JOB_B');
  const JOB_C = place<unknown>('JOB_C');
  const SEARCH_RESULT = place<unknown>('SEARCH_RESULT');
  const SELECTION_READY = place<unknown>('SELECTION_READY');
  const OTHER_INPUT = place<unknown>('OTHER_INPUT');
  const OTHER_DOWNSTREAM = place<unknown>('OTHER_DOWNSTREAM');
  const USER_NEW_TURN = place<unknown>('USER_NEW_TURN');

  const spawnJobs = Transition.builder('SpawnJobs')
    .inputs(one(SEARCH_REQUEST), one(COLLECTOR))
    .outputs(and(outPlace(JOB_A), outPlace(JOB_B), outPlace(JOB_C), outPlace(COLLECTOR)))
    .build();

  const workerA = Transition.builder('WorkerA')
    .inputs(one(JOB_A))
    .outputs(outPlace(SEARCH_RESULT))
    .build();
  const workerB = Transition.builder('WorkerB')
    .inputs(one(JOB_B))
    .outputs(outPlace(SEARCH_RESULT))
    .build();
  const workerC = Transition.builder('WorkerC')
    .inputs(one(JOB_C))
    .outputs(outPlace(SEARCH_RESULT))
    .build();

  const collectResult = Transition.builder('CollectResult')
    .inputs(one(SEARCH_RESULT), one(COLLECTOR))
    .outputs(xor(outPlace(COLLECTOR), and(outPlace(COLLECTOR), outPlace(SELECTION_READY))))
    .build();

  const orthogonalRead = Transition.builder('OrthogonalRead')
    .inputs(one(OTHER_INPUT))
    .read(COLLECTOR)
    .outputs(outPlace(OTHER_DOWNSTREAM))
    .build();

  const onNewUserTurn = Transition.builder('OnNewUserTurn')
    .inputs(one(USER_NEW_TURN))
    .reset(COLLECTOR)
    .reset(JOB_A)
    .reset(JOB_B)
    .reset(JOB_C)
    .reset(SEARCH_RESULT)
    .outputs(outPlace(COLLECTOR))
    .build();

  const net = PetriNet.builder('StatefulMonitor')
    .transition(spawnJobs)
    .transition(workerA)
    .transition(workerB)
    .transition(workerC)
    .transition(collectResult)
    .transition(orthogonalRead)
    .transition(onNewUserTurn)
    .build();

  write(
    'stateful-monitor',
    dotExport(
      net,
      baseConfig(
        ['SEARCH_REQUEST', 'SELECTION_READY', 'OTHER_INPUT', 'OTHER_DOWNSTREAM', 'USER_NEW_TURN'],
        'LR',
      ),
    ),
  );
}

// ============================================================
// Main.
// ============================================================
llmAgentSubnet();
staleResultValidation();
speculativeRace();
reaskBudget();
bidiComposition();
statefulMonitor();
process.stdout.write('done.\n');
