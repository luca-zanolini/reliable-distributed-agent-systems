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

**Early and active** (started September 2026). Currently: Phase 0 complete —
[Foundation Map](docs/architecture/FOUNDATION_MAP.md), connecting the completed
[Concurrent and Distributed Systems in Rust](https://github.com/luca-zanolini/distributed-systems-in-rust)
course to this system's design. Phase 1 (the model-call layer) is next.

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

Built on: [distributed-systems-in-rust](https://github.com/luca-zanolini/distributed-systems-in-rust) —
twelve implemented modules from replicated registers to Raft, PBFT, transactional
concurrency control, and CRDTs.

## Author

[Luca Zanolini](https://lucazanolini.com) — PhD in distributed systems (Byzantine
consensus, asymmetric trust); previously Research Scientist at the Ethereum
Foundation (consensus protocol design and security: 3-Slot Finality, RLMD-GHOST,
ePBS security analysis).
