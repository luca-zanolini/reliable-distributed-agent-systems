# Roadmap

This project grows toward its capstone — a multi-agent system that takes a bounded
software-engineering objective and produces a verified result through a replicated
control plane — in staged increments. Each stage ends by breaking the system it
just built: the failure experiment that motivates the next stage.

Week numbers are targets at a cadence of roughly four focused days per week, counted
from project start (September 2026). Each stage follows the same rhythm: design notes
first, then the build, then the failure experiment and measurements. The schedule is
a commitment device, not a promise; stages that surface something interesting are
allowed to run long.

## Overview

| Stage | Weeks | Focus | Exit criterion |
|---|---|---|---|
| 0 | 1 | Foundation map | DS concepts mapped to agent-system load points ✅ |
| 1 | 2–3 | LLM runtime | Model calls engineered: adapters, schemas, measured nondeterminism |
| 2 | 4–5 | Single agent | Hand-built loop survives the malformed-tool suite |
| 3 | 6–7 | Durable state & recovery | Kill −9 mid-task; resume without repeating irreversible work |
| 4 | 8–9 | Tools, sandboxes, capability security | Threat model closes a real exfiltration path |
| 5 | 10–11 | Evals & observability | Changes judged by a repeatable suite, not anecdotes |
| 6 | 12–13 | Multi-agent, one machine | Naive coordination failures reproduced on demand |
| 7 | 14–15 | Distributed sandboxed workers | Worker death cannot corrupt peers; coordinator recovers |
| 8 | 16–17 | Reliable task ledger | Explicit semantics for every retry/duplicate scenario |
| 9 | 18–20 | Replicated coordination | Every consistency choice justified: CRDT vs Raft vs BFT |
| 10 | 21–22 | Byzantine & adversarial agents | Precise statement of provided (and not provided) Byzantine safety |
| 11 | 23 | Verifiable agent work | Assertion-only completion rejected on every commit path |
| 12 | 24–25 | Chaos & formal models | One coordination invariant model-checked and fault-tested |
| 13 | 26 | Multi-machine deployment | Survives real machine disappearance per declared fault model |
| Capstone | 27–33 | End-to-end verified multi-agent system | Milestones C1–C12 below |

---

## Stage 0 — Foundation Map *(complete)*

Connect the completed [distributed-systems-in-rust](https://github.com/luca-zanolini/distributed-systems-in-rust)
course to this system's design: which classical mechanism carries which agent-system
load. Deliverable: [FOUNDATION_MAP.md](docs/architecture/FOUNDATION_MAP.md).

## Stage 1 — LLM Runtime *(complete)*

Model calls as engineered components, not magic.

- **Build:** a provider-neutral adapter; structured outputs with schema validation;
  retry and error policy; token/cost accounting.
- **Measure:** the same task N times — free-form vs structured output; variance,
  latency, cost. Nondeterminism is confined to non-load-bearing places, not wished away.
- **Exit:** conversation state is owned and persisted by the application; the model
  call is a stateless function with a receipt.

## Stage 2 — Single Agent *(complete)*

The observe/decide/act loop from first principles, no framework.

- **Build:** minimal loop over small tools (read, list, search, safe compute);
  step budgets and stopping conditions. The model **requests** actions; the runtime
  **authorizes and executes** them.
- **Break:** unknown tool requested, invalid arguments, tool timeout, repeated tool
  loop, oversized output, tool failure mid-action.
- **Exit:** the loop survives the full malformed-tool suite with explicit handling
  for each case.

## Stage 3 — Durable State, Checkpointing, Recovery *(complete)*

From toy loop to resumable system.

- **Build:** durable run state (objective, step, tool calls, artifacts, checkpoints);
  event history distinct from model context; replay on restart.
- **Break:** kill the process mid-task; restart; resume.
- **Exit:** no completed irreversible action is silently repeated. Every part of a
  run is classified: safe to replay, or requiring idempotency / a durable record
  before retry. (Write-ahead logging and state-machine recovery, wearing new clothes.)

## Stage 4 — Tools, MCP, Sandboxes, Capability Security *(weeks 8–9)*

Useful work without ambient authority.

- **Build:** a small MCP/tool server; a sandboxed worker; an explicit capability
  manifest per role (allow / ask / deny).
- **Threat-model:** prompt injection (direct and indirect), tool poisoning,
  confused-deputy paths, exfiltration through nominally read-only tools.
- **Exit:** the documented threat model identifies at least one realistic
  exfiltration path — and the capability policy closes it. Every external input is
  treated as untrusted.

## Stage 5 — Evals, Verification, Observability *(weeks 10–11)*

No more judging the system by vibes.

- **Build:** an eval harness recording per run: task, configuration, result,
  validator verdict, latency, cost, tool calls, failure category, artifact hashes.
- **Principle:** an agent claim ("tests pass") is not evidence (exit code, report
  hash, commit, environment). The suite grows with every stage after this one;
  important bugs become regression cases.
- **Exit:** a model or prompt change is evaluated against the suite, not an anecdote.

## Stage 6 — Multi-Agent Coordination, One Machine *(weeks 12–13)*

Planner / implementer / tester / reviewer over a bounded software task, with a
trusted central coordinator — and the naive failures deliberately left in.

- **Break (on purpose):** two workers claim one task; reviewer reads a stale commit;
  tester tests version A while the implementer moves to B; duplicate execution of a
  finished action; conflicting edits; coordinator crash.
- **Exit:** each failure reproduced on demand and documented — the requirements list
  for stages 7–9. Multi-agent structure is justified by parallelism, specialization,
  isolation, and independent verification, or not used at all.

## Stage 7 — Distributed Sandboxed Workers *(weeks 14–15)*

Real process and network boundaries; a dedicated always-on lab host comes online.

- **Build:** each worker in its own sandbox behind RPC; timeouts, retries,
  serialization, worker identity, node authentication, health checks.
- **Exit:** killing one worker cannot corrupt another worker's state, and the
  coordinator detects and recovers from the loss.

## Stage 8 — Reliable Task Ledger & Execution Semantics *(weeks 16–17)*

The first serious coordination substrate: authoritative task state.

- **Build:** a durable single-node task service — task states and legal transitions
  (PENDING → CLAIMED → RUNNING → PROPOSED → VERIFIED → COMMITTED, plus failure
  paths), leases with expiration, fencing tokens, idempotency keys, durable intent
  before external action.
- **Break:** worker dies after claiming; finishes after lease expiry; duplicate
  completion; stale proposal; coordinator crash between intent and reply;
  ambiguous timeout on an irreversible action.
- **Exit:** explicit, documented semantics for every retry and duplicate scenario
  in the declared scope. "Exactly once" is treated as an end-to-end property, never
  a transport guarantee.

## Stage 9 — Replicated Coordination: CRDT vs Raft vs BFT *(weeks 18–20)*

The coordination layer becomes fault tolerant — with the correct primitive per state.

- **Classify:** for every shared object — eventual convergence, causal, total order,
  linearizable, quorum-approved, or Byzantine-safe. The ledger replicates; LLM
  thought does not.
- **Build:** a crash-fault-tolerant replicated ledger (Raft territory); mergeable
  CRDT state only where merge semantics are genuinely sound (annotations, telemetry —
  never ornamentally).
- **Break:** leader crash, failover, partition; stale-leader protection;
  no double assignment across committed state.
- **Exit:** every replicated component carries a written justification —
  consistency requirement, failure model, mechanism, why anything weaker fails,
  why anything stronger is wasted cost.

## Stage 10 — Byzantine & Adversarial Agents *(weeks 21–22)*

What "Byzantine" precisely means when workers are LLMs — and what it does not.

- **Build:** a Byzantine-worker simulator: false completions, equivocation, stale
  artifacts, fabricated test results, malicious tool requests, poisoned messages.
- **Design question:** workers as untrusted clients of a deterministic BFT control
  plane, versus agent nodes voting with evidence-backed claims, versus independent
  verifiers attesting to deterministic evidence. Which yields *meaningful* security.
- **Exit:** a precise statement of the Byzantine safety the system provides — and
  what it deliberately does not provide. A hallucination is not automatically a
  Byzantine replica; the fault model says which processes may equivocate and what
  is authenticated.

## Stage 11 — Verifiable Agent Work *(week 23)*

Evidence over confidence.

- **Build:** commit policies requiring declared evidence — pinned commits, content
  hashes, test reports, environment versions, independent verifier attestations;
  human approval where machine verification is insufficient.
- **Principle:** multiple LLMs agreeing is not independent evidence; correlated
  models share failure modes. One first-hand artifact beats a quorum of correlated
  opinions.
- **Exit:** every high-value commit path rejects assertion-only completion.

## Stage 12 — Chaos, Adversarial Testing, Formal Models *(weeks 24–25)*

Test it like a distributed system, not like a chatbot.

- **Build:** a repeatable fault-injection harness — crash, restart, delay, drop,
  duplication, reordering, partition, retry storm, malicious agent, injected prompt.
- **Formalize:** a small model of task states, leases/fencing, duplicate completion,
  and commit rules; safety ("no two conflicting committed artifacts", "an expired
  worker cannot overwrite a newer lease holder") and liveness ("abandoned tasks are
  eventually reclaimable") stated precisely.
- **Exit:** at least one nontrivial coordination invariant is written precisely,
  tested under randomized fault schedules, and model-checked.

## Stage 13 — Multi-Machine Deployment *(week 26)*

From simulated distribution to independent failure domains: control plane and
workers across genuinely independent machines/VMs.

- **Exit:** at least one earlier failure experiment repeated on real machines,
  compared against its single-host simulation; the system survives a real
  worker-machine disappearance according to its declared fault model.

---

## Capstone *(weeks 27–33)*

A bounded software-engineering objective ("implement feature X preserving invariant
Y, passing suite Z") executed by coordinating autonomous agents. The project is
about the infrastructure, not the impressiveness of the generated application.

| Milestone | Week | Success criterion |
|---|---|---|
| C1 — Multi-agent task, trusted coordinator | 27 | Planner decomposes; workers claim; artifacts version-pinned; output validated |
| C2 — Durable task ledger | 27 | Coordinator restart loses nothing; completed actions not repeated |
| C3 — Leases, fencing, idempotency | 28 | Dead worker's task reassigned; returning stale worker safely rejected |
| C4 — Replicated coordinator | 29 | Leader crash → failover; committed state survives; no conflicting assignment |
| C5 — Real sandbox isolation | 30 | Per-role capabilities; scoped secrets; no ambient tool access |
| C6 — Evidence-based commit | 30 | "Tests passed" requires the declared evidence, not assertion |
| C7 — Byzantine worker | 31 | Lying/equivocating worker cannot violate the declared threat model |
| C8 — Byzantine coordination | 32 | n = 3f+1 control plane; equivocation; quorum certificates; view change; safety holds |
| C9 — Network chaos | 32 | Delay, drop, duplication, reordering, partition, restart — under the declared model |
| C10 — Multi-machine deployment | 33 | Same protocol tests pass across independent machines |
| C11 — Quantitative evaluation | 33 | Completion, duplicate-work and conflict rates; failover and recovery times; cost |
| C12 — Final report | 33 | `docs/FINAL_REPORT.md`: system model, threat model, protocol choices, results, limitations, open questions |

## Continuous tracks

Three concerns run through every stage rather than appearing as final modules:
**security** (least privilege, injection, exfiltration, audit — from the first tool
call onward), **evaluation** (every stage adds suite categories; every important bug
becomes a regression test), and **observability** (if a distributed failure cannot
be reconstructed from traces, the missing telemetry is part of the bug).

## Destination

A system in which autonomous AI workers receive a high-level objective, acquire
tasks safely, act inside constrained sandboxes, survive crashes and partitions,
avoid duplicate or conflicting irreversible actions, coordinate through a justified
replicated state layer, tolerate the declared crash or Byzantine faults, and commit
only versioned artifacts carrying verifiable evidence — with every guarantee, and
every limit of every guarantee, stated precisely:

> What is replicated, why it needs that consistency level, what can fail, what the
> protocol guarantees, what evidence makes an agent's result trustworthy — and where
> the system's guarantees stop.
