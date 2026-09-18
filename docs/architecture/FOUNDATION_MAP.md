# Foundation Map

*How classical distributed-systems mechanisms — implemented from scratch in
[distributed-systems-in-rust](https://github.com/luca-zanolini/distributed-systems-in-rust) —
become load-bearing in a reliable multi-agent system. This document fixes the
project's architecture vocabulary and design principles before any code exists.*

## The architecture in one paragraph

The system splits into two worlds joined by a border. The **coordination plane** is
classical distributed systems, unchanged: a small set of replicas running crash-fault
consensus over a deterministic task ledger (assignments, leases, commits, artifact
hashes). The **agent workers** are *not replicas* — they are untrusted,
nondeterministic, sandboxed **clients** of that plane, individually disposable and
replaceable. Between them sit **verifiers**: the border where a worker's *claims*
become the plane's *facts*, by evidence rather than assertion. Nondeterminism stays
outside the deterministic core; the model requests, the runtime decides; the worker
claims, the verifier establishes.

## Concept map

| DS mechanism (course module) | Role in the agent system | Load-bearing in |
|---|---|---|
| Failure detectors, ◇P; wrong suspicion must be harmless (05) | Worker-liveness timeouts: impatience is a liveness decision, never a safety one | Task ledger (leases) |
| Leader election, terms; stale leaders rejected by number (05, 07) | **Leases + fencing tokens**: time-bounded task ownership carrying a monotone epoch; a stale worker's writes bounce arithmetically | Task ledger, takeover semantics |
| Raft: replicated log, majority commit, failover (07) | The coordination plane itself: a replicated task ledger that survives coordinator crash with no double-assignment | Replicated coordination |
| Persist-before-externalize, write-ahead intent, fsync (08, 11) | Durable intent *before* any external action; resumable runs; crash recovery that never repeats irreversible work | Agent runtime, task ledger |
| Atomic commitment and its blocking window (08) | Multi-step agent actions that commit atomically; why leases must expire rather than wait | Task ledger |
| Idempotency; exactly-once as an end-to-end property (02, 09) | Idempotency keys on tool actions; ambiguous retries resolved by durable intent → keyed action → durable outcome | Tool gateway |
| Versioning and optimistic validation (04, 09) | Artifact version pinning; detecting reviewers acting on stale commits | Artifact store, commit policy |
| Vector clocks, causal broadcast (06) | Causal ordering of agent observations; transport for operation-based replication | Knowledge plane |
| CRDTs, strong eventual consistency, gossip (12) | Mergeable shared knowledge (annotations, telemetry, discovered facts) with zero coordination — and the boundary: invariants spanning concurrent updates remain consensus problems | Knowledge plane |
| Byzantine reliable broadcast, quorum intersection (10) | Reliable dissemination among mutually distrusting participants | Adversarial-worker experiments |
| BFT consensus, certificates, authenticated view change (11) | A BFT control-plane mode *only where replicas can genuinely lie*; the evidence-collection shape behind commit policies | High-value decisions (if justified) |
| First-hand evidence vs hearsay; sign-everything (10, 11) | **Claims vs evidence**: a deterministically checkable claim needs one reproducible artifact, not a vote — a quorum of correlated model opinions is not independence | Verifiers, commit policy |
| Stable storage vs self-equivocation (11) | Why worker and coordinator state must persist across restarts before anything is signed or claimed | Agent runtime |
| Protocol selection: crash vs Byzantine, cost of stronger machinery (05–11) | Per-component consistency choice — CRDT / Raft / BFT / leases — each justified, none ornamental | Every design review |

## Five founding problems

Design answers this project commits to, stated up front:

1. **The slow-not-dead worker.** A worker misses its deadline; the task must be
   reclaimable — but the worker may merely be slow, and no timeout can tell the
   difference. *Answer:* leases with fencing tokens. The timeout decides when to get
   impatient (liveness); the token decides whose writes count (safety). A wrongly
   suspected worker wastes compute; it cannot corrupt state.
2. **The dying coordinator.** Committed task assignments must survive coordinator
   failure with no task ending up owned twice. *Answer:* a Raft-replicated ledger.
   Crash faults on machines we own do not justify BFT; protocol selection is a cost
   decision, not a maximalism contest.
3. **The ambiguous retry.** A side-effecting tool call times out with unknown
   outcome. *Answer:* durable intent before the action, an idempotency key on it,
   durable outcome after. Exactly-once is an end-to-end property, never a transport
   guarantee.
4. **The lying reviewer.** A worker claims "tests pass"; the claim may be wrong,
   stale, or injected. *Answer:* evidence-based commit — the ledger accepts a
   reproducible artifact (base commit, test command, exit code, environment hash),
   not an assertion, and not a vote of correlated model opinions.
5. **The offline notebooks.** Concurrent, partitioned agents accumulate observations
   that must converge. *Answer:* CRDTs for the mergeable knowledge plane — with the
   standing caveat that any invariant spanning concurrent updates is out of CRDT
   scope by nature.

## Glossary

**LLM** — a stateless function from context to text; it remembers nothing and
executes nothing. **Agent** — the loop around it: observe, let the model decide,
execute what the runtime authorizes, feed back the result. **Agent worker** — a
sandboxed, leased, role-specialized agent employed by the system. **Orchestrator** —
decides what work exists and who does it; a client of the plane, not a privileged
component. **Coordination service** — the deterministic, durable store of shared
truth. **Replicated coordination state** — the subset of that truth that must survive
machine failure. **Artifact** — immutable evidence of work: a hash, a commit, a
report. **Verifier** — turns claims into evidence, deterministically wherever
possible.
