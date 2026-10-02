# Reliable Distributed Agent Systems

Distributed-systems rigor applied to autonomous AI infrastructure.

**The goal:** systems in which AI agents cooperate across machines while maintaining
consistent task state, tolerating crashes and adversarial behavior, preventing
duplicate or conflicting actions, and producing verifiable outcomes — engineered and
evaluated as the distributed systems they are, not judged as chat demos.

**The central design principle:** agents are workers, not replicas. The system splits
into a deterministic, replicated **coordination plane** (task ledger, leases, commits —
classical consensus territory) and nondeterministic, sandboxed **agent workers** that
act as *untrusted clients* of that plane. Between them sit **verifiers**: the border
where an agent's claims become the system's facts, by evidence rather than assertion.

## Status

**Active** (started September 2026).

| Stage | State | Highlights |
|---|---|---|
| 0 — [Foundation map](docs/architecture/FOUNDATION_MAP.md) | done | The two-plane principle; the DS mechanisms each agent failure maps to. |
| 1 — [LLM runtime](01-llm-runtime/) | done | Provider-neutral adapter with a receipt per call and a three-way failure taxonomy; free-form vs structured output measured (N=30 per arm: 2.4× cheaper, 1 distinct answer in 30). |
| 2 — [Single agent](02-single-agent/) | done | Framework-free agent loop; a six-step gate on every tool request; a 20-case failure suite (escapes, injection, hangs, runaway loops, partial writes). |
| 3 — [Resumable agent](03-resumable-agent/) | done | Write-ahead journal; state rebuilt by replay; recovery by effect class (re-run / same idempotency key / never retry). Killed mid-action: naive restart duplicates every effect, durable resume performs each once. |
| 4 — [Capability security](04-capability-security/) | mechanisms done; threat model pending | Per-role capability manifests (allow / ask / deny, default deny, secret paths, permitted destinations); tools in a separate MCP-style process with no inherited credentials, killed on timeout; pinned tool declarations; human approval; audit log. |
| 5 — [Evals and observability](05-evals/) | done | Task suite with evidence-based validators, per-run records with configuration hashes and provenance, span traces, regrading, regression reports. First comparison: claude-sonnet-5 matched claude-opus-4-8 on the suite at half the cost (30 runs). |
| 6 — [Multi-agent, one machine](06-multi-agent/) | done | Planner, two concurrent implementers, a test program, a reviewer and a naive coordinator over a content-addressed repository; six coordination failures reproduced on demand by a deterministic scheduler (five end in a silent acceptance). |
| 7 — [Distributed workers](07-distributed-workers/) | done (single host) | Coordinator as a server process; workers as separate processes with their own sandboxes and secrets; signed, versioned, validated RPC; heartbeat failure detection. A killed worker is detected and its job recovered; a frozen worker becomes a zombie whose external effect is duplicated. |
| 8 — [Reliable task ledger](08-reliable-task-ledger/) | done (containers on the lab host) | A durable, journaled ledger with legal transitions only; leases and per-task fencing tokens saved before every reply; an intent before the irreversible send, the task id as idempotency key, deduplication at the receiver. Workers and checkers in containers with no route to the mail service. Eight failure experiments (zombies, crashes between save and reply, lost replies, timeouts) all confirmed: every email delivered exactly once; without the receiver's memory, twice. |
| 9 — Replicated coordination | next | CRDT vs Raft vs BFT for the ledger. |

**Theory companion:** [docs/theory](docs/theory/) states the concepts behind each stage
(definitions, propositions with proofs, common misconceptions) and connects them to the
code and to the literature. It grows with every stage.

## The staircase

The repository grows toward a capstone — a multi-agent system given a bounded
software-engineering objective, coordinating through a replicated control plane to
produce a verified result — through incremental stages, each motivated by a failure
of the previous one:

1. **LLM runtime** — model calls as engineered components: adapters, structured
   output, nondeterminism measured.
2. **Single agent** — the observe/decide/act loop from first principles; the runtime
   authorizes, the model only requests.
3. **Resumable agent** — durable state, checkpointing, crash recovery without
   repeating irreversible work.
4. **Tools, sandboxes, capability security** — least privilege, prompt injection,
   the blast radius of every tool.
5. **Evals and observability** — claims vs evidence; repeatable suites, not anecdotes.
6. **Multi-agent, one machine** — planner/builder/reviewer with the naive failures
   exposed before they are fixed.
7. **Distributed workers** — real process and network boundaries.
8. **Reliable task ledger** — leases, fencing tokens, idempotency, duplicate-safe
   execution semantics.
9. **Replicated coordination** — Raft for the ledger; CRDTs where merging is sound;
   each choice justified.
10. **Byzantine and adversarial agents** — lying workers, forged evidence, and what
    BFT does and does not buy.
11. **Verifiable execution** — evidence-based commit policies.
12. **Chaos and formal models** — fault injection and model-checked coordination
    invariants.

The full stage-by-stage plan — deliverables, failure experiments, exit criteria,
and target weeks through the capstone — is in [ROADMAP.md](ROADMAP.md).

Built on: [distributed-systems-in-rust](https://github.com/luca-zanolini/distributed-systems-in-rust) —
twelve implemented modules from replicated registers to Raft, PBFT, transactional
concurrency control, and CRDTs.

## Author

[Luca Zanolini](https://lucazanolini.com) — PhD in distributed systems (Byzantine
consensus, asymmetric trust); previously Research Scientist at the Ethereum
Foundation (consensus protocol design and security: 3-Slot Finality, RLMD-GHOST,
ePBS security analysis).
